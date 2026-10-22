"""页面组件：只负责展示和本地筛选，不调用 GitHub 或模型。

main.py 负责数据流程；这个文件负责用 NiceGUI 摆放元素；styles.css 负责外观。
"""

from pathlib import Path

from nicegui import ui, app

from i18n import L, current_language, set_language

ASSETS = Path(__file__).resolve().parent


def _switch_language(language: str) -> None:
    set_language(language)
    ui.navigate.reload()


def language_switch():
    active = current_language()
    with ui.row().classes('language-switch').props('aria-label="Language"'):
        for code, label in (('zh', '中文'), ('en', 'EN')):
            button = ui.button(label, on_click=lambda c=code: _switch_language(c)).props(
                'flat dense no-caps').classes('lang-button')
            if code == active:
                button.classes('is-active')


def account_area():
    """把当前浏览器的账号资料绑定到导航栏，登录回调更新资料后也会自动更新。"""
    storage = app.storage.user
    storage.setdefault('github_user', {})
    ui.link(L('连接 GitHub', 'Connect GitHub'), '/auth/github').classes(
        'button-link header-connect').bind_visibility_from(
        storage, 'github_user', backward=lambda user: not bool(user.get('login')))

    with ui.row().classes('account-area').bind_visibility_from(
            storage, 'github_user', backward=lambda user: bool(user.get('login'))):
        with ui.link(target='/stars').classes('account-link'):
            with ui.avatar(size='38px', color='secondary').classes('account-avatar'):
                avatar = ui.image().props(
                    f'alt="{L("GitHub 用户头像", "GitHub avatar")}" no-spinner no-transition').bind_source_from(
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
        ui.link(L('退出', 'Sign out'), '/auth/logout').classes('logout-link')


def page_shell(title: str, active: str = ''):
    """建立导航和页脚，返回中间的内容区域，配合 with 使用。"""
    ui.page_title(f'{title} · LookingFor')
    ui.colors(primary='#48634b', secondary='#797c62', accent='#a46743')
    ui.add_css((ASSETS / 'styles.css').read_text(encoding='utf-8'))

    ui.link(L('跳到主要内容', 'Skip to main content'), '#main-content').classes('skip-link')
    with ui.element('header').classes('site-header'):
        with ui.row().classes('header-inner'):
            with ui.link(target='/').classes('brand').props(
                    f'aria-label="{L("LookingFor 首页", "LookingFor home")}"'):
                ui.icon('eco').classes('brand-icon')
                ui.label('LookingFor')
            with ui.element('nav').classes('site-nav').props(
                    f'aria-label="{L("主导航", "Main navigation")}"'):
                for label, target, key in [
                    (L('发现', 'Discover'), '/', 'home'),
                    (L('探索仓库', 'Explore'), '/search', 'search'),
                    (L('我的 Stars', 'My Stars'), '/stars', 'stars'),
                ]:
                    link = ui.link(label, target).classes('nav-link')
                    if active == key:
                        link.classes('is-active').props('aria-current="page"')
            with ui.row().classes('header-actions'):
                language_switch()
                account_area()

    content = ui.element('main').classes('page-main').props('id="main-content"')
    with ui.element('footer').classes('site-footer'):
        ui.label('LookingFor').classes('footer-brand')
        ui.label(L('保持好奇，持续发现。', 'Stay curious. Keep discovering.')).classes('footer-note')
        ui.link(L('隐私与数据', 'Privacy & data'), '/privacy').classes('footer-link')
        ui.label(L('让每一次好奇，都有新的去处。',
                   'Every curiosity deserves somewhere to go.')).classes('footer-chinese')
    return content


def page_heading(eyebrow: str, title: str, description: str):
    with ui.column().classes('page-heading'):
        ui.label(eyebrow).classes('eyebrow')
        ui.label(title).classes('page-title').props('role="heading" aria-level="1"')
        ui.label(description).classes('muted intro-copy')


def home_page(star_count: int | None):
    with ui.element('section').classes('hero'):
        with ui.column().classes('hero-copy'):
            ui.label(L('跟随你的好奇心', 'FOLLOW YOUR CURIOSITY')).classes('eyebrow')
            with ui.element('h1').classes('hero-title'):
                ui.label(L('你在寻找', 'What are you'))
                ui.label(L('什么？', 'looking for?')).classes('hero-title-accent')
            ui.label(L('让好奇心，找到下一站。', 'Let curiosity find its next stop.')).classes('hero-subtitle')
            ui.label(L(
                '从你 Star 过的项目出发，发现合拍的开源仓库。'
                '遇见新灵感，也重逢那些差点被忘记的好项目。',
                'Start from the projects you have starred and discover open-source repositories '
                'that fit your interests. Find new inspiration, and revisit good projects you almost forgot.',
            )).classes('hero-description')
            with ui.row().classes('hero-actions'):
                with ui.link(target='/search').classes('button-link primary-link'):
                    ui.label(L('开始探索', 'Start exploring'))
                    ui.icon('arrow_forward', size='19px')
                ui.link(L('查看我的 Stars', 'View my Stars'), '/stars').classes('text-link')
            with ui.row().classes('hero-note'):
                ui.icon('bookmark_border', size='17px')
                if star_count is not None:
                    ui.label(L(
                        f'已同步 {star_count:,} 个 Star，喜欢过的也值得再发现。',
                        f'{star_count:,} stars synced. Old favorites are worth another look.',
                    ))
                else:
                    ui.label(L('连接 GitHub，从你的收藏开始。',
                               'Connect GitHub to start from your collection.'))

        with ui.element('div').classes('hero-art').props('aria-hidden="true"'):
            ui.label(L('为新的发现，留点空间。', 'ROOM FOR SOMETHING NEW')).classes('art-caption')
            ui.image(ASSETS / 'assets' / 'seedling.svg').props('no-spinner no-transition alt=""').classes('seedling')
            with ui.row().classes('art-tag tag-top'):
                ui.icon('code', size='18px')
                ui.label(L('一点灵感', 'a little inspiration'))
            with ui.row().classes('art-tag tag-bottom'):
                ui.icon('auto_awesome', size='17px')
                ui.label(L('你的下一个发现', 'your next discovery'))
            ui.label(L('好奇会生长出好东西。', 'Good things grow from curiosity.')).classes('art-bottom')

    with ui.element('section').classes('how-section'):
        with ui.row().classes('section-topline'):
            ui.label(L('从喜欢，到发现', 'From favorites to discoveries')).classes(
                'section-title').props('role="heading" aria-level="2"')
            ui.label(L('一条通往惊喜的小径', 'A SMALL TRAIL TO SOMETHING GOOD')).classes('eyebrow small-eyebrow')
        with ui.element('div').classes('step-grid'):
            steps = [
                ('01', L('收藏是起点', 'Favorites are the starting point'),
                 L('连接 GitHub，带上你已经喜欢的项目。', 'Connect GitHub and bring the projects you already love.'),
                 'bookmark_border', '/auth/github'),
                ('02', L('兴趣有迹可循', 'Interests leave traces'),
                 L('从项目描述、语言和话题中，梳理你的兴趣。',
                   'Make sense of your interests through descriptions, languages, and topics.'),
                 'interests', '/stars'),
                ('03', L('继续向外探索', 'Keep exploring outward'),
                 L('循着兴趣寻找仓库，留下让你想点开的那一个。',
                   'Follow your interests to repositories worth opening.'),
                 'explore', '/search'),
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
            label, target = L('准备分析', 'Prepare analysis'), '/analyse'
        elif app.storage.user.get('github_user', {}).get('login'):
            label, target = L('重新同步 Stars', 'Resync Stars'), '/auth/github'
        else:
            label, target = L('连接 GitHub', 'Connect GitHub'), '/auth/github'
        ui.link(label, target).classes('button-link primary-link')
        if setup:
            with ui.expansion(L('如何准备推荐数据', 'How to prepare recommendations'),
                              icon='help_outline').classes('setup-details'):
                ui.label(L('1. 连接 GitHub，完成 Star 同步。',
                           '1. Connect GitHub and finish syncing your Stars.'))
                ui.label(L('2. 同步后会自动分析。分析失败时，通过「准备分析」按钮重试。',
                           '2. Analysis runs after syncing. If it fails, retry with “Prepare analysis”.'))
                ui.label(L('3. 完成后回到「探索仓库」页面。',
                           '3. Return to the Explore page when it is done.'))
                ui.link(L('重新打开探索页', 'Reopen Explore'), '/search').classes('text-link')


def notice(message: str):
    with ui.row().classes('notice').props('role="status"'):
        ui.icon('info_outline', size='20px')
        ui.label(message)


def repo_card(repo: dict, starred_ids: set[int], *, is_new: bool = False):
    """一张仓库卡片。外部文字交给 label/link 渲染，不拼接 HTML。"""
    full_name = repo.get('full_name') or L('未命名仓库', 'Unnamed repository')
    owner, _, name = full_name.partition('/')
    repo_url = repo.get('html_url')
    valid_url = isinstance(repo_url, str) and repo_url.startswith('https://github.com/')
    with ui.element('article').classes('repo-card'):
        with ui.row().classes('repo-top'):
            ui.icon('bookmarks' if repo.get('id') in starred_ids else 'book', size='21px').classes('repo-icon')
            with ui.row().classes('repo-badges'):
                if repo.get('id') in starred_ids:
                    ui.label(L('已 Star', 'Starred')).classes('starred-badge')
                if is_new:
                    ui.label(L('新发现', 'New')).classes('new-badge')
                if repo.get('id') not in starred_ids and not is_new:
                    ui.label(L('开源仓库', 'Open source')).classes('repo-kind')
        ui.label(owner if name else 'GitHub').classes('repo-owner')
        if valid_url:
            ui.link(name or full_name, repo_url, new_tab=True).classes('repo-name').props('rel="noopener noreferrer"')
        else:
            ui.label(name or full_name).classes('repo-name')
        description = repo.get('description') or L(
            '这个项目还没有简介，去仓库里看看吧。',
            'This project has no description yet. Visit the repository to learn more.',
        )
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
                ui.label(repo.get('language') or L('未标注语言', 'Language not specified'))
            if valid_url:
                with ui.link(target=repo_url, new_tab=True).classes('repo-open').props('rel="noopener noreferrer"'):
                    ui.label(L('打开仓库', 'Open repository'))
                    ui.icon('north_east', size='16px')
        matched = repo.get('matched_keywords') or []
        if matched:
            ui.label(L('关键词组 · ', 'Matched keywords · ') + ' / '.join(matched)).classes(
                'match-note').tooltip(L(
                    '这个仓库出现在这组关键词的搜索结果中。',
                    'This repository appeared in search results for this keyword group.',
                ))


def repository_browser(repos: list[dict], *, starred_ids: set[int] | None = None,
                       keywords: list[str] | None = None, remote: bool = False):
    """只筛选传入的列表；不会重新搜索，不改变后端排序，也不排除已 Star 项目。"""
    starred_ids = starred_ids or set()
    all_languages = L('全部语言', 'All languages')
    state = {'query': '', 'language': all_languages, 'limit': 12}
    languages = [all_languages] + sorted({repo['language'] for repo in repos if repo.get('language')})

    if keywords:
        with ui.expansion(L('这次探索的兴趣关键词', 'Interest keywords for this exploration'),
                          icon='tune').classes('keyword-details'):
            with ui.row().classes('topic-list'):
                for keyword in keywords:
                    ui.label(keyword).classes('topic-chip')
            ui.label(L(
                '关键词按每组最多 4 个合并搜索；结果缓存在 Redis 6 小时，已缓存的组不会重复请求 GitHub。',
                'Keywords are searched in groups of up to 4; results are cached in Redis for 6 hours, '
                'so cached groups do not hit GitHub again.',
            )).classes('muted')

    def set_query(event):
        state['query'] = (event.value or '').strip().casefold()
        state['limit'] = 12
        results.refresh()

    def set_language_filter(event):
        state['language'] = event.value
        state['limit'] = 12
        results.refresh()

    def show_more():
        state['limit'] += 12
        results.refresh()

    def reset_filters():
        query_input.set_value('')
        language_input.set_value(all_languages)

    with ui.row().classes('browse-toolbar'):
        with ui.input(placeholder=L('搜索名称、简介或话题', 'Search name, description, or topic'),
                      on_change=set_query).props(
            f'outlined clearable debounce=250 aria-label="{L("筛选仓库", "Filter repositories")}"'
        ).classes('repo-search') as query_input:
            with query_input.add_slot('prepend'):
                ui.icon('search', size='22px')
        language_input = ui.select(
            languages, value=all_languages, on_change=set_language_filter,
        ).props(
            f'outlined aria-label="{L("筛选编程语言", "Filter programming language")}"'
        ).classes('language-select')

    @ui.refreshable
    def results():
        filtered = []
        for repo in repos:
            searchable = ' '.join([repo.get('full_name') or '', repo.get('description') or '',
                                   ' '.join(repo.get('topics') or [])]).casefold()
            if state['query'] not in searchable:
                continue
            if state['language'] != all_languages and repo.get('language') != state['language']:
                continue
            filtered.append(repo)

        with ui.row().classes('results-summary').props('role="status" aria-live="polite"'):
            ui.label(L(f'{len(filtered):,} 个仓库', f'{len(filtered):,} repositories')).classes('result-count')
            ui.label(
                L('结果按批次显示；最新一批的卡片会标记“新发现”。',
                  'Results are grouped by batch; the newest batch is marked “New”.')
                if remote else
                L('收藏过的，也可能带来新灵感。', 'Starred projects can still spark new ideas.')
            ).classes('muted')
        if not filtered:
            with ui.column().classes('filter-empty'):
                ui.icon('search_off', size='32px')
                ui.label(L('这次没有找到匹配的仓库', 'No matching repositories')).classes('section-title')
                ui.label(L('试试更短的词，或切换到其他语言。',
                           'Try a shorter query or switch to another language.')).classes('muted')
                ui.button(L('清除筛选', 'Clear filters'), on_click=reset_filters).props('outline no-caps')
            return

        if remote:
            grouped: dict[int, list[dict]] = {}
            for repo in filtered:
                batch = repo.get('_batch')
                grouped.setdefault(batch if isinstance(batch, int) and batch > 0 else 1, []).append(repo)
            latest = max(grouped)

            for batch in sorted(grouped):
                repos_in_batch = sorted(
                    grouped[batch],
                    key=lambda repo: (
                        len(repo.get('matched_keywords') or []),
                        repo.get('stargazers_count') or 0,
                    ),
                    reverse=True,
                )
                if latest == 1:
                    title = L('初始推荐', 'Initial recommendations')
                elif batch == latest:
                    title = L(f'第 {batch} 批 · 本次新增', f'Batch {batch} · New this round')
                else:
                    title = L(f'第 {batch} 批 · 较早发现', f'Batch {batch} · Found earlier')

                with ui.row().classes('batch-header').props(f'id="batch-{batch}"'):
                    ui.label(title).classes('batch-title')
                    ui.label(L(f'{len(repos_in_batch)} 个仓库',
                               f'{len(repos_in_batch)} repositories')).classes('batch-meta')
                with ui.element('div').classes('repo-grid'):
                    for repo in repos_in_batch:
                        repo_card(repo, starred_ids, is_new=(latest > 1 and batch == latest))

            with ui.column().classes('browse-bottom'):
                ui.label(L(f'已加载 {len(filtered)} 个仓库，全部显示。',
                           f'{len(filtered)} repositories loaded. Showing all.')).classes('muted')
                ui.label(L('需要更多时，点击下方的“继续发现下一批”。',
                           'Need more? Use “Continue to the next batch” below.')).classes('end-note')
        else:
            with ui.element('div').classes('repo-grid'):
                for repo in filtered[:state['limit']]:
                    repo_card(repo, starred_ids)
            with ui.column().classes('browse-bottom'):
                ui.label(L(f'已显示 {min(state["limit"], len(filtered))} / {len(filtered)} 个仓库',
                           f'Showing {min(state["limit"], len(filtered))} / {len(filtered)} repositories')
                         ).classes('muted')
                if state['limit'] < len(filtered):
                    ui.button(L('显示更多已加载的仓库', 'Show more loaded repositories'),
                              icon='expand_more', on_click=show_more).props(
                        'outline no-caps').classes('load-more')
                else:
                    ui.label(L('这批仓库看完了，去看看让你好奇的那一个吧。',
                               'You have seen this batch. Open the one that sparks your curiosity.')).classes('end-note')

    results()

    def refresh():
        languages = [all_languages] + sorted({repo['language'] for repo in repos if repo.get('language')})
        language_input.options = languages
        language_input.update()
        results.refresh()

    return refresh
