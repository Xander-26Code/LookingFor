"""公开仓库分页缓存、可续期请求锁、滚动窗口限流和 GitHub 冷却时间。"""
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

import redis
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
SEARCH_CACHE_TTL = 6 * 60 * 60
LOCK_TTL = 60  # 必须明显大于一次 GitHub 请求的 15 秒超时
RENEW_INTERVAL = 10
WAIT_TIMEOUT = 2.0
RATE_LIMIT_CAPACITY = 25  # 保守地低于 GitHub 的认证搜索额度
RATE_WINDOW_SECONDS = 60
API_VERSION = '2026-03-10'


class SearchError(Exception):
    def __init__(self, code: str, detail: str = '', wait: float = 0):
        super().__init__(code)
        self.code, self.detail, self.wait = code, detail, wait


@dataclass
class GuardResult:
    data: dict | None
    source: str
    wait: float = 0.0


@lru_cache(maxsize=1)
def get_client():
    # 首次使用才读环境变量，避免 main.py 尚未加载 .env 就创建连接。
    load_dotenv()
    options = dict(decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    if os.getenv('REDIS_URL'):
        return redis.Redis.from_url(os.environ['REDIS_URL'], **options)
    return redis.Redis(host=os.getenv('REDIS_HOST', '127.0.0.1'),
                       port=int(os.getenv('REDIS_PORT', '6379')),
                       db=int(os.getenv('REDIS_DB', '0')), **options)


def ping() -> bool:
    """健康检查用：Redis 可连接。失败时抛 redis.RedisError。"""
    return bool(get_client().ping())


def cache_key(query: str, page: int = 1, per_page: int = 10) -> str:
    # 不替换查询内的空格或连字符，完整编码影响结果的参数。
    signature = json.dumps([query, page, per_page, 'stars', 'desc', API_VERSION, 'public'], ensure_ascii=False)
    return 'search:public:v2:' + hashlib.sha256(signature.encode()).hexdigest()


def public_page(data) -> bool:
    return (isinstance(data, dict) and isinstance(data.get('items'), list)
            and type(data.get('has_more')) is bool and type(data.get('limit_reached')) is bool
            and type(data.get('incomplete_results')) is bool
            and all(isinstance(item, dict) and type(item.get('id')) is int
                    and item.get('private') is False
                    and item.get('visibility', 'public') == 'public' for item in data['items']))


def get_cached(query: str, page: int = 1, per_page: int = 10) -> dict | None:
    client = get_client()
    key = cache_key(query, page, per_page)
    raw = client.get(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if public_page(data):
            return data
    except (ValueError, TypeError):
        pass
    client.delete(key)
    return None


RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end return 0"
SAVE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""
WINDOW = """
local cooldown = redis.call('pttl', KEYS[2])
if cooldown > 0 then return cooldown end
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
local window = tonumber(ARGV[2])*1000
redis.call('zremrangebyscore', KEYS[1], '-inf', now-window)
if redis.call('zcard', KEYS[1]) >= tonumber(ARGV[1]) then
    local first = redis.call('zrange', KEYS[1], 0, 0, 'WITHSCORES')
    return math.max(1, tonumber(first[2])+window-now)
end
redis.call('zadd', KEYS[1], now, ARGV[3])
redis.call('expire', KEYS[1], ARGV[2])
return 0
"""
COOLDOWN = """
local remaining = redis.call('pttl', KEYS[1])
if remaining < tonumber(ARGV[1]) then redis.call('psetex', KEYS[1], ARGV[1], '1') end
"""


def set_cooldown(user_id, seconds):
    get_client().eval(COOLDOWN, 1, f'search:cooldown:{user_id}', max(1, math.ceil(seconds * 1000)))


@contextmanager
def lease(key: str):
    client = get_client()
    token = uuid.uuid4().hex
    if not client.set(key, token, nx=True, ex=LOCK_TTL):
        yield None
        return
    stopped = threading.Event()

    def renew():
        while not stopped.wait(RENEW_INTERVAL):
            try:
                if not client.eval(RENEW, 1, key, token, LOCK_TTL):
                    return
            except redis.RedisError:
                return

    worker = threading.Thread(target=renew, daemon=True)
    worker.start()
    try:
        yield token
    finally:
        stopped.set()
        worker.join(timeout=3)
        try:
            client.eval(RELEASE, 1, key, token)
        except redis.RedisError:
            # 解锁异常不能掩盖已经得到的 GitHub 结果；锁最终由 TTL 回收。
            logger.warning('Search lock release failed; waiting for expiry')


def get_or_fetch(query, fetch, user_id, *, page=1, per_page=10, ttl=SEARCH_CACHE_TTL) -> GuardResult:
    try:
        cached = get_cached(query, page, per_page)
        if cached is not None:
            return GuardResult(cached, 'cache')
        key = cache_key(query, page, per_page)
        with lease('lock:' + key) as owner:
            if owner is None:
                deadline = time.monotonic() + WAIT_TIMEOUT
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                    cached = get_cached(query, page, per_page)
                    if cached is not None:
                        return GuardResult(cached, 'cache')
                return GuardResult(None, 'busy', 1)
            cached = get_cached(query, page, per_page)
            if cached is not None:
                return GuardResult(cached, 'cache')
            # 同一账号的 GitHub 搜索串行执行，其他页面不会越过新收到的冷却时间。
            with lease(f'lock:search:user:{user_id}') as user_owner:
                if user_owner is None:
                    return GuardResult(None, 'busy', 1)
                wait_ms = get_client().eval(WINDOW, 2, f'search:window:{user_id}',
                                           f'search:cooldown:{user_id}', RATE_LIMIT_CAPACITY,
                                           RATE_WINDOW_SECONDS, uuid.uuid4().hex)
                if wait_ms > 0:
                    return GuardResult(None, 'rate_limited', wait_ms / 1000)
                try:
                    data = fetch()
                except SearchError as exc:
                    if exc.code == 'rate_limit':
                        try:
                            set_cooldown(user_id, max(1, exc.wait))
                        except redis.RedisError:
                            # 冷却写入失败不能把真正的限流原因覆盖成缓存故障。
                            logger.warning('GitHub rate-limit cooldown could not be saved')
                    raise

                remaining = data.pop('_rate_remaining', None)
                reset_after = data.pop('_rate_reset_after', 0)
                if not public_page(data):
                    raise SearchError('unexpected')

                # GitHub 的真实剩余额度是最终裁判：只剩 0~1 次时，直接停到 reset。
                if remaining is not None and remaining <= 1:
                    try:
                        set_cooldown(user_id, reset_after or 60)
                    except redis.RedisError:
                        logger.warning('GitHub quota cooldown could not be saved')

                try:
                    # 不完整结果短暂缓存，之后允许重新查询。
                    cache_ttl = min(ttl, 30) if data['incomplete_results'] else ttl
                    saved = get_client().eval(SAVE, 2, 'lock:' + key, key, owner,
                                              json.dumps(data, ensure_ascii=False), cache_ttl)
                except redis.RedisError:
                    return GuardResult(data, 'fetched_uncached')
                return GuardResult(data, 'fetched' if saved else 'fetched_uncached')
    except (redis.RedisError, ValueError, OSError) as exc:
        raise SearchError('cache_unavailable') from exc
