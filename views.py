"""页面组件：只负责展示和本地筛选，不调用 GitHub 或模型。

main.py 负责数据流程；这个文件负责用 NiceGUI 摆放元素；styles.css 负责外观。
"""

from pathlib import Path

from nicegui import ui, app

ASSETS = Path(__file__).resolve().parent


def account_area():
    """把当前浏览器的账号资料绑定到导航栏，登录回调更新资料后也会自动更新。"""
    storage = app.storage.user
    storage.setdefault('github_user', {})
    ui.link('连接 GitHub', '/auth/github').classes('button-link header-connect').bind_visibility_from(
        storage, 'github_user', backward=lambda user: not bool(user.get('login')))

    with ui.link(target='/stars').classes('account-link').bind_visibility_from(
            storage, 'github_user', backward=lambda user: bool(user.get('login'))):
        with ui.avatar(size='38px', color='secondary').classes('account-avatar'):
            avatar = ui.image().props('alt="GitHub 用户头像" no-spinner no-transition').bind_source_from(
                storage, 'github_user', backward=lambda user: user.get('avatar_url') or '')
            avatar.bind_visibility_from(storage, 'github_user', backward=lambda user: bool(user.get('avatar_url')))
            # URL 缺失或图片加载失败时都显示默认人像。
            with avatar.add_slot('error'):
                ui.icon('person', size='26px')
            ui.icon('person', size='26px').bind_visibility_from(
                storage, 'github_user', backward=lambda user: not bool(user.get('avatar_url')))
        with ui.column().classes('account-info'):
            ui.label().classes('account-name').bind_text_from(
                storage, 'github_user', backward=lambda user: user.get('name') or user.get('login', ''))
            ui.label().classes('account-login').bind_text_from(
                storage, 'github_user', backward=lambda user: '@' + user.get('login', ''))
        ui.icon('arrow_forward', size='17px').classes('account-arrow')


def page_shell(title: str, active: str = ''):
    """建立导航和页脚，返回中间的内容区域，配合 with 使用。"""
    ui.page_title(f'{title} · LookingFor')
    ui.colors(primary='#48634b', secondary='#797c62', accent='#a46743')
    ui.add_css((ASSETS / 'styles.css').read_text(encoding='utf-8'))

    ui.link('跳到主要内容', '#main-content').classes('skip-link')
    with ui.element('header').classes('site-header'):
        with ui.row().classes('header-inner'):
            with ui.link(target='/').classes('brand').props('aria-label="LookingFor 首页"'):
                ui.icon('eco').classes('brand-icon')
                ui.label('LookingFor')
            with ui.element('nav').classes('site-nav').props('aria-label="主导航"'):
                for label, target, key in [('发现', '/', 'home'), ('探索仓库', '/search', 'search'),
                                           ('我的 Stars', '/stars', 'stars')]:
                    link = ui.link(label, target).classes('nav-link')
                    if active == key:
                        link.classes('is-active').props('aria-current="page"')
            account_area()

    content = ui.element('main').classes('page-main').props('id="main-content"')
    with ui.element('footer').classes('site-footer'):
        ui.label('LookingFor').classes('footer-brand')
        ui.label('Stay curious. Keep discovering.').classes('footer-note')
        ui.label('让每一次好奇，都有新的去处。').classes('footer-chinese')
    return content


def page_heading(eyebrow: str, title: str, description: str):
    with ui.column().classes('page-heading'):
        ui.label(eyebrow).classes('eyebrow')
        ui.label(title).classes('page-title').props('role="heading" aria-level="1"')
        ui.label(description).classes('muted intro-copy')


