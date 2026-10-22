"""GitHub 仓库搜索：底层请求 + Redis 保护层。"""

import math
import time
from email.utils import parsedate_to_datetime

import requests
from nicegui import run

from search_guard import API_VERSION, GuardResult, SearchError, get_or_fetch

SEARCH_URL = "https://api.github.com/search/repositories"
REQUEST_TIMEOUT = 15
PER_PAGE = 10
DEFAULT_BATCH_SIZE = 4


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
        if keyword and len(keyword) <= 100 and folded not in seen:
            seen.add(folded)
            unique.append(keyword)

    groups, current = [], []
    for keyword in unique:
        if current and (len(current) >= batch_size or sum(map(len, current)) + len(keyword) > 200):
            groups.append(current)
            current = []
        current.append(keyword)
    if current:
        groups.append(current)
    return groups


def build_query(group: list[str]) -> str:
    """把一组关键词拼成 GitHub 查询串。"""
    return " OR ".join(f'"{keyword}"' for keyword in group) + ' is:public'


def retry_wait(headers) -> float:
    waits = []
    retry = headers.get('Retry-After')
    if retry:
        try:
            waits.append(float(retry))
        except ValueError:
            try:
                waits.append(parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    if headers.get('X-RateLimit-Remaining') == '0':
        try:
            waits.append(float(headers.get('X-RateLimit-Reset', 0)) - time.time())
        except ValueError:
            pass
    return max(1, math.ceil(max(waits))) if waits else 60


def rate_limit_remaining(headers) -> int | None:
    try:
        return int(headers.get('X-RateLimit-Remaining'))
    except (TypeError, ValueError):
        return None


def rate_limit_reset_after(headers) -> float:
    try:
        reset = float(headers.get('X-RateLimit-Reset', 0))
    except (TypeError, ValueError):
        return 0
    return max(0, math.ceil(reset - time.time()))


def fetch_repositories(query: str, token: str | None = None, page: int = 1) -> dict:
    """同步调用 GitHub Search，返回 {"items": [...], "incomplete_results": bool}。

    不创建任何 UI；失败时抛 SearchError。
    """
    if not 1 <= page <= 100:
        raise SearchError('page_limit')
    if not query.endswith(' is:public'):
        raise SearchError('unexpected')
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
                "page": page,
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
    if response.status_code == 429 or (response.status_code == 403 and (
        response.headers.get('X-RateLimit-Remaining') == '0'
        or response.headers.get('Retry-After')
        or 'rate limit' in response.text.lower()
    )):
        raise SearchError('rate_limit', wait=retry_wait(response.headers))
    if response.status_code != 200:
        raise SearchError(f"http_{response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("json") from exc

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SearchError("unexpected")

    total = data.get('total_count')
    if type(total) is not int or total < 0:
        raise SearchError('unexpected')
    items = [
        item
        for item in data["items"]
        if isinstance(item, dict) and type(item.get("id")) is int
        and item.get('private') is False and item.get('visibility', 'public') == 'public' 
    ]
    return {
        "items": items,
        "incomplete_results": bool(data.get("incomplete_results")),
        "has_more": page * PER_PAGE < min(total, 1000),
        "limit_reached": total > 1000 and page * PER_PAGE >= 1000,
        "_rate_remaining": rate_limit_remaining(response.headers),
        "_rate_reset_after": rate_limit_reset_after(response.headers),
    }


async def search_one(
    query: str,
    user_id: int | str,
    token: str | None = None,
    page: int = 1,
) -> GuardResult | None:
    """异步入口：把 get_or_fetch 放进工作线程，避免阻塞 NiceGUI。"""
    return await run.io_bound(
        get_or_fetch,
        query,
        lambda: fetch_repositories(query, token, page),
        user_id,
        page=page, per_page=PER_PAGE,
    )

