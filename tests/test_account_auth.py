"""账号会话与 GitHub App token 刷新测试；不访问真实 GitHub。"""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch('nicegui.ui.run'), patch('dotenv.load_dotenv'):
    import main


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


async def run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class AccountAuthTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': 42, 'login': 'alice'}
        self.session = {'github_user': self.user}
        patcher = patch.object(main, 'app', SimpleNamespace(
            storage=SimpleNamespace(user=self.session)
        ))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_store_tokens_keeps_refresh_and_expiry(self):
        main.store_github_tokens({
            'access_token': 'access-1',
            'refresh_token': 'refresh-1',
            'expires_in': 28800,
            'refresh_token_expires_in': 15897600,
        })
        self.assertEqual(self.session['github_token'], 'access-1')
        self.assertEqual(self.session['github_refresh_token'], 'refresh-1')
        self.assertGreater(self.session['github_token_expires_at'], time.time())
        self.assertGreater(self.session['github_refresh_expires_at'], time.time())

    def test_valid_token_is_reused_without_network(self):
        self.session.update({
            'github_token': 'valid',
            'github_token_expires_at': time.time() + 3600,
            'github_refresh_token': 'refresh',
        })
        with patch.object(main.requests, 'post') as post:
            token = asyncio.run(main.get_valid_github_token())
        self.assertEqual(token, 'valid')
        post.assert_not_called()

    def test_expired_token_is_refreshed_and_rotated(self):
        self.session.update({
            'github_token': 'old',
            'github_token_expires_at': time.time() - 1,
            'github_refresh_token': 'old-refresh',
            'github_refresh_expires_at': time.time() + 3600,
        })
        response = FakeResponse({
            'access_token': 'new-access',
            'refresh_token': 'new-refresh',
            'expires_in': 28800,
            'refresh_token_expires_in': 15897600,
        })
        with patch.object(main.run, 'io_bound', new=run_inline), \
                patch.object(main.requests, 'post', return_value=response) as post:
            token = asyncio.run(main.get_valid_github_token())

        self.assertEqual(token, 'new-access')
        self.assertEqual(self.session['github_refresh_token'], 'new-refresh')
        self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'refresh_token')

    def test_invalid_refresh_token_clears_credentials(self):
        self.session.update({
            'github_token': 'old',
            'github_token_expires_at': time.time() - 1,
            'github_refresh_token': 'bad-refresh',
        })
        response = FakeResponse({'error': 'bad_refresh_token'}, status_code=400)
        with patch.object(main.run, 'io_bound', new=run_inline), \
                patch.object(main.requests, 'post', return_value=response):
            token = asyncio.run(main.get_valid_github_token())

        self.assertIsNone(token)
        self.assertNotIn('github_token', self.session)
        self.assertNotIn('github_refresh_token', self.session)

    def test_clear_session_removes_identity_and_tokens(self):
        self.session.update({
            'github_token': 'token',
            'github_refresh_token': 'refresh',
            'github_token_expires_at': time.time() + 100,
            'github_refresh_expires_at': time.time() + 200,
            'state': 'oauth-state',
        })
        main.clear_github_session()
        self.assertEqual(self.session, {})


if __name__ == '__main__':
    unittest.main()