def home_page(star_count: int | None):
    with ui.element('section').classes('hero'):
        with ui.column().classes('hero-copy'):
            ui.label('FOLLOW YOUR CURIOSITY').classes('eyebrow')
            with ui.element('h1').classes('hero-title'):
                ui.label('What are you')
                ui.label('looking for?').classes('hero-title-accent')
            ui.label('让好奇心，找到下一站。').classes('hero-subtitle')
            ui.label('从你 Star 过的项目出发，发现合拍的开源仓库。'
                     '遇见新灵感，也重逢那些差点被忘记的好项目。').classes('hero-description')
            with ui.row().classes('hero-actions'):
                with ui.link(target='/search').classes('button-link primary-link'):
                    ui.label('开始探索')
                    ui.icon('arrow_forward', size='19px')
                ui.link('查看我的 Stars', '/stars').classes('text-link')
            with ui.row().classes('hero-note'):
                ui.icon('bookmark_border', size='17px')
                ui.label(f'已同步 {star_count:,} 个 Star，喜欢过的也值得再发现。'
                         if star_count is not None else '连接 GitHub，从你的收藏开始。')

        with ui.element('div').classes('hero-art').props('aria-hidden="true"'):
            ui.label('ROOM FOR SOMETHING NEW').classes('art-caption')
            ui.image(ASSETS / 'assets' / 'seedling.svg').props('no-spinner no-transition alt=""').classes('seedling')
            with ui.row().classes('art-tag tag-top'):
                ui.icon('code', size='18px')
                ui.label('a little inspiration')
            with ui.row().classes('art-tag tag-bottom'):
                ui.icon('auto_awesome', size='17px')
                ui.label('your next discovery')
            ui.label('Good things grow from curiosity.').classes('art-bottom')

    with ui.element('section').classes('how-section'):
        with ui.row().classes('section-topline'):
            ui.label('从喜欢，到发现').classes('section-title').props('role="heading" aria-level="2"')
            ui.label('A SMALL TRAIL TO SOMETHING GOOD').classes('eyebrow small-eyebrow')
        with ui.element('div').classes('step-grid'):
            steps = [
                ('01', '收藏是起点', '连接 GitHub，带上你已经喜欢的项目。', 'bookmark_border', '/auth/github'),
                ('02', '兴趣有迹可循', '从项目描述、语言和话题中，梳理你的兴趣。', 'interests', '/stars'),
                ('03', '继续向外探索', '循着兴趣寻找仓库，留下让你想点开的那一个。', 'explore', '/search'),
            ]
            for number, title, description, icon, target in steps:
                with ui.link(target=target).classes('step-card'):
                    with ui.row().classes('step-top'):
                        ui.icon(icon, size='22px')
                        ui.label(number).classes('step-number')
                    ui.label(title).classes('step-title')
                    ui.label(description).classes('muted')


def empty_state(title: str, description: str, *, setup: bool = False):
    with ui.column().classes('empty-state'):
        ui.icon('travel_explore', size='34px').classes('empty-icon')
        ui.label(title).classes('section-title')
        ui.label(description).classes('muted empty-description')
        if setup:
            label, target = '准备分析', '/analyse'
        elif app.storage.user.get('github_user', {}).get('login'):
            label, target = '重新同步 Stars', '/auth/github'
        else:
            label, target = '连接 GitHub', '/auth/github'
        ui.link(label, target).classes('button-link primary-link')
        if setup:
            with ui.expansion('如何准备推荐数据', icon='help_outline').classes('setup-details'):
                ui.label('1. 连接 GitHub，完成 Star 同步。')
                ui.label('2. 同步后会自动分析。分析失败时，通过「准备分析」按钮重试。')
                ui.label('3. 完成后回到「探索仓库」页面。')
                ui.link('重新打开探索页', '/search').classes('text-link')


def notice(message: str):
    with ui.row().classes('notice').props('role="status"'):
        ui.icon('info_outline', size='20px')
        ui.label(message)


def repo_card(repo: dict, starred_ids: set[int]):
    """一张仓库卡片。外部文字交给 label/link 渲染，不拼接 HTML。"""
    full_name = repo.get('full_name') or 'Unnamed repository'
    owner, _, name = full_name.partition('/')
    repo_url = repo.get('html_url')
    valid_url = isinstance(repo_url, str) and repo_url.startswith('https://github.com/')
    with ui.element('article').classes('repo-card'):
        with ui.row().classes('repo-top'):
            ui.icon('bookmarks' if repo.get('id') in starred_ids else 'book', size='21px').classes('repo-icon')
            if repo.get('id') in starred_ids:
                ui.label('已 Star').classes('starred-badge')
            else:
                ui.label('开源仓库').classes('repo-kind')
        ui.label(owner if name else 'GitHub').classes('repo-owner')
        if valid_url:
            ui.link(name or full_name, repo_url, new_tab=True).classes('repo-name').props('rel="noopener noreferrer"')
        else:
            ui.label(name or full_name).classes('repo-name')
        description = repo.get('description') or '这个项目还没有简介，去仓库里看看吧。'
        ui.label(description).classes('repo-description').tooltip(description)
        with ui.row().classes('topic-list'):
            for topic in (repo.get('topics') or [])[:3]:
                ui.label(topic).classes('topic-chip')
        with ui.row().classes('repo-meta'):
            with ui.row().classes('meta-item'):
                ui.icon('star_border', size='18px')
                stars = repo.get('stargazers_count')
                ui.label(f'{stars:,}' if isinstance(stars, int) and stars >= 0 else '—')
            with ui.row().classes('meta-item'):
                ui.element('span').classes('language-dot')
                ui.label(repo.get('language') or '未标注语言')
            if valid_url:
                with ui.link(target=repo_url, new_tab=True).classes('repo-open').props('rel="noopener noreferrer"'):
                    ui.label('打开仓库')
                    ui.icon('north_east', size='16px')
        matched = repo.get('matched_keywords') or []
        if matched:
            ui.label('关键词组 · ' + ' / '.join(matched)).classes('match-note').tooltip('这个仓库出现在这组关键词的搜索结果中。')


