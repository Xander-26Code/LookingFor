import os

from dotenv import load_dotenv

# 先加载 .env，再导入 NiceGUI/数据库：NICEGUI_STORAGE_PATH、DATABASE_PATH 在 import 时读取。
load_dotenv()

import asyncio
import logging
import math
import secrets
import sqlite3
import time
from urllib.parse import urlencode

from fastapi.responses import JSONResponse
from nicegui import ui, app, run
import requests

from analyse import generate_search_keywords
from database import (
    delete_user_data,
    get_analysis_job,
    init_db,
    load_user_data,
    ping as database_ping,
    save_star_snapshot,
    save_user,
)
from analysis_jobs import start_analysis, wait_for_analysis
from discovery import DiscoverySession
from i18n import L, set_language
from search_guard import ping as redis_ping
from views import page_shell, page_heading, home_page, empty_state, notice, repository_browser

logger = logging.getLogger(__name__)
TOKEN_URL = "https://github.com/login/oauth/access_token"
TOKEN_REFRESH_MARGIN = 120
_token_locks: dict[int, asyncio.Lock] = {}


def current_user_id() -> int | None:
    """用户身份只取自服务端登录会话，不接受 URL 中传入的用户 ID。"""
    user = app.storage.user.get('github_user') or {}
    user_id = user.get('id')
    return user_id if type(user_id) is int and user_id > 0 else None


def require_user() -> int | None:
    user_id = current_user_id()
    if user_id is None:
        empty_state(
            L('请连接 GitHub', 'Connect GitHub'),
            L('登录后查看自己的收藏和推荐；旧版登录状态需要重新授权一次。',
              'Sign in to see your collection and recommendations. Older sessions need to reconnect once.'),
        )
    return user_id


def _safe_delete(element) -> None:
    """刷新或关闭页面后，客户端可能已经不存在；清理失败不应影响后台任务。"""
    try:
        element.delete()
    except (RuntimeError, ValueError):
        pass


def _remove_session_key(key: str) -> None:
    if key in app.storage.user:
        del app.storage.user[key]


def clear_github_tokens() -> None:
    for key in (
        'github_token',
        'github_refresh_token',
        'github_token_expires_at',
        'github_refresh_expires_at',
    ):
        _remove_session_key(key)


def clear_github_session() -> None:
    clear_github_tokens()
    for key in ('github_user', 'state'):
        _remove_session_key(key)


