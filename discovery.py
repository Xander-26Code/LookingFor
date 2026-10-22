"""一次探索的分页进度。只有某页成功获取后才前进，失败可以原页重试。"""
import time
from dataclasses import dataclass

from search import SearchError, build_query, build_query_groups, search_one


@dataclass
class QueryProgress:
    keywords: list[str]
    page: int = 1
    done: bool = False
    limited: bool = False


class DiscoverySession:
    def __init__(self, keywords: list[str]):
        self.groups = [QueryProgress(group) for group in build_query_groups(keywords)]
        self.repos: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.loading = False
        self.retry_at = 0.0
        self.error = ''
        self.incomplete = False
        self.cache_warning = False
        self.batch = 0

    @property
    def has_more(self):
        return any(not group.done for group in self.groups)

    @property
    def reached_limit(self):
        return any(group.limited for group in self.groups)

    async def load_more(self, user_id, token, *, budget=4, is_current=lambda: True):
        if self.loading or time.time() < self.retry_at:
            return
        self.loading = True
        self.error = ''
        self.incomplete = False
        self.cache_warning = False
        new_batch = self.batch + 1
        added = 0
        try:
            for _ in range(budget):
                active = [(index, group) for index, group in enumerate(self.groups) if not group.done]
                if not active or not is_current():
                    return
                # 先覆盖各兴趣的第一页，再逐步深入，避免一个兴趣占满结果。
                _, group = min(active, key=lambda pair: (pair[1].page, pair[0]))
                try:
                    result = await search_one(build_query(group.keywords), user_id, token, page=group.page)
                except SearchError as exc:
                    if exc.code == 'page_limit':
                        # GitHub 分页到头：这组结束，不能让 UI 永远重试同一页。
                        group.done = True
                        group.limited = True
                        self.error = exc.code
                        continue
                    self.error = exc.code
                    self.retry_at = time.time() + (max(1, exc.wait) if exc.code == 'rate_limit' else 2)
                    return
                if not is_current() or result is None:
                    return
                if result.source in ('rate_limited', 'busy'):
                    self.error = 'rate_limit' if result.source == 'rate_limited' else 'busy'
                    self.retry_at = time.time() + max(1, result.wait)
                    return
                data = result.data
                if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                    self.error = 'unexpected'
                    return
                for repo in data['items']:
                    if not isinstance(repo, dict) or type(repo.get('id')) is not int:
                        continue
                    if repo['id'] not in self.by_id:
                        copy = {**repo, 'matched_keywords': [], '_batch': new_batch}
                        self.by_id[repo['id']] = copy
                        self.repos.append(copy)
                        added += 1
                    matched = self.by_id[repo['id']]['matched_keywords']
                    for keyword in group.keywords:
                        if keyword not in matched:
                            matched.append(keyword)
                group.done = not data['has_more']
                group.limited = data['limit_reached']
                group.page += 1
                self.incomplete |= data['incomplete_results']
                self.cache_warning |= result.source == 'fetched_uncached'
        finally:
            if added:
                self.batch = new_batch
            self.loading = False
