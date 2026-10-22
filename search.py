"""GitHub 仓库搜索：底层请求 + Redis 保护层。"""

import requests
from nicegui import run

from search_guard import GuardResult, get_cached, get_or_fetch

SEARCH_URL = "https://api.github.com/search/repositories"
API_VERSION = "2026-03-10"
REQUEST_TIMEOUT = 15
PER_PAGE = 10
DEFAULT_BATCH_SIZE = 4


class SearchError(Exception):
    """带错误码的搜索异常，页面可以根据 code 显示中文提示。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def build_query_groups(
    keywords: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[str]]:
    """去重后，每 4 个关键词分成一组。"""
    batch_size = max(1, min(batch_size, 6))
    unique = []
    seen = set()

    for keyword in keywords:
        if not isinstance(keyword, str):
            continue
        keyword = " ".join(keyword.replace('"', "").split())
        folded = keyword.casefold()
        if keyword and folded not in seen:
            seen.add(folded)
            unique.append(keyword)

    return [unique[i:i + batch_size] for i in range(0, len(unique), batch_size)]


def build_query(group: list[str]) -> str:
    """把一组关键词拼成 GitHub 查询串。"""
    return " OR ".join(f'"{keyword}"' for keyword in group)


def fetch_repositories(query: str, token: str | None = None) -> dict:
    """同步调用 GitHub Search，返回 {"items": [...], "incomplete_results": bool}。

    不创建任何 UI；失败时抛 SearchError。
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            SEARCH_URL,
            headers=headers,
            params={
                "q": query,
                "per_page": PER_PAGE,
                "page": 1,
                "sort": "stars",
                "order": "desc",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout as exc:
        raise SearchError("timeout") from exc
    except requests.exceptions.RequestException as exc:
        raise SearchError("network") from exc

    if response.status_code == 401:
        raise SearchError("unauthorized")
    if response.status_code in (403, 429):
        raise SearchError("rate_limit")
    if response.status_code != 200:
        raise SearchError(f"http_{response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("json") from exc

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SearchError("unexpected")

    items = [
        item
        for item in data["items"]
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    ]
    return {
        "items": items,
        "incomplete_results": bool(data.get("incomplete_results")),
    }


async def search_one(
    query: str,
    user_id: int | str,
    token: str | None = None,
) -> GuardResult | None:
    """异步入口：把 get_or_fetch 放进工作线程，避免阻塞 NiceGUI。"""
    return await run.io_bound(
        get_or_fetch,
        query,
        lambda: fetch_repositories(query, token),
        user_id,
    )


def get_cached_result(query: str) -> dict | None:
    """只读缓存，不发请求；main.py 用它先加载已有结果。"""
    return get_cached(query)