def _positive_seconds(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def store_github_tokens(payload: dict, *, keep_refresh: bool = False) -> None:
    """保存 GitHub App/OAuth 返回的 token 及过期时间，不把 token 写到页面。"""
    token = payload.get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('GitHub did not return an access token.')

    now = time.time()
    app.storage.user['github_token'] = token

    refresh_token = payload.get('refresh_token')
    if isinstance(refresh_token, str) and refresh_token:
        app.storage.user['github_refresh_token'] = refresh_token
    elif not keep_refresh:
        _remove_session_key('github_refresh_token')

    expires_in = _positive_seconds(payload.get('expires_in'))
    if expires_in is None:
        _remove_session_key('github_token_expires_at')
    else:
        app.storage.user['github_token_expires_at'] = now + expires_in

    refresh_expires_in = _positive_seconds(payload.get('refresh_token_expires_in'))
    if refresh_expires_in is None:
        _remove_session_key('github_refresh_expires_at')
    else:
        app.storage.user['github_refresh_expires_at'] = now + refresh_expires_in


def _token_lock(user_id: int) -> asyncio.Lock:
    return _token_locks.setdefault(user_id, asyncio.Lock())


async def refresh_github_token() -> str | None:
    """用 refresh token 换新 token；失败时保留会话，让用户可以在受控环境下重试。"""
    refresh_token = app.storage.user.get('github_refresh_token')
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    refresh_expires_at = app.storage.user.get('github_refresh_expires_at')
    if isinstance(refresh_expires_at, (int, float)) and refresh_expires_at <= time.time():
        clear_github_tokens()
        return None

    data = {
        'client_id': os.getenv('GITHUB_CLIENT_ID', ''),
        'client_secret': os.getenv('GITHUB_CLIENT_SECRET', ''),
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }
    try:
        response = await run.io_bound(
            requests.post,
            TOKEN_URL,
            data=data,
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        if response is None:
            return None
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if response.status_code >= 400 or payload.get('error'):
        if payload.get('error') in {'bad_refresh_token', 'invalid_grant'}:
            clear_github_tokens()
        return None
    try:
        store_github_tokens(payload, keep_refresh=True)
    except ValueError:
        return None
    return payload['access_token']


async def get_valid_github_token() -> str | None:
    """token 有效就复用；快过期或已过期时用 refresh token 换新。"""
    user_id = current_user_id()
    if user_id is None:
        token = app.storage.user.get('github_token')
        return token if isinstance(token, str) and token else None

    async with _token_lock(user_id):
        token = app.storage.user.get('github_token')
        expires_at = app.storage.user.get('github_token_expires_at')
        expires_soon = (
            isinstance(expires_at, (int, float))
            and expires_at <= time.time() + TOKEN_REFRESH_MARGIN
        )
        if isinstance(token, str) and token and not expires_soon:
            return token
        return await refresh_github_token()


@app.get('/health')
def health():
    """平台健康检查：数据库不可用返回 503；Redis 不可用返回 degraded。"""
    database_ok = True
    try:
        database_ping()
    except Exception:
        database_ok = False
        logger.warning('Database health check failed')

    redis_ok = True
    try:
        redis_ok = bool(redis_ping())
    except Exception:
        redis_ok = False
        logger.warning('Redis health check failed')

    if not database_ok:
        status = 'error'
    elif not redis_ok:
        status = 'degraded'
    else:
        status = 'ok'
    return JSONResponse(
        {
            'status': status,
            'database': 'ok' if database_ok else 'error',
            'redis': 'ok' if redis_ok else 'error',
        },
        status_code=200 if database_ok else 503,
    )


async def load_saved_stars():
    """只读取当前登录用户的收藏；匿名和未同步用户均不会回退到旧文件。"""
    user_id = current_user_id()
    if user_id is None:
        return None
    try:
        data = await run.io_bound(load_user_data, user_id)
    except (sqlite3.Error, ValueError):
        notice(L('暂时无法读取收藏，请稍后重试。',
                 'Could not load saved stars. Please try again later.'))
        return None
    if current_user_id() != user_id:
        return None
    return data['repositories'] if data is not None else None


@ui.page('/auth/github/callback')
def github_callback_page(code: str = '', state: str = ''):
    with page_shell(L('同步收藏', 'Sync collection'), 'stars'):
        page_heading(
            L('你的收藏', 'YOUR COLLECTION'),
            L('把喜欢的项目，带过来。', 'Bring your favorites with you.'),
            L('同步 GitHub Stars，为下一次发现准备好起点。',
              'Sync your GitHub Stars and prepare a starting point for the next discovery.'),
        )
        with ui.row().classes('items-center') as loading:
            ui.spinner(size='24px')
            ui.label(L('正在连接 GitHub 并同步收藏，请稍候……',
                       'Connecting to GitHub and syncing your stars…'))
        feedback = ui.column().classes('w-full').style('align-items: stretch')

        async def finish_sign_in():
            try:
                with feedback:
                    await sync_github_stars(code, state)
            finally:
                _safe_delete(loading)

        # 先返回页面，让浏览器建立连接，再开始网络请求。
        ui.timer(0.1, finish_sign_in, once=True, immediate=False)


async def sync_github_stars(code: str, state: str):
    # 保留原来的 OAuth 和同步流程，页面布局在上面的路由函数中建立。
    if not code or not state:
        ui.label(L('缺少 code 或 state 参数。', 'Missing code or state parameter.'))
        return
    if state != app.storage.user.get('state'):
        ui.label(L('state 参数无效，可能是 CSRF 攻击。',
                   'Invalid state parameter. Possible CSRF attack.'))
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
        ui.label(L('GitHub 返回了无效的 token 数据，请重新登录。',
                   'GitHub returned invalid token data. Please sign in again.'))
        return
    except requests.exceptions.Timeout:
        ui.label(L('获取 token 超时，请重新登录。',
                   'The token request timed out. Please sign in again.'))
        return
    except requests.exceptions.HTTPError:
        ui.label(L('GitHub 拒绝了 token 请求，请重新登录。',
                   'GitHub rejected the token request. Please sign in again.'))
        return
    except requests.exceptions.RequestException:
        ui.label(L('无法连接 GitHub 获取 token，请重新登录。',
                   'Could not connect to GitHub to obtain a token. Please sign in again.'))
        return
    if not isinstance(result, dict):
        ui.label(L('GitHub 返回了意外的 token 响应，请重新登录。',
                   'GitHub returned an unexpected token response. Please sign in again.'))
        return
    if 'error' in result:
        ui.label(L(f"错误：{result['error']}", f"Error: {result['error']}"))
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
            ui.label(L('GitHub 返回了无效的用户数据，请重新登录。',
                       'GitHub returned invalid user data. Please sign in again.'))
            return
        except requests.exceptions.Timeout:
            ui.label(L('获取用户信息超时，请重新登录。',
                       'The user information request timed out. Please sign in again.'))
            return
        except requests.exceptions.HTTPError:
            ui.label(L('GitHub 拒绝了用户信息请求，请重新登录。',
                       'GitHub rejected the user information request. Please sign in again.'))
            return
        except requests.exceptions.RequestException:
            ui.label(L('无法连接 GitHub 获取用户信息，请重新登录。',
                       'Could not connect to GitHub to fetch user information. Please sign in again.'))
            return
        try:
            # 数据库可能等待写锁，放到工作线程，避免阻塞其他用户的页面。
            saved_user = await run.io_bound(save_user, user_info)
        except ValueError:
            ui.label(L('GitHub 返回的用户 ID 或用户名无效，请重新登录。',
                       'GitHub returned an invalid user ID or login. Please sign in again.'))
            return
        except sqlite3.Error:
            ui.label(L('暂时无法保存用户资料，请稍后重新登录。',
                       'Could not save your profile. Please sign in again later.'))
            return
        if saved_user is None:
            return  # 服务关闭或任务被取消。
        # 数据库提交成功后才更新当前浏览器的登录身份。
        # token/refresh token 只保存在服务端会话存储，不写入页面或日志。
        app.storage.user['github_user'] = saved_user
        store_github_tokens(result)
        notice(L(f"你好，{user_info.get('login')}。已成功连接 GitHub。",
                 f"Hi, {user_info.get('login')}. GitHub connected successfully."))
        USERNAME = user_info.get('login')
    else:
        ui.label(L('没有收到 access token。', 'No access token received.'))
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
            
            if not isinstance(starred_repos, list) or not all(isinstance(repo, dict) for repo in starred_repos):
                ui.label(L('GitHub 返回的收藏格式无效，之前的收藏已保留。',
                           'GitHub returned invalid starred repositories data. Previous stars were preserved.'))
                return
            all_starred_repos.extend(starred_repos)
            page += 1
            if len(starred_repos) < 100:
                break
        if current_user_id() != saved_user['id']:
            return
        version = await run.io_bound(save_star_snapshot, saved_user['id'], all_starred_repos)
        if version is None or current_user_id() != saved_user['id']:
            return

        
    except requests.exceptions.JSONDecodeError:
        ui.label(L('GitHub 返回了无效的收藏数据，请重试。',
                   'GitHub returned invalid starred repositories data. Please try again.'))
        return
    except requests.exceptions.Timeout:
        ui.label(L('获取收藏超时，请重试。',
                   'The starred repositories request timed out. Please try again.'))
        return
    except requests.exceptions.HTTPError:
        ui.label(L('GitHub 拒绝了收藏请求，请重试。',
                   'GitHub rejected the starred repositories request. Please try again.'))
        return
    except requests.exceptions.RequestException:
        ui.label(L('无法连接 GitHub 获取收藏，请重试。',
                   'Could not connect to GitHub to fetch starred repositories. Please try again.'))
        return
    except (sqlite3.Error, ValueError):
        ui.label(L('无法保存收藏，之前的收藏已保留，请稍后重试。',
                   'Could not save stars. Previous stars were preserved. Please try again later.'))
        return

    notice(L(f'已同步 {len(all_starred_repos):,} 个收藏。',
             f'Synced {len(all_starred_repos):,} starred repositories.'))
    if not all_starred_repos:
        notice(L('你的收藏还是空的。先在 GitHub 上 Star 一些感兴趣的项目，再回来同步。',
                 'Your collection is empty. Star some projects on GitHub, then come back to sync.'))
        ui.link(L('回到首页', 'Back to home'), '/').classes('text-link')
        return
    if await analyse_and_open_search():
        return
    if current_user_id() != saved_user['id']:
        return
    with ui.row().classes('sync-actions'):
        ui.link(L('重试分析', 'Retry analysis'), '/analyse').classes('button-link primary-link')
        ui.link(L('查看已同步的 Stars', 'View synced Stars'), '/stars').classes('text-link')
    repository_browser(all_starred_repos, starred_ids={repo.get('id') for repo in all_starred_repos})


async def analyse_and_open_search() -> bool:
    user_id = require_user()
    if user_id is None:
        return False
    with ui.row().classes('items-center') as progress:
        ui.spinner(size='24px')
        status_label = ui.label(L('正在检查分析状态……', 'Checking analysis status…'))
    try:
        result = await start_analysis(user_id, generate_search_keywords)
        if result['status'] == 'running':
            status_label.set_text(
                L('已有分析正在运行，等待同一份结果……', 'An analysis is already running; waiting for the same result…')
                if result['joined'] else
                L('正在分析你的兴趣……', 'Analyzing your interests…')
            )
            result = await wait_for_analysis(user_id, result['job_id'])
        if current_user_id() != user_id:
            return False
        status = result['status']
        if status == 'succeeded':
            ui.navigate.to('/search')
            return True
        if status == 'limited':
            notice(L(
                f"分析请求较频繁，请约 {math.ceil(result['wait'])} 秒后再试。已有结果仍可浏览。",
                f"Analysis was requested too often. Try again in about {math.ceil(result['wait'])} seconds. "
                "Existing results are still available.",
            ))
        else:
            notice({
                'empty': L('还没有可分析的收藏，请先同步 Stars。',
                           'There are no stars to analyze. Sync your Stars first.'),
                'busy': L('当前分析名额已满，请稍后重试。此次没有调用模型。',
                          'All analysis slots are busy. Please try again later. No model call was made.'),
                'stale': L('分析期间收藏已更新，旧结果未覆盖新收藏，请重新分析。',
                           'Your stars changed during analysis. The old result did not overwrite the new collection; please analyze again.'),
                'interrupted': L('之前的分析任务已中断，请重新开始分析。',
                                 'The previous analysis was interrupted. Please start again.'),
                'failed': L('分析失败，收藏已保留。请稍后重试；收藏较多时请检查分析输入限制。',
                            'Analysis failed, but your stars were preserved. Please try again later.'),
            }.get(status, L('分析暂时未完成，请稍后重试。',
                            'Analysis did not finish. Please try again later.')))
    except (sqlite3.Error, OSError):
        notice(L('暂时无法读取或保存分析状态，请稍后重试。',
                 'Could not read or save analysis status. Please try again later.'))
    finally:
        _safe_delete(progress)
    return False


@ui.page('/analyse')
async def analyse_page():
    with page_shell(L('分析兴趣', 'Analyze interests'), 'search'):
        page_heading(
            L('跟随你的兴趣', 'FOLLOW YOUR INTERESTS'),
            L('从收藏中，找到下一条线索。', 'Find the next clue in your collection.'),
            L('使用已同步的收藏生成兴趣关键词，无需重新登录 GitHub。',
              'Generate interest keywords from your synced stars without signing in to GitHub again.'),
        )
        if require_user() is None:
            return
        repos = await load_saved_stars()
        if not repos:
            empty_state(L('还没有可分析的收藏', 'No stars to analyze yet'),
                        L('先同步一些感兴趣的项目，再来开始分析。',
                          'Sync some projects you are interested in, then come back to analyze.'))
            return
        ui.label(L(f'将分析你已同步的 {len(repos):,} 个仓库。',
                   f'Analyzing {len(repos):,} synced repositories.')).classes('muted')
        ui.label(L(
            '收藏较多时会按输入预算自动分批，最多 6 批；全站同时最多 2 个任务，'
            '每人每 60 秒至多发起一次、24 小时最多 10 次。已有结果和正在执行的任务会复用。',
            'Large collections are split into up to 6 batches by input size. '
            'At most 2 analyses run globally; each user can start one every 60 seconds, '
            'up to 10 per 24 hours. Existing results and running jobs are reused.',
        )).classes('muted')
        try:
            job = await run.io_bound(get_analysis_job, current_user_id())
            if job:
                status_text = {
                    'running': L('正在运行，可点击下方按钮查看进度', 'Running; use the button below to follow progress'),
                    'succeeded': L('已完成', 'Completed'),
                    'failed': L('失败，可稍后重试', 'Failed; try again later'),
                    'stale': L('收藏已更新，需重新分析', 'Stars changed; analyze again'),
                    'interrupted': L('已中断，可重试', 'Interrupted; you can retry'),
                }.get(job['status'], job['status'])
                ui.label(L(f'上次分析：{status_text}', f'Last analysis: {status_text}')).classes('muted')
        except (sqlite3.Error, ValueError):
            notice(L('暂时无法读取任务状态，请稍后重试。',
                     'Could not read job status. Please try again later.'))

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

        analyse_button = ui.button(L('开始分析', 'Start analysis'), on_click=start_analysis).props(
            'unelevated no-caps').classes('auth-button')
        feedback = ui.column().classes('w-full')


@ui.page('/lang/{language}')
def language_page(language: str):
    if language in ('zh', 'en'):
        set_language(language)
    ui.navigate.to('/')


@ui.page('/auth/github')
def sign_github_page():
    state = secrets.token_urlsafe(16)
    app.storage.user['state'] = state
    authorize_url = 'https://github.com/login/oauth/authorize?' + urlencode({
        'client_id': os.getenv('GITHUB_CLIENT_ID', ''),
        'redirect_uri': os.getenv('GITHUB_REDIRECT_URI', ''),
        'state': state,
    })
    with page_shell(L('连接 GitHub', 'Connect GitHub')):
        with ui.column().classes('auth-panel'):
            ui.icon('eco', size='36px').classes('brand-icon')
            ui.label(L('从这里开始', 'A PLACE TO START')).classes('eyebrow')
            ui.label(L('从你的收藏开始。', 'Start with your collection.')).classes(
                'page-title').props('role="heading" aria-level="1"')
            ui.label(L(
                '连接 GitHub 后，LookingFor 会读取你的 Star 列表，作为发现更多项目的起点。',
                'After connecting GitHub, LookingFor reads your starred repositories as a starting point '
                'for discovering more projects.',
            )).classes('muted')
            # 普通链接由浏览器直接跳转，不需要通过 WebSocket 调用 Python。
            with ui.link(target=authorize_url).classes('button-link primary-link'):
                ui.icon('arrow_forward', size='19px')
                ui.label(L('继续前往 GitHub', 'Continue to GitHub'))
            ui.label(L('你将在 GitHub 页面确认授权，完成后自动回到这里。',
                       'You will confirm authorization on GitHub and return here automatically.')).classes('muted')
            ui.link(L('先回去看看', 'Back to home'), '/').classes('text-link')


@ui.page('/auth/logout')
def logout_page():
    clear_github_session()
    with page_shell(L('已退出', 'Signed out')):
        page_heading(
            L('已退出', 'SIGNED OUT'),
            L('已退出当前账号。', 'You have signed out.'),
            L('本机会话中的登录信息和 GitHub token 已清除。',
              'Login information and the GitHub token have been cleared from this browser session.'),
        )
        with ui.row().classes('sync-actions'):
            ui.link(L('回到首页', 'Back to home'), '/').classes('button-link primary-link')
            ui.link(L('重新连接 GitHub', 'Reconnect GitHub'), '/auth/github').classes('text-link')


PRIVACY_ZH = """
### 我们保存什么

- GitHub 账号公开资料：`github_id`、登录名、昵称、头像。
- 你同步的 Star 仓库快照：仓库名、简介、语言、topics、URL、fork 数等公开信息。
- 模型生成的关键词、分析任务状态和时间。
- OAuth access token / refresh token（如果 GitHub 返回）：只保存在服务端会话存储，不下发到页面。
- Redis 搜索缓存：只保存公开仓库的搜索结果，默认 6 小时后过期，不含你的 token。

### 用来做什么

- 登录并识别你的账号。
- 同步你的 Stars，生成兴趣关键词。
- 搜索公开仓库并显示推荐。
- 执行限流、缓存和故障恢复。

### 会发给第三方什么

- **GitHub API**：你的访问 token、Stars 请求、搜索关键词。
- **DeepSeek**：仓库名称、简介、语言、topics、URL、fork 数，用于生成兴趣关键词；不会发送 token、登录名或邮箱。
- **托管服务和 Redis 服务商**：运行本服务所需的服务器日志和缓存数据。

### 保存多久

- Stars 和分析结果：直到你重新同步且内容发生变化，或主动删除。
- 搜索缓存：默认 6 小时；限流计数：约 2 分钟。
- Token：直到退出登录、GitHub 撤销授权或 token 过期。

### 你的选择

- 退出登录：清除本机会话中的账号和 token。
- 删除数据：删除收藏、分析、任务记录和账号行，操作不可恢复。
- 撤销 GitHub 授权：在 GitHub 的 Settings → Applications 中撤销本应用。

### 安全说明

- Token 只保存在服务端会话存储，不会显示在页面或写入应用日志。
- 当前 beta 环境需限制服务器和会话存储的访问；正式公开前会加入 token 加密和更完整的审计。
- 有问题可以通过项目 Issues 联系。
"""

PRIVACY_EN = """
### What we store

- Public GitHub profile data: `github_id`, login, name, and avatar.
- A snapshot of your starred repositories: public names, descriptions, languages, topics, URLs, and fork counts.
- Keywords generated by the model, analysis job status, and timestamps.
- OAuth access token / refresh token (if GitHub returns one): server-side session storage only; never sent to the page.
- Redis search cache: public repository search results only, expiring after 6 hours, with no token.

### What we use it for

- Signing you in and identifying your account.
- Syncing your Stars and generating interest keywords.
- Searching public repositories and showing recommendations.
- Rate limiting, caching, and failure recovery.

### What is shared with third parties

- **GitHub API**: your access token, Stars requests, and search keywords.
- **DeepSeek**: repository names, descriptions, languages, topics, URLs, and fork counts used to generate keywords; no token, login, or email is sent.
- **Hosting and Redis providers**: server logs and cached data needed to run the service.

### Retention

- Stars and analysis results: until you resync with changes or delete them.
- Search cache: 6 hours by default; rate-limit counters: about 2 minutes.
- Token: until you sign out, revoke the GitHub authorization, or the token expires.

### Your choices

- Sign out: clear the account and token from this browser session.
- Delete data: delete stars, analyses, job records, and the account row. This cannot be undone.
- Revoke GitHub authorization: GitHub Settings → Applications.

### Security

- Tokens live only in server-side session storage; they are not shown in the page or written to application logs.
- The beta environment is not encrypted at rest; server and session storage access must be restricted. Encryption and fuller auditing will be added before public launch.
- Contact us through the project Issues page.
"""


@ui.page('/privacy')
def privacy_page():
    with page_shell(L('隐私与数据', 'Privacy & data')):
        page_heading(
            L('你的数据', 'YOUR DATA'),
            L('你的数据，怎么被使用。', 'How your data is used.'),
            L('这是 LookingFor beta 版本的数据说明；正式公开前会继续更新。',
              'This describes data handling in the LookingFor beta. It will be updated before public launch.'),
        )
        ui.markdown(L(PRIVACY_ZH, PRIVACY_EN)).classes('privacy-content')
        with ui.row().classes('sync-actions'):
            ui.link(L('删除我的数据', 'Delete my data'), '/account/delete').classes('button-link')
            ui.link(L('退出登录', 'Sign out'), '/auth/logout').classes('text-link')
            ui.link(L('项目 Issues', 'Project Issues'),
                    'https://github.com/Xander-26Code/LookingFor/issues',
                    new_tab=True).classes('text-link').props('rel="noopener noreferrer"')


@ui.page('/account/delete')
def delete_account_page():
    user_id = require_user()
    if user_id is None:
        return
    with page_shell(L('删除数据', 'Delete data')):
        page_heading(
            L('删除你的数据', 'DELETE YOUR DATA'),
            L('删除我的数据', 'Delete my data'),
            L('这会删除你的收藏、分析、任务记录和账号行，操作不可恢复。',
              'This deletes your stars, analysis, job records, and account row. It cannot be undone.'),
        )
        notice(L(
            'GitHub 上的 Stars 不会被删除；Redis 中不含个人 token 的公开搜索缓存会在 6 小时内自然过期。',
            'Your Stars on GitHub are not deleted. Public search cache in Redis contains no personal token '
            'and expires naturally within 6 hours.',
        ))
        with ui.column().classes('auth-panel'):
            ui.label(L('确认后将从 LookingFor 数据库删除：',
                       'This will delete from the LookingFor database:')).classes('section-title')
            ui.label(L(
                'GitHub 资料行、Star 快照、兴趣关键词、分析任务记录、会话中的 token。',
                'GitHub profile row, star snapshot, interest keywords, analysis job records, and session tokens.',
            )).classes('muted')
            confirm = ui.checkbox(L(
                '我确认删除自己的数据，且了解无法恢复。',
                'I confirm that I want to delete my data and understand this cannot be undone.',
            ))

            async def delete_account():
                if not confirm.value:
                    return
                delete_button.disable()
                try:
                    await run.io_bound(delete_user_data, user_id)
                except sqlite3.Error:
                    notice(L('删除失败，请稍后重试。', 'Delete failed. Please try again later.'))
                    delete_button.enable()
                    return
                if current_user_id() != user_id:
                    return
                clear_github_session()
                ui.navigate.to('/')

            delete_button = ui.button(L('删除我的数据', 'Delete my data'), on_click=delete_account).props(
                'unelevated no-caps color=negative').classes('auth-button')
            delete_button.bind_enabled_from(confirm, 'value')
            delete_button.disable()
            ui.link(L('返回隐私说明', 'Back to privacy'), '/privacy').classes('text-link')


@ui.page('/')
async def main_page():
    with page_shell(L('发现', 'Discover'), 'home'):
        repos = await load_saved_stars()
        home_page(len(repos) if repos is not None else None)


@ui.page('/stars')
async def stars_page():
    with page_shell(L('我的 Stars', 'My Stars'), 'stars'):
        page_heading(
            L('值得收藏', 'SAVED FOR A REASON'),
            L('喜欢过，值得再看看。', 'You liked it once; it is worth another look.'),
            L('这里是上次同步的收藏。有些项目，第二次遇见才刚刚好。',
              'These are the stars from your last sync. Some projects are even better the second time.'),
        )
        if require_user() is None:
            return
        repos = await load_saved_stars()
        if repos is None:
            empty_state(L('还没有可浏览的收藏', 'No stars to browse yet'),
                        L('连接 GitHub 同步你的 Star 列表。首次使用新版需要重新同步一次。',
                          'Connect GitHub to sync your Stars. First-time users of this version need to sync once.'))
            return
        with ui.row().classes('sync-actions'):
            ui.link(L('重新同步 Stars', 'Resync Stars'), '/auth/github').classes('button-link')
            ui.link(L('探索更多仓库', 'Explore more repositories'), '/search').classes('text-link')
        if not repos:
            empty_state(L('你的收藏还是空的', 'Your collection is empty'),
                        L('在 GitHub 上 Star 一些感兴趣的项目，再回来同步。',
                          'Star some projects on GitHub, then come back to sync.'))
            return
        repository_browser(repos, starred_ids={repo.get('id') for repo in repos})


def search_error_message(code: str) -> str:
    if code.startswith('http_'):
        return L(f'GitHub 返回了 HTTP {code[5:]}', f'GitHub returned HTTP {code[5:]}')
    return {
        'timeout': L('请求 GitHub 超时了', 'The GitHub request timed out'),
        'network': L('无法连接 GitHub', 'Could not connect to GitHub'),
        'unauthorized': L('GitHub 授权已失效', 'GitHub authorization expired'),
        'json': L('GitHub 返回了无法解析的数据', 'GitHub returned unreadable data'),
        'unexpected': L('GitHub 返回了意外的数据结构', 'GitHub returned an unexpected data structure'),
        'rate_limit': L('GitHub 搜索额度用完了', 'GitHub search quota is exhausted'),
        'busy': L('搜索正在其他页面运行，请稍后重试', 'Search is running in another tab. Please try again later'),
        'cache_unavailable': L('搜索缓存服务暂时不可用', 'Search cache is temporarily unavailable'),
        'page_limit': L('这组查询已达到 GitHub 的分页上限', 'This query reached GitHub’s pagination limit'),
    }.get(code, L('搜索暂时失败', 'Search failed'))


@ui.page('/search')
def search_page():
    with page_shell(L('探索仓库', 'Explore'), 'search'):
        page_heading(
            L('一条新的探索路径', 'A NEW PATH TO EXPLORE'),
            L('循着兴趣，发现更多。', 'Follow your interests. Discover more.'),
            L('从你的收藏中提炼关键词，寻找值得点开的仓库。熟悉的项目，也会留在这里。',
              'Turn your collection into keywords and find repositories worth opening. '
              'Familiar projects stay here too.'),
        )

        if require_user() is None:
            return

        if not app.storage.user.get('github_token') and not app.storage.user.get('github_refresh_token'):
            empty_state(L('需要重新授权一次', 'Reconnect once'),
                        L('为了使用搜索额度，请重新连接 GitHub。',
                          'Reconnect GitHub to use your search quota.'))
            return

        with ui.row().classes('items-center') as loading:
            ui.spinner(size='24px')
            ui.label(L('正在读取缓存并补充搜索结果，请稍候……',
                       'Reading cache and fetching search results…'))

        results = ui.column().classes('w-full').style('align-items: stretch')

        async def load_results():
            try:
                with results:
                    await search_repositories()
            finally:
                _safe_delete(loading)

        ui.timer(0.1, load_results, once=True, immediate=False)


async def search_repositories():
    user_id = require_user()
    if user_id is None:
        return
    token = await get_valid_github_token()
    if not token:
        empty_state(L('需要重新连接 GitHub', 'Reconnect GitHub'),
                    L('登录授权已失效，请重新连接后再试。',
                      'Your authorization expired. Reconnect and try again.'))
        return
    try:
        data = await run.io_bound(load_user_data, user_id)
    except (sqlite3.Error, ValueError):
        notice(L('暂时无法读取推荐数据，请稍后重试。',
                 'Could not load recommendation data. Please try again later.'))
        return
    if current_user_id() != user_id:
        return
    if data is None:
        empty_state(L('还没有同步收藏', 'No synced stars yet'),
                    L('请先连接 GitHub，同步你自己的 Stars。',
                      'Connect GitHub and sync your own Stars first.'))
        return
    keywords = data['keywords']
    if not keywords:
        empty_state(L('兴趣关键词需要生成', 'Interest keywords need to be generated'),
                    L('请分析当前收藏，再回来探索仓库。',
                      'Analyze your current stars, then come back to explore.'),
                    setup=True)
        return
    saved_stars = data['repositories']

    session = DiscoverySession(keywords)
    message = ui.label().classes('muted').props('role="status" aria-live="polite"')
    refresh_browser = repository_browser(session.repos, starred_ids={repo.get('id') for repo in saved_stars},
                                         keywords=keywords, remote=True)
    stale = False
    fetching = False

    def is_current():
        return current_user_id() == user_id and app.storage.user.get('github_token') == token

    def update_button():
        wait = max(0, math.ceil(session.retry_at - time.time()))
        if stale or not is_current():
            more.disable()
            more.set_text(L('请刷新页面后继续', 'Refresh the page to continue'))
        elif fetching:
            more.disable()
            more.set_text(L('正在获取下一批……', 'Fetching the next batch…'))
        elif wait:
            more.disable()
            more.set_text(L(f'{wait} 秒后可继续', f'Available again in {wait} seconds'))
        elif not session.has_more:
            more.disable()
            more.set_text(
                L('当前查询已达到搜索上限', 'This query reached the search limit')
                if session.reached_limit else
                L('当前兴趣的搜索结果已看完', 'You have seen all results for these interests')
            )
        else:
            more.enable()
            label = L(f'继续发现下一批（{len(session.repos)} 个已加载）',
                      f'Continue to the next batch ({len(session.repos)} loaded)')
            more.set_text(
                L(f'重试获取下一批（{len(session.repos)} 个已加载）',
                  f'Retry the next batch ({len(session.repos)} loaded)')
                if session.error else label
            )

    async def fetch_more():
        nonlocal stale, fetching
        if fetching or stale or not is_current() or time.time() < session.retry_at:
            return
        fetching = True
        update_button()
        try:
            latest = await run.io_bound(load_user_data, user_id)
            if not latest or latest['version'] != data['version']:
                stale = True
                message.set_text(L('收藏已更新，请重新分析后刷新探索页。',
                                   'Your stars changed. Analyze again and refresh the Explore page.'))
                return
            before = len(session.repos)
            await session.load_more(user_id, token, is_current=is_current)
            if not is_current():
                return
            count = len(session.repos) - before
            new_batch = session.batch
            if session.error == 'unauthorized':
                clear_github_tokens()
                message.set_text(L('GitHub 授权已失效，请重新连接 GitHub。',
                                   'GitHub authorization expired. Reconnect GitHub.'))
                ui.link(L('重新连接 GitHub', 'Reconnect GitHub'), '/auth/github').classes('text-link')
            elif session.error:
                message.set_text(L(
                    search_error_message(session.error) + '。已加载的仓库会保留，可稍后重试。',
                    search_error_message(session.error) + '. Loaded repositories will be kept; try again later.',
                ))
            elif count:
                if session.has_more:
                    tail = L('还有更多结果，可以继续获取下一批。',
                             'There are more results; continue to the next batch.')
                elif session.reached_limit:
                    tail = L('已达到 GitHub 1000 条上限。',
                             'The GitHub 1,000-result limit has been reached.')
                else:
                    tail = L('当前兴趣的搜索结果已看完。',
                             'You have seen all results for these interests.')
                message.set_text(L(
                    f'本次新增 {count} 个，累计 {len(session.repos)} 个，已定位到第 {new_batch} 批。',
                    f'Added {count}; {len(session.repos)} total. Scrolled to batch {new_batch}.',
                ) + (
                    L('GitHub 提示部分结果不完整。', 'GitHub reported that some results are incomplete.')
                    if session.incomplete else ''
                ) + (
                    L('本次缓存未写入，下次可能需要重新获取。',
                      'Cache was not written this time; the next request may need to fetch again.')
                    if session.cache_warning else ''
                ) + tail)
            else:
                message.set_text(L(
                    f'这一批没有新增仓库（结果可能与其他批次重复），累计 {len(session.repos)} 个。',
                    f'This batch added no new repositories (it may overlap earlier batches); '
                    f'{len(session.repos)} total.',
                ) + (
                    L('还有更多结果，可以继续获取下一批。',
                      'There are more results; continue to the next batch.')
                    if session.has_more else
                    L('当前兴趣的搜索结果已看完。', 'You have seen all results for these interests.')
                ))
            refresh_browser()
            if count and new_batch > 1:
                # 新批次在列表底部；直接定位到批次标题，避免整页平滑滑动。
                ui.timer(0.15, lambda: ui.run_javascript(
                    f'document.getElementById("batch-{new_batch}")?.scrollIntoView({{block: "start"}});'
                ), once=True)
        except (sqlite3.Error, ValueError):
            message.set_text(L('暂时无法读取收藏状态，请稍后重试。',
                               'Could not read star status. Please try again later.'))
        finally:
            fetching = False
            update_button()

    ui.label(L(
        '“继续发现下一批”会按兴趣组轮流获取：先补齐各组第一页，再逐步获取下一页；'
        '新结果作为新批次追加，并定位到新批次标题。',
        '“Continue to the next batch” rotates through interest groups: it fills each group’s first page, '
        'then advances page by page. New results are appended as a new batch and scrolled into view.',
    )).classes('muted')
    more = ui.button(L('继续发现下一批', 'Continue to the next batch'),
                     on_click=fetch_more).props('outline no-caps').classes('load-more')
    ui.timer(1, update_button)
    await fetch_more()


# 只有应用真正启动时才建表，导入模块用于测试不会创建正式数据库。
app.on_startup(init_db)


def run_app() -> None:
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO'),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    secret = os.getenv('STORAGE_SECRET')
    if not secret:
        raise SystemExit('STORAGE_SECRET is required. Copy .env.example to .env and set it.')
    host = os.getenv('HOST', '127.0.0.1')
    port = int(os.getenv('PORT', '8080'))
    reload = os.getenv('RELOAD', '0').lower() in ('1', 'true', 'yes')
    show = os.getenv('SHOW', '0').lower() in ('1', 'true', 'yes')
    logger.info('Starting LookingFor on %s:%s (reload=%s)', host, port, reload)
    ui.run(
        host=host,
        port=port,
        title='LookingFor',
        reload=reload,
        show=show,
        uvicorn_logging_level=os.getenv('UVICORN_LOG_LEVEL', 'info'),
        storage_secret=secret,
    )


if __name__ in {'__main__', '__mp_main__'}:
    run_app()
