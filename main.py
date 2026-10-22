import json
import hashlib
import secrets
from pathlib import Path
from urllib.parse import urlencode

from nicegui import ui, app, run
from dotenv import load_dotenv
import os
import requests
from openai import OpenAIError
from analyse import generate_search_keywords
from search import (
    SearchError,
    build_query,
    build_query_groups,
    get_cached_result,
    search_one,
)
from views import page_shell, page_heading, home_page, empty_state, notice, repository_browser
# Load environment variables from .env file
load_dotenv()
project_dir = Path(__file__).resolve().parent
MAX_NEW_GROUPS = 20  # 每次最多抓 20 组未缓存结果，给 30 次/分钟留余量


def load_saved_stars():
    """读取当前本地收藏，仅供界面展示；不触发同步或模型调用。"""
    try:
        repos = json.loads((project_dir / 'starred_repos.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(repos, list) or not all(isinstance(repo, dict) for repo in repos):
        return None
    return repos


@ui.page('/auth/github/callback')
def github_callback_page(code: str = '', state: str = ''):
    with page_shell('同步收藏', 'stars'):
        page_heading('YOUR COLLECTION', '把喜欢的项目，带过来。', '同步 GitHub Stars，为下一次发现准备好起点。')
        with ui.row().classes('items-center') as loading:
            ui.spinner(size='24px')
            ui.label('正在连接 GitHub 并同步收藏，请稍候……')
        feedback = ui.column().classes('w-full').style('align-items: stretch')

        async def finish_sign_in():
            try:
                with feedback:
                    await sync_github_stars(code, state)
            finally:
                loading.delete()

        # 先返回页面，让浏览器建立连接，再开始网络请求。
        ui.timer(0.1, finish_sign_in, once=True, immediate=False)


async def sync_github_stars(code: str, state: str):
    # 保留原来的 OAuth 和同步流程，页面布局在上面的路由函数中建立。
    if not code or not state:
        ui.label('Missing code or state parameter.')
        return
    if state != app.storage.user.get('state'):
        ui.label('Invalid state parameter. Possible CSRF attack.')
        return
    del app.storage.user['state']  # Remove the state parameter from storage after validation
    url = "https://github.com/login/oauth/access_token"
    request_para = {
        "client_id": os.getenv("GITHUB_CLIENT_ID"),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": os.getenv("GITHUB_REDIRECT_URI"),
    }
    # Keep the request itself inside try: connection errors happen before a response exists.
    try:
        response = await run.io_bound(requests.post, url, data=request_para, headers={"Accept": "application/json"}, timeout=10)
        if response is None:
            return  # 服务关闭或任务被取消。
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.JSONDecodeError:
        ui.label("GitHub returned invalid token data. Please sign in again.")
        return
    except requests.exceptions.Timeout:
        ui.label("The token request timed out. Please sign in again.")
        return
    except requests.exceptions.HTTPError:
        ui.label("GitHub rejected the token request. Please sign in again.")
        return
    except requests.exceptions.RequestException:
        ui.label("Could not connect to GitHub to obtain a token. Please sign in again.")
        return
    if not isinstance(result, dict):
        ui.label("GitHub returned an unexpected token response. Please sign in again.")
        return
    if 'error' in result:
        ui.label("error: " + result['error'])
        return
    if result.get('access_token'):
        access_token = result['access_token']
        try:
            user_response = await run.io_bound(requests.get, "https://api.github.com/user", headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10", "Authorization": f"Bearer {access_token}"}, timeout=10)
            if user_response is None:
                return
            user_response.raise_for_status()
            user_info = user_response.json()
        except requests.exceptions.JSONDecodeError:
            ui.label("GitHub returned invalid user data. Please sign in again.")
            return
        except requests.exceptions.Timeout:
            ui.label("The user information request timed out. Please sign in again.")
            return
        except requests.exceptions.HTTPError:
            ui.label("GitHub rejected the user information request. Please sign in again.")
            return
        except requests.exceptions.RequestException:
            ui.label("Could not connect to GitHub to fetch user information. Please sign in again.")
            return
        if not isinstance(user_info, dict) or not user_info.get('login'):
            ui.label("GitHub returned user information without a username. Please sign in again.")
            return
        # 保存已验证的公开资料；搜索接口也需要 token（10 次/分 → 30 次/分）。
        # token 只保存在本机 .nicegui 会话文件里，该目录已在 .gitignore 中。
        app.storage.user['github_user'] = {
            'login': user_info['login'],
            'name': user_info.get('name') or user_info['login'],
            'avatar_url': user_info.get('avatar_url') or '',
        }
        app.storage.user['github_token'] = access_token
        notice(f"你好，{user_info.get('login')}。已成功连接 GitHub。")
        USERNAME = user_info.get('login')
    else:
        ui.label("No access token received.")
        return

    try:
        page = 1
        all_starred_repos = []
        while True:
            starred_response = await run.io_bound(requests.get, f"https://api.github.com/users/{USERNAME}/starred", headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10", "Authorization": f"Bearer {access_token}"}, timeout=10, params={"per_page": 100, "page": page})
            if starred_response is None:
                return
            starred_response.raise_for_status()
            starred_repos = starred_response.json()
            
            all_starred_repos.extend(starred_repos)
            page += 1
            if len(starred_repos) < 100:
                break
        with open(project_dir / "starred_repos.json", "w", encoding="utf-8") as f:
            json.dump(all_starred_repos, f)

        
    except requests.exceptions.JSONDecodeError:
        ui.label("GitHub returned invalid starred repositories data. Please try again.")
        return
    except requests.exceptions.Timeout:
        ui.label("The starred repositories request timed out. Please try again.")
        return
    except requests.exceptions.HTTPError:
        ui.label("GitHub rejected the starred repositories request. Please try again.")
        return
    except requests.exceptions.RequestException:
        ui.label("Could not connect to GitHub to fetch starred repositories. Please try again.")
        return
    except OSError:
        ui.label("Could not save starred repositories to disk.")
        return

    notice(f'已同步 {len(all_starred_repos):,} 个收藏。')
    if not all_starred_repos:
        notice('你的收藏还是空的。先在 GitHub 上 Star 一些感兴趣的项目，再回来同步。')
        ui.link('回到首页', '/').classes('text-link')
        return
    if await analyse_and_open_search():
        return
    with ui.row().classes('sync-actions'):
        ui.link('重试分析', '/analyse').classes('button-link primary-link')
        ui.link('查看已同步的 Stars', '/stars').classes('text-link')
    repository_browser(all_starred_repos, starred_ids={repo.get('id') for repo in all_starred_repos})


async def analyse_and_open_search() -> bool:
    """只在分析成功且有关键词时跳转；失败不会删除已同步的收藏。"""
    with ui.row().classes('items-center') as progress:
        ui.spinner(size='24px')
        ui.label('正在分析你的兴趣，请稍候……')
    try:
        # 分析函数不创建 UI，可以整段放到工作线程执行。
        keywords = await run.io_bound(generate_search_keywords)
    except OpenAIError:
        notice('收藏已保留，但模型请求失败。请检查网络、模型服务和 API 配置后重试分析。')
        return False
    except (ValueError, UnicodeError):
        notice('收藏已保留，但分析数据无效，或模型没有返回有效的 JSON。请检查收藏数据或重试分析。')
        return False
    except OSError:
        notice('分析未完成：无法读取收藏或保存关键词。请检查本地文件和目录权限后重试。')
        return False
    finally:
        progress.delete()
    if keywords is None:
        return False
    if not keywords:
        notice('模型暂时没有提取出兴趣关键词。可以重试分析，或更新收藏后再次同步。')
        return False
    ui.navigate.to('/search')
    return True


@ui.page('/analyse')
def analyse_page():
    with page_shell('分析兴趣', 'search'):
        page_heading('FOLLOW YOUR INTERESTS', '从收藏中，找到下一条线索。', '使用已同步的收藏生成兴趣关键词，无需重新登录 GitHub。')
        repos = load_saved_stars()
        if not repos:
            empty_state('还没有可分析的收藏', '先同步一些感兴趣的项目，再来开始分析。')
            return
        ui.label(f'将分析本地已同步的 {len(repos):,} 个仓库。').classes('muted')

        async def start_analysis():
            if not analyse_button.enabled:
                return
            analyse_button.disable()
            feedback.clear()
            try:
                with feedback:
                    await analyse_and_open_search()
            finally:
                analyse_button.enable()

        analyse_button = ui.button('开始分析', on_click=start_analysis).props('unelevated no-caps').classes('auth-button')
        feedback = ui.column().classes('w-full')


@ui.page('/auth/github')
def sign_github_page():
    state = secrets.token_urlsafe(16)
    app.storage.user['state'] = state
    authorize_url = 'https://github.com/login/oauth/authorize?' + urlencode({
        'client_id': os.getenv('GITHUB_CLIENT_ID', ''),
        'redirect_uri': os.getenv('GITHUB_REDIRECT_URI', ''),
        'state': state,
    })
    with page_shell('连接 GitHub'):
        with ui.column().classes('auth-panel'):
            ui.icon('eco', size='36px').classes('brand-icon')
            ui.label('A PLACE TO START').classes('eyebrow')
            ui.label('从你的收藏开始。').classes('page-title').props('role="heading" aria-level="1"')
            ui.label('连接 GitHub 后，LookingFor 会读取你的 Star 列表，作为发现更多项目的起点。').classes('muted')
            # 普通链接由浏览器直接跳转，不需要通过 WebSocket 调用 Python。
            with ui.link(target=authorize_url).classes('button-link primary-link'):
                ui.icon('arrow_forward', size='19px')
                ui.label('继续前往 GitHub')
            ui.label('你将在 GitHub 页面确认授权，完成后自动回到这里。').classes('muted')
            ui.link('先回去看看', '/').classes('text-link')


@ui.page('/')
def main_page():
    repos = load_saved_stars()
    with page_shell('发现', 'home'):
        home_page(len(repos) if repos is not None else None)


@ui.page('/stars')
def stars_page():
    with page_shell('我的 Stars', 'stars'):
        page_heading('SAVED FOR A REASON', '喜欢过，值得再看看。', '这里是上次同步的收藏。有些项目，第二次遇见才刚刚好。')
        repos = load_saved_stars()
        if repos is None:
            empty_state('还没有可浏览的收藏', '连接 GitHub 同步你的 Star 列表。如果本地数据无法读取，重新同步即可。')
            return
        with ui.row().classes('sync-actions'):
            ui.link('重新同步 Stars', '/auth/github').classes('button-link')
            ui.link('探索更多仓库', '/search').classes('text-link')
        if not repos:
            empty_state('你的收藏还是空的', '在 GitHub 上 Star 一些感兴趣的项目，再回来同步。')
            return
        repository_browser(repos, starred_ids={repo.get('id') for repo in repos})

def current_user_id() -> str:
    """登录用户用 GitHub login 作为限流身份。"""
    return (app.storage.user.get('github_user') or {}).get('login', '')


def search_error_message(code: str) -> str:
    if code.startswith('http_'):
        return f'GitHub 返回了 HTTP {code[5:]}'
    return {
        'timeout': '请求 GitHub 超时了',
        'network': '无法连接 GitHub',
        'unauthorized': 'GitHub 授权已失效',
        'json': 'GitHub 返回了无法解析的数据',
        'unexpected': 'GitHub 返回了意外的数据结构',
        'rate_limit': 'GitHub 搜索额度用完了',
        'busy': '同一批结果正在被另一个请求更新',
    }.get(code, '搜索暂时失败')


def merge_group_items(repos_by_id: dict, items: list[dict], group: list[str]) -> None:
    """把一组搜索结果合并进 repos_by_id。"""
    for repo in items:
        if not isinstance(repo, dict) or not isinstance(repo.get('id'), int):
            continue
        if repo['id'] not in repos_by_id:
            repos_by_id[repo['id']] = {**repo, 'matched_keywords': []}
        for keyword in group:
            if keyword not in repos_by_id[repo['id']]['matched_keywords']:
                repos_by_id[repo['id']]['matched_keywords'].append(keyword)


@ui.page('/search')
def search_page():
    with page_shell('探索仓库', 'search'):
        page_heading(
            'A NEW PATH TO EXPLORE',
            '循着兴趣，发现更多。',
            '从你的收藏中提炼关键词，寻找值得点开的仓库。熟悉的项目，也会留在这里。',
        )

        user = app.storage.user.get('github_user') or {}
        if not user.get('login'):
            empty_state('先连接 GitHub', '登录后才能根据你的收藏生成推荐。')
            return

        if not app.storage.user.get('github_token'):
            empty_state('需要重新授权一次', '为了使用搜索额度，请重新连接 GitHub。')
            return

        with ui.row().classes('items-center') as loading:
            ui.spinner(size='24px')
            ui.label('正在读取缓存并补充搜索结果，请稍候……')

        results = ui.column().classes('w-full').style('align-items: stretch')

        async def load_results():
            try:
                with results:
                    await search_repositories()
            finally:
                loading.delete()

        ui.timer(0.1, load_results, once=True, immediate=False)


async def search_repositories():
    # This first version reads the single local user's saved analysis.
    try:
        keyword_data = json.loads((project_dir / 'search_keywords.json').read_text(encoding='utf-8'))
        source_content = (project_dir / 'starred_repos.json').read_bytes()
    except FileNotFoundError:
        empty_state('探索，从一点准备开始', '先同步你的 Stars，再生成兴趣关键词。准备好之后，相关仓库就会出现在这里。', setup=True)
        return
    except (OSError, UnicodeError, json.JSONDecodeError):
        empty_state('暂时无法读取推荐数据', '请重新生成兴趣关键词，然后再次打开探索页面。', setup=True)
        return

    if not isinstance(keyword_data, dict) or not isinstance(keyword_data.get('keywords'), list):
        empty_state('兴趣关键词需要重新生成', '保存的数据格式不正确，请重新运行分析。', setup=True)
        return
    if keyword_data.get('source_sha256') != hashlib.sha256(source_content).hexdigest():
        empty_state('你的收藏有了新变化', '重新生成兴趣关键词，让这次探索跟上你的新兴趣。', setup=True)
        return
    keywords = keyword_data['keywords']
    if not all(isinstance(keyword, str) and keyword.strip() for keyword in keywords):
        empty_state('兴趣关键词需要重新生成', '保存的关键词格式不正确，请重新运行分析。', setup=True)
        return
    if not keywords:
        empty_state('还没有找到兴趣关键词', '请确认已同步收藏，然后重新运行分析。', setup=True)
        return

    selected_keywords = list(dict.fromkeys(keywords))
    groups = build_query_groups(selected_keywords)
    total_groups = len(groups)

    user_id = current_user_id()
    token = app.storage.user.get('github_token')
    if not user_id or not token:
        empty_state('需要重新连接 GitHub', '登录并授权后才能生成推荐。')
        return

    repos_by_id: dict[int, dict] = {}
    pending = []
    cached_groups = 0
    fetched_groups = 0
    rate_limited = False
    wait_seconds = 0.0
    failed_code = None
    incomplete = False

    # 已缓存的组先直接合并，不消耗请求和令牌。
    for group in groups:
        query = build_query(group)
        cached = get_cached_result(query)
        if isinstance(cached, dict) and isinstance(cached.get('items'), list):
            cached_groups += 1
            if cached.get('incomplete_results'):
                incomplete = True
            merge_group_items(repos_by_id, cached['items'], group)
        else:
            pending.append((group, query))

    # 本次最多抓 MAX_NEW_GROUPS 组未缓存的结果。
    for group, query in pending[:MAX_NEW_GROUPS]:
        try:
            result = await search_one(query, user_id, token)
        except SearchError as exc:
            failed_code = exc.code
            if exc.code == 'unauthorized' and 'github_token' in app.storage.user:
                del app.storage.user['github_token']
            if exc.code == 'rate_limit':
                rate_limited = True
            break

        if result is None:
            return  # 服务关闭或任务被取消

        if result.source == 'rate_limited':
            rate_limited = True
            wait_seconds = result.wait
            break
        if result.source == 'busy':
            failed_code = 'busy'
            break
        if not isinstance(result.data, dict) or not isinstance(result.data.get('items'), list):
            failed_code = 'unexpected'
            break

        if result.source == 'cache':
            cached_groups += 1
        else:
            fetched_groups += 1

        if result.data.get('incomplete_results'):
            incomplete = True
        merge_group_items(repos_by_id, result.data['items'], group)

    all_repos = list(repos_by_id.values())
    completed_groups = cached_groups + fetched_groups
    skipped_groups = max(0, total_groups - completed_groups)

    if rate_limited:
        if wait_seconds > 0:
            wait_text = f'约 {max(1, round(wait_seconds))} 秒后'
        else:
            wait_text = '稍后'
        notice(f'已拿到 {completed_groups}/{total_groups} 组结果。GitHub 搜索额度用完了，建议 {wait_text}再试。')
    elif failed_code == 'unauthorized':
        notice('GitHub 授权已失效，请重新连接。已先展示拿到的结果。')
        ui.link('重新连接 GitHub', '/auth/github').classes('text-link')
    elif failed_code:
        notice(f'{search_error_message(failed_code)}，已先展示拿到的结果。')
    elif skipped_groups:
        notice(f'本次先更新 {fetched_groups} 组，剩余 {skipped_groups} 组已保留，下次打开会继续。')
    elif incomplete:
        notice('GitHub 提示部分搜索结果可能不完整，先看看已经找到的仓库。')

    if cached_groups:
        ui.label(f'本次 {total_groups} 组中，{cached_groups} 组来自 Redis 缓存。').classes('muted')

    if not all_repos:
        if rate_limited:
            empty_state('这次没有拿到结果', 'GitHub 搜索额度暂时用完了，等一会儿再试。')
        elif failed_code:
            empty_state('这次没有拿到结果', f'{search_error_message(failed_code)}，请稍后再试。')
        elif skipped_groups:
            empty_state('这次先更新了一部分', f'还有 {skipped_groups} 组没有抓取，下次打开会继续。')
        else:
            empty_state('这次没有找到仓库', 'GitHub 暂时没有返回匹配仓库，可以调整兴趣关键词后再来探索。')
        return

    saved_stars = load_saved_stars() or []
    repository_browser(all_repos, starred_ids={repo.get('id') for repo in saved_stars}, keywords=selected_keywords)

ui.run(host='127.0.0.1', port=8080, storage_secret=os.getenv("STORAGE_SECRET"))
