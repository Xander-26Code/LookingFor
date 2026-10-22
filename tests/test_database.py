"""使用临时数据库和模拟 GitHub 响应，不访问真实账号或模型。"""

import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database

with patch('nicegui.ui.run'), patch('dotenv.load_dotenv'):
    import main


class UserDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'test.db'
        patcher = patch.object(database, 'DATABASE_PATH', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        self.profile = {'id': 42, 'login': 'alice', 'name': 'Alice', 'avatar_url': 'https://example.com/a.png'}

    def rows(self):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(
                'SELECT github_id, login, name, avatar_url FROM users ORDER BY github_id'
            ).fetchall()
        finally:
            connection.close()

    def test_new_user_is_saved_and_init_preserves_it(self):
        self.assertEqual(database.save_user(self.profile), self.profile)
        database.init_db()
        self.assertEqual(self.rows(), [(42, 'alice', 'Alice', 'https://example.com/a.png')])

    def test_returning_user_updates_by_id_without_duplicates(self):
        database.save_user(self.profile)
        updated = {**self.profile, 'login': 'alice-renamed', 'name': "Alice's new name"}
        database.save_user(updated)
        self.assertEqual(self.rows(), [(42, 'alice-renamed', "Alice's new name", 'https://example.com/a.png')])

    def test_distinct_users_and_missing_optional_fields(self):
        database.save_user(self.profile)
        user = database.save_user({'id': 99, 'login': 'bob', 'name': None, 'avatar_url': None})
        self.assertEqual(user, {'id': 99, 'login': 'bob', 'name': 'bob', 'avatar_url': ''})
        self.assertEqual(len(self.rows()), 2)

    def test_invalid_identity_is_not_saved(self):
        invalid = [None, {}, {'id': True, 'login': 'alice'}, {'id': '42', 'login': 'alice'},
                   {'id': -1, 'login': 'alice'}, {'id': 42, 'login': ' '}, {'id': 42, 'login': 123}]
        for profile in invalid:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                database.save_user(profile)
        self.assertEqual(self.rows(), [])

    def test_concurrent_users_keep_separate_snapshots_and_analyses(self):
        database.save_user(self.profile)
        database.save_user({'id': 99, 'login': 'bob'})

        def work(user_id):
            repositories = [{'id': user_id, 'full_name': f'example/user-{user_id}'}]
            version = database.save_star_snapshot(user_id, repositories)
            self.assertTrue(database.save_analysis(user_id, version, [f'tools-{user_id}']))
            return database.load_user_data(user_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            alice, bob = list(pool.map(work, [42, 99]))
        self.assertEqual(alice['repositories'][0]['id'], 42)
        self.assertEqual(alice['keywords'], ['tools-42'])
        self.assertEqual(bob['repositories'][0]['id'], 99)
        self.assertEqual(bob['keywords'], ['tools-99'])
        self.assertFalse(database.save_analysis(99, alice['version'], ['wrong user']))
        self.assertEqual(database.load_user_data(99), bob)

    def test_resync_invalidates_analysis_and_rejects_stale_results(self):
        database.save_user(self.profile)
        old = database.save_star_snapshot(42, [{'id': 1}])
        database.save_analysis(42, old, ['old'])
        new = database.save_star_snapshot(42, [{'id': 2}])
        self.assertNotEqual(new, old)
        self.assertIsNone(database.load_user_data(42)['keywords'])
        self.assertFalse(database.save_analysis(42, old, ['late result']))
        self.assertTrue(database.save_analysis(42, new, ['new']))
        self.assertEqual(database.load_user_data(42)['keywords'], ['new'])

    def test_invalid_snapshot_preserves_previous_data_and_empty_snapshot_is_valid(self):
        database.save_user(self.profile)
        version = database.save_star_snapshot(42, [{'id': 1}])
        database.save_analysis(42, version, ['tools'])
        before = database.load_user_data(42)
        with self.assertRaises(ValueError):
            database.save_star_snapshot(42, {'error': 'bad data'})
        self.assertEqual(database.load_user_data(42), before)
        database.save_star_snapshot(42, [])
        self.assertEqual(database.load_user_data(42)['repositories'], [])
        self.assertIsNone(database.load_user_data(42)['keywords'])

    def test_unregistered_user_cannot_create_snapshot(self):
        with self.assertRaises(sqlite3.IntegrityError):
            database.save_star_snapshot(123, [{'id': 1}])
        self.assertIsNone(database.load_user_data(123))

    def run_callback(self, profile, *, save_error=None):
        session = {'state': 'test-state'}
        fake_app = SimpleNamespace(storage=SimpleNamespace(user=session))

        def response(payload):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

        async def io_bound(function, *args, **kwargs):
            return function(*args, **kwargs)

        real_save = database.save_user

        def save(info):
            # 在保存完成前，会话不能提前显示登录成功。
            self.assertNotIn('github_user', session)
            self.assertNotIn('github_token', session)
            if save_error is not None:
                raise save_error
            return real_save(info)

        with patch.object(main, 'app', fake_app), \
                patch.object(main.run, 'io_bound', side_effect=io_bound), \
                patch.object(main.requests, 'post', return_value=response({'access_token': 'fake-token'})), \
                patch.object(main.requests, 'get', side_effect=[response(profile), response([])]) as get, \
                patch.object(main, 'save_user', side_effect=save), \
                patch.object(main.ui, 'label') as label, patch.object(main.ui, 'link'), \
                patch.object(main, 'notice'):
            asyncio.run(main.sync_github_stars('test-code', 'test-state'))
            return session, get.call_count, label

    def test_callback_saves_before_setting_session_and_syncing_stars(self):
        session, calls, _ = self.run_callback(self.profile)
        self.assertEqual(session['github_user'], self.profile)
        self.assertEqual(session['github_token'], 'fake-token')
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(database.load_user_data(42)['repositories'], [])
        self.assertFalse((self.root / 'starred_repos.json').exists())

    def test_database_failure_stops_login_and_star_sync(self):
        session, calls, label = self.run_callback(self.profile, save_error=sqlite3.OperationalError('locked'))
        self.assertNotIn('github_user', session)
        self.assertNotIn('github_token', session)
        self.assertEqual(calls, 1)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.root / 'starred_repos.json').exists())
        label.assert_called_once_with('暂时无法保存用户资料，请稍后重新登录。')

    def test_invalid_github_identity_stops_login(self):
        session, calls, label = self.run_callback({'login': 'alice'})
        self.assertNotIn('github_user', session)
        self.assertEqual(calls, 1)
        self.assertEqual(self.rows(), [])
        label.assert_called_once_with('GitHub 返回的用户 ID 或用户名无效，请重新登录。')


if __name__ == '__main__':
    unittest.main()
