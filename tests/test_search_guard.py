"""搜索保护层的回归测试：不连接真实 Redis/GitHub。"""

import json
import time
import unittest
from unittest.mock import patch

import redis

import search
import search_guard


class FakeRedis:
    def __init__(self, *, fail_get=False):
        self.values = {}
        self.fail_get = fail_get

    def get(self, key):
        if self.fail_get:
            raise redis.RedisError("redis down")
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1

    def eval(self, script, numkeys, *args):
        return 0


def public_page(items, *, has_more=True, limit_reached=False, incomplete=False):
    return {
        "items": items,
        "has_more": has_more,
        "limit_reached": limit_reached,
        "incomplete_results": incomplete,
    }


class SearchGuardTests(unittest.TestCase):
    def test_cache_key_does_not_collide_for_different_queries_or_pages(self):
        first = search_guard.cache_key('"a b" is:public', page=1)
        second = search_guard.cache_key('"a-b" is:public', page=1)
        third = search_guard.cache_key('"a b" is:public', page=2)

        self.assertTrue(first.startswith('search:public:v2:'))
        self.assertEqual(len({first, second, third}), 3)
        self.assertNotEqual(first, third)

    def test_lock_ttl_is_longer_than_request_timeout(self):
        self.assertGreater(search_guard.LOCK_TTL, search.REQUEST_TIMEOUT)

    def test_public_page_rejects_private_or_incomplete_cache_entries(self):
        good = public_page([{"id": 1, "private": False, "visibility": "public"}])
        private = public_page([{"id": 1, "private": True, "visibility": "private"}])
        missing = {"items": []}

        self.assertTrue(search_guard.public_page(good))
        self.assertFalse(search_guard.public_page(private))
        self.assertFalse(search_guard.public_page(missing))

    def test_redis_failure_is_reported_as_cache_unavailable(self):
        with patch.object(search_guard, 'get_client', return_value=FakeRedis(fail_get=True)):
            with self.assertRaises(search_guard.SearchError) as raised:
                search_guard.get_or_fetch('"x" is:public', lambda: public_page([]), 42)
        self.assertEqual(raised.exception.code, 'cache_unavailable')

    def test_rate_limit_error_is_not_masked_by_cooldown_failure(self):
        fake = FakeRedis()
        with patch.object(search_guard, 'get_client', return_value=fake), \
                patch.object(search_guard, 'set_cooldown', side_effect=redis.RedisError('down')):
            with self.assertRaises(search_guard.SearchError) as raised:
                search_guard.get_or_fetch(
                    '"x" is:public',
                    lambda: (_ for _ in ()).throw(search_guard.SearchError('rate_limit', wait=10)),
                    42,
                )
        self.assertEqual(raised.exception.code, 'rate_limit')

    def test_github_remaining_quota_sets_cooldown(self):
        fake = FakeRedis()
        data = public_page([{"id": 1, "private": False, "visibility": "public"}])
        data["_rate_remaining"] = 1
        data["_rate_reset_after"] = 30

        with patch.object(search_guard, 'get_client', return_value=fake), \
                patch.object(search_guard, 'set_cooldown') as cooldown:
            result = search_guard.get_or_fetch('"x" is:public', lambda: dict(data), 42)

        self.assertEqual(result.source, 'fetched_uncached')
        cooldown.assert_called_once_with(42, 30)
        self.assertNotIn('_rate_remaining', result.data)

    def test_fetch_filters_private_repositories_and_returns_rate_metadata(self):
        response = type('Response', (), {
            'status_code': 200,
            'headers': {
                'X-RateLimit-Remaining': '10',
                'X-RateLimit-Reset': str(int(time.time()) + 30),
            },
            'text': '',
            'json': lambda self: {
                'total_count': 2,
                'incomplete_results': False,
                'items': [
                    {'id': 1, 'private': False, 'visibility': 'public'},
                    {'id': 2, 'private': True, 'visibility': 'private'},
                ],
            },
        })()

        with patch.object(search.requests, 'get', return_value=response):
            data = search.fetch_repositories('"x" is:public')

        self.assertEqual([item['id'] for item in data['items']], [1])
        self.assertEqual(data['_rate_remaining'], 10)
        self.assertGreaterEqual(data['_rate_reset_after'], 1)


if __name__ == "__main__":
    unittest.main()
