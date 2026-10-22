"""搜索保护层：Redis 缓存 + 单飞锁 + 令牌桶限流。"""

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import redis

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

SEARCH_CACHE_TTL = 6 * 60 * 60  # 6 小时
LOCK_TTL = 10                   # 锁最多存在 10 秒
WAIT_TIMEOUT = 5.0              # 等待别人写缓存的最长时间
WAIT_INTERVAL = 0.05            # 每 50ms 查一次缓存

RATE_LIMIT_CAPACITY = 30        # 桶容量：30 次
RATE_LIMIT_REFILL_RATE = 0.5    # 每秒补 0.5 个，即 30 次/分钟
RATE_LIMIT_PREFIX = "rl:search:user:"

r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
    socket_connect_timeout=2,
)

RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(data[1])
local last = tonumber(data[2])

if tokens == nil or last == nil then
    tokens = capacity
    last = now
end

local elapsed = now - last
if elapsed < 0 then
    elapsed = 0
end

tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call("HSET", key, "tokens", string.format("%.6f", tokens),
           "ts", string.format("%.6f", now))

local ttl = math.ceil(capacity / refill_rate) + 60
redis.call("EXPIRE", key, string.format("%d", ttl))

local wait = 0
if allowed == 0 then
    wait = (cost - tokens) / refill_rate
end

return {allowed, string.format("%.6f", tokens), string.format("%.6f", wait)}
"""


@dataclass
class GuardResult:
    """一次受保护搜索的结果。"""

    data: dict | None
    source: str          # cache / fetched / rate_limited / busy
    wait: float = 0.0    # 被限流时建议等待的秒数


def normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def cache_key(query: str) -> str:
    return "search:v1:" + normalize_query(query).replace(" ", "-")


def lock_key(query: str) -> str:
    return "lock:v1:" + normalize_query(query).replace(" ", "-")


def rate_limit_key(user_id: int | str) -> str:
    return f"{RATE_LIMIT_PREFIX}{user_id}"


def get_cached(query: str) -> dict | None:
    raw = r.get(cache_key(query))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        r.delete(cache_key(query))
        return None
    if not isinstance(data, dict):
        r.delete(cache_key(query))
        return None
    return data


def set_cached(query: str, data: dict, ttl: int = SEARCH_CACHE_TTL) -> None:
    r.set(cache_key(query), json.dumps(data, ensure_ascii=False), ex=ttl)


def delete_cached(query: str) -> None:
    r.delete(cache_key(query))


def acquire_lock(query: str) -> str | None:
    token = str(uuid.uuid4())
    acquired = r.set(lock_key(query), token, nx=True, ex=LOCK_TTL)
    return token if acquired else None


def release_lock(query: str, token: str) -> None:
    r.eval(RELEASE_LOCK_LUA, 1, lock_key(query), token)


def delete_lock(query: str) -> None:
    r.delete(lock_key(query))


def allow_request(
    user_id: int | str,
    capacity: float = RATE_LIMIT_CAPACITY,
    refill_rate: float = RATE_LIMIT_REFILL_RATE,
    cost: int = 1,
) -> tuple[bool, float, float]:
    """从用户令牌桶取 cost 个令牌。

    返回 (allowed, remaining_tokens, wait_seconds)。
    """
    key = rate_limit_key(user_id)
    allowed, tokens, wait = r.eval(
        TOKEN_BUCKET_LUA, 1, key, capacity, refill_rate, cost, time.time()
    )
    return bool(int(allowed)), float(tokens), float(wait)


def get_or_fetch(
    query: str,
    fetch: Callable[[], dict],
    user_id: int | str,
    ttl: int = SEARCH_CACHE_TTL,
) -> GuardResult:
    """缓存 → 单飞锁 → 限流 → 抓取。"""
    cached = get_cached(query)
    if cached is not None:
        return GuardResult(cached, "cache")

    token = acquire_lock(query)

    if token is not None:
        try:
            # 抢到锁后再查一次：可能别人刚写好
            cached = get_cached(query)
            if cached is not None:
                return GuardResult(cached, "cache")

            # 只有真正要调 GitHub 的人才扣令牌
            allowed, _tokens, wait = allow_request(user_id)
            if not allowed:
                return GuardResult(None, "rate_limited", wait)

            data = fetch()
            if not isinstance(data, dict):
                raise TypeError("fetch() 必须返回 dict")

            set_cached(query, data, ttl=ttl)
            return GuardResult(data, "fetched")
        finally:
            # 成功、失败、限流，都要释放锁
            release_lock(query, token)

    # 没抢到锁：等别人写好缓存
    deadline = time.time() + WAIT_TIMEOUT
    while time.time() < deadline:
        time.sleep(WAIT_INTERVAL)
        cached = get_cached(query)
        if cached is not None:
            return GuardResult(cached, "cache")

    return GuardResult(None, "busy", WAIT_INTERVAL)


if __name__ == "__main__":
    import threading
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        raise SystemExit("连不上 Redis，请先运行：brew services start redis")

    query = "MCP server"
    delete_cached(query)
    delete_lock(query)
    r.delete(rate_limit_key(42), rate_limit_key(99), rate_limit_key(7))

    calls = {"fetch": 0}
    calls_lock = threading.Lock()

    def fake_fetch() -> dict:
        with calls_lock:
            calls["fetch"] += 1
        time.sleep(0.5)
        return {"items": ["repo-a", "repo-b"]}

    print("=== 1. 第一次调用：应该 fetched ===")
    result = get_or_fetch(query, fake_fetch, 42, ttl=60)
    print(result.source, result.data)

    print("\n=== 2. 第二次调用：应该 cache ===")
    result = get_or_fetch(query, fake_fetch, 42, ttl=60)
    print(result.source, result.data)

    print("\n=== 3. 清空缓存，20 个并发：应该只 fetch 1 次 ===")
    delete_cached(query)
    delete_lock(query)
    with calls_lock:
        calls["fetch"] = 0

    workers = 20
    barrier = threading.Barrier(workers)

    def task(_):
        barrier.wait()
        return get_or_fetch(query, fake_fetch, 42, ttl=60)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(task, range(workers)))

    print("状态分布:", dict(Counter(result.source for result in results)))
    print("fetch 调用次数:", calls["fetch"])

    print("\n=== 4. 用户 99 没有令牌：应该 rate_limited ===")
    limited_query = "rate-limited-query"
    delete_cached(limited_query)
    delete_lock(limited_query)
    r.hset(rate_limit_key(99), mapping={"tokens": "0", "ts": f"{time.time():.6f}"})
    with calls_lock:
        calls["fetch"] = 0

    result = get_or_fetch(limited_query, fake_fetch, 99, ttl=60)
    print("source:", result.source)
    print("wait:", round(result.wait, 2))
    print("fetch 调用次数:", calls["fetch"])

    print("\n=== 5. 用户 7 命中缓存：不应创建限流 key ===")
    result = get_or_fetch(query, fake_fetch, 7, ttl=60)
    print("source:", result.source)
    print("用户 7 的限流 key 是否存在:", r.exists(rate_limit_key(7)))