def repository_browser(repos: list[dict], *, starred_ids: set[int] | None = None,
                       keywords: list[str] | None = None):
    """只筛选传入的列表；不会重新搜索，不改变后端排序，也不排除已 Star 项目。"""
    starred_ids = starred_ids or set()
    state = {'query': '', 'language': '全部语言', 'limit': 12}
    languages = ['全部语言'] + sorted({repo['language'] for repo in repos if repo.get('language')})

    if keywords:
        with ui.expansion('这次探索的兴趣关键词', icon='tune').classes('keyword-details'):
            with ui.row().classes('topic-list'):
                for keyword in keywords:
                    ui.label(keyword).classes('topic-chip')
            ui.label('关键词按每组最多 4 个合并搜索；结果缓存在 Redis 6 小时，已缓存的组不会重复请求 GitHub。').classes('muted')

    def set_query(event):
        state['query'] = (event.value or '').strip().casefold()
        state['limit'] = 12
        results.refresh()

    def set_language(event):
        state['language'] = event.value
        state['limit'] = 12
        results.refresh()

    def show_more():
        state['limit'] += 12
        results.refresh()

    def reset_filters():
        query_input.set_value('')
        language_input.set_value('全部语言')

    with ui.row().classes('browse-toolbar'):
        with ui.input(placeholder='搜索名称、简介或话题', on_change=set_query).props(
            'outlined clearable debounce=250 aria-label="筛选仓库"').classes('repo-search') as query_input:
            with query_input.add_slot('prepend'):
                ui.icon('search', size='22px')
        language_input = ui.select(languages, value='全部语言', on_change=set_language).props(
            'outlined aria-label="筛选编程语言"').classes('language-select')

    @ui.refreshable
    def results():
        filtered = []
        for repo in repos:
            searchable = ' '.join([repo.get('full_name') or '', repo.get('description') or '',
                                   ' '.join(repo.get('topics') or [])]).casefold()
            if state['query'] not in searchable:
                continue
            if state['language'] != '全部语言' and repo.get('language') != state['language']:
                continue
            filtered.append(repo)

        with ui.row().classes('results-summary').props('role="status" aria-live="polite"'):
            ui.label(f'{len(filtered):,} 个仓库').classes('result-count')
            ui.label('收藏过的，也可能带来新灵感。').classes('muted')
        if not filtered:
            with ui.column().classes('filter-empty'):
                ui.icon('search_off', size='32px')
                ui.label('这次没有找到匹配的仓库').classes('section-title')
                ui.label('试试更短的词，或切换到其他语言。').classes('muted')
                ui.button('清除筛选', on_click=reset_filters).props('outline no-caps')
            return
        with ui.element('div').classes('repo-grid'):
            for repo in filtered[:state['limit']]:
                repo_card(repo, starred_ids)
        with ui.column().classes('browse-bottom'):
            ui.label(f'已显示 {min(state["limit"], len(filtered))} / {len(filtered)} 个仓库').classes('muted')
            if state['limit'] < len(filtered):
                ui.button('再看看更多', icon='expand_more', on_click=show_more).props('outline no-caps').classes('load-more')
            else:
                ui.label('这批仓库看完了，去看看让你好奇的那一个吧。').classes('end-note')

    results()
