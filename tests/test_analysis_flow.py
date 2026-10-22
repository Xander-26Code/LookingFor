"""分析流程与多用户隔离测试：临时 SQLite + 模拟模型，无真实 API 请求。"""

import asyncio
import importlib.util
import json
import re
import sqlite3
import tempfile
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import analyse
import database
import analysis_jobs
from openai import OpenAIError

with patch('nicegui.ui.run'), patch('dotenv.load_dotenv'):
    import main


class AnalysisFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch(database, 'DATABASE_PATH', self.root / 'test.db')
        database.init_db()
        self.patch(database, 'claim_analysis', side_effect=partial(database.claim_analysis, cooldown=0))
        self.patch(analysis_jobs, 'POLL_SECONDS', 0.01)
        database.save_user({'id': 42, 'login': 'alice'})
        database.save_user({'id': 99, 'login': 'bob'})
        self.session = {'github_user': {'id': 42, 'login': 'alice'}, 'github_token': 'fake-token'}
        self.patch(main, 'app', SimpleNamespace(storage=SimpleNamespace(user=self.session)))
        self.patch(analyse, 'load_dotenv')
        self.client_factory = self.patch(analyse, 'OpenAI')
        self.create = self.client_factory.return_value.chat.completions.create
        self.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'interests': [{'search_keywords': [' python ', 'python', 'agent tools']}]})))])
        self.notice = self.patch(main, 'notice')
        self.empty = self.patch(main, 'empty_state')
        self.navigate = self.patch(main.ui.navigate, 'to')
        for name in ('row', 'spinner', 'label'):
            self.patch(main.ui, name)

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def stars(self, user_id=42, name='example/alice'):
        repos = [{'id': user_id, 'full_name': name, 'description': '中文简介',
                  'language': 'Python', 'topics': ['agents']}]
        database.save_star_snapshot(user_id, repos)
        return repos

    def test_import_does_not_read_files_or_call_model(self):
        copied = self.root / 'analyse.py'
        copied.write_text(Path(analyse.__file__).read_text())
        spec = importlib.util.spec_from_file_location('isolated_analysis', copied)
        module = importlib.util.module_from_spec(spec)
        with patch('openai.OpenAI') as constructor, patch('dotenv.load_dotenv') as dotenv:
            spec.loader.exec_module(module)
        constructor.assert_not_called()
        dotenv.assert_not_called()
        self.assertEqual(list(self.root.glob('*.json')), [])

    def test_model_uses_only_explicit_input_and_deduplicates_keywords(self):
        alice = self.stars()
        bob = self.stars(99, 'example/bob')
        self.assertEqual(analyse.generate_search_keywords(alice), ['python', 'agent tools'])
        analyse.generate_search_keywords(bob)
        prompt = self.create.call_args.kwargs['messages'][1]['content']
        self.assertIn('example/bob', prompt)
        self.assertNotIn('example/alice', prompt)

    def test_model_keywords_are_capped(self):
        repos = self.stars()
        keywords = [f'keyword {index}' for index in range(90)]
        self.create.return_value.choices[0].message.content = json.dumps({
            'interests': [{'search_keywords': keywords}]
        })
        result = analyse.generate_search_keywords(repos)
        self.assertEqual(len(result), analyse.MAX_SEARCH_KEYWORDS)

    def test_empty_model_content_retries_without_thinking(self):
        repos = self.stars()
        empty = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='length',
            message=SimpleNamespace(content='', reasoning_content='thinking...'),
        )])
        good = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop',
            message=SimpleNamespace(content=json.dumps({
                'interests': [{'search_keywords': ['python']}]
            })),
        )])
        self.create.side_effect = [empty, good]
        result = analyse.generate_search_keywords(repos)
        self.assertEqual(result, ['python'])
        self.assertEqual(self.create.call_count, 2)
        self.assertNotIn('reasoning_effort', self.create.call_args_list[1].kwargs)

    def test_long_repository_metadata_is_compacted_before_model_call(self):
        repos = [
            {
                'full_name': f'example/repo-{index}',
                'description': 'x' * 5000,
                'language': 'Python',
                'topics': ['topic-' + 'y' * 100 for _ in range(20)],
                'html_url': f'https://github.com/example/repo-{index}',
            }
            for index in range(118)
        ]
        result = analyse.generate_search_keywords(repos)
        self.assertEqual(result, ['python', 'agent tools'])
        prompt = self.create.call_args.kwargs['messages'][1]['content']
        self.assertIn('example/repo-0', prompt)
        self.assertNotIn('x' * 1000, prompt)
        self.assertLess(len(prompt), analyse.MAX_INPUT_CHARS + 2000)

    def test_large_collection_is_split_into_multiple_model_calls(self):
        repos = [
            {
                'full_name': f'example/repo-{index}',
                'description': 'x' * 5000,
                'language': ['Python', 'Rust', 'Go'][index % 3],
                'topics': [f'topic-{index % 12}', 'y' * 100],
                'html_url': f'https://github.com/example/repo-{index}',
            }
            for index in range(500)
        ]
        result = analyse.generate_search_keywords(repos)
        self.assertEqual(result, ['python', 'agent tools'])
        self.assertGreater(self.create.call_count, 1)
        self.assertLessEqual(self.create.call_count, analyse.MAX_MODEL_CALLS)
        prompts = [
            call.kwargs['messages'][1]['content']
            for call in self.create.call_args_list
        ]
        self.assertTrue(any('example/repo-0' in prompt for prompt in prompts))
        self.assertTrue(all(
            len(prompt) < analyse.MAX_INPUT_CHARS + 2000
            for prompt in prompts
        ))
        represented = {
            int(number)
            for prompt in prompts
            for number in re.findall(r'example/repo-(\d+)', prompt)
        }
        self.assertEqual(represented, set(range(500)))

    def test_very_large_collection_is_capped_to_max_model_calls(self):
        repos = [
            {
                'full_name': f'example/repo-{index}',
                'description': 'x' * 5000,
                'language': ['Python', 'Rust', 'Go'][index % 3],
                'topics': [f'topic-{index % 12}', 'y' * 100],
                'html_url': f'https://github.com/example/repo-{index}',
            }
            for index in range(1000)
        ]
        analyse.generate_search_keywords(repos)
        self.assertLessEqual(self.create.call_count, analyse.MAX_MODEL_CALLS)

    def test_empty_stars_do_not_call_model(self):
        with self.assertRaises(ValueError):
            analyse.generate_search_keywords([])
        self.client_factory.assert_not_called()

    def test_invalid_model_response_preserves_stars(self):
        self.stars()
        before = database.load_user_data(42)
        self.create.return_value.choices[0].message.content = 'not JSON'
        self.assertFalse(asyncio.run(main.analyse_and_open_search()))
        self.assertEqual(database.load_user_data(42), before)
        self.navigate.assert_not_called()

    def test_failures_do_not_redirect_and_retry_can_succeed(self):
        self.stars()
        with patch.object(main, 'generate_search_keywords') as generate:
            for error in (OpenAIError('mock failure'), ValueError('invalid JSON'), sqlite3.OperationalError('locked')):
                generate.side_effect = error
                self.assertFalse(asyncio.run(main.analyse_and_open_search()))
                self.navigate.assert_not_called()
                self.assertIsNone(database.load_user_data(42)['keywords'])
            generate.side_effect = None
            generate.return_value = []
            self.assertFalse(asyncio.run(main.analyse_and_open_search()))
            generate.return_value = ['python']
            self.assertTrue(asyncio.run(main.analyse_and_open_search()))
            self.assertEqual(database.load_user_data(42)['keywords'], ['python'])
            self.navigate.assert_called_once_with('/search')

    def test_saved_analysis_is_reused_without_model(self):
        self.stars()
        data = database.load_user_data(42)
        database.save_analysis(42, data['version'], ['python'])
        self.assertTrue(asyncio.run(main.analyse_and_open_search()))
        self.client_factory.assert_not_called()

    def test_identical_resync_reuses_analysis_without_calling_model(self):
        repos = self.stars()
        data = database.load_user_data(42)
        database.save_analysis(42, data['version'], ['python'])
        database.save_star_snapshot(42, [{**repos[0], 'stargazers_count': 100}])
        self.assertTrue(asyncio.run(main.analyse_and_open_search()))
        self.client_factory.assert_not_called()
        self.navigate.assert_called_once_with('/search')

    def test_old_model_result_does_not_replace_new_snapshot_analysis(self):
        self.stars()

        def update_during_analysis(repos):
            version = database.save_star_snapshot(42, [{'id': 2, 'full_name': 'example/new'}])
            database.save_analysis(42, version, ['new keywords'])
            return ['old keywords']

        with patch.object(main, 'generate_search_keywords', side_effect=update_during_analysis):
            self.assertFalse(asyncio.run(main.analyse_and_open_search()))
        self.assertEqual(database.load_user_data(42)['keywords'], ['new keywords'])
        self.navigate.assert_not_called()

    def test_switching_account_during_analysis_does_not_save_into_new_account(self):
        self.stars()
        self.stars(99, 'example/bob')

        def switch_account(repos):
            self.session['github_user'] = {'id': 99, 'login': 'bob'}
            return ['alice keywords']

        with patch.object(main, 'generate_search_keywords', side_effect=switch_account):
            self.assertFalse(asyncio.run(main.analyse_and_open_search()))
        self.assertIsNone(database.load_user_data(99)['keywords'])
        self.navigate.assert_not_called()

    def test_anonymous_and_legacy_sessions_cannot_read_or_analyse_other_users(self):
        self.stars()
        for user in ({}, {'login': 'alice'}):
            self.session['github_user'] = user
            with patch.object(main, 'load_user_data') as read:
                self.assertIsNone(asyncio.run(main.load_saved_stars()))
                self.assertFalse(asyncio.run(main.analyse_and_open_search()))
                asyncio.run(main.search_repositories())
                read.assert_not_called()
        self.client_factory.assert_not_called()

    def test_star_reads_follow_current_user(self):
        alice = self.stars()
        bob = self.stars(99, 'example/bob')
        self.assertEqual(asyncio.run(main.load_saved_stars()), alice)
        self.session['github_user'] = {'id': 99, 'login': 'bob'}
        self.assertEqual(asyncio.run(main.load_saved_stars()), bob)

    def test_search_uses_current_users_keywords_and_star_badges(self):
        self.stars()
        self.stars(99, 'example/bob')
        for user_id, keyword in ((42, 'alice tools'), (99, 'bob tools')):
            data = database.load_user_data(user_id)
            database.save_analysis(user_id, data['version'], [keyword])
        for user_id, keyword in ((42, 'alice tools'), (99, 'bob tools')):
            self.session['github_user'] = {'id': user_id, 'login': 'test'}
            with patch('discovery.search_one', return_value=SimpleNamespace(
                    source='cache', data={'items': [{'id': user_id}], 'has_more': False,
                                          'limit_reached': False, 'incomplete_results': False})) as fetch, \
                    patch.object(main, 'repository_browser') as browser, \
                    patch.object(main.ui, 'button'), patch.object(main.ui, 'timer'):
                asyncio.run(main.search_repositories())
                from search import build_query
                fetch.assert_awaited_once_with(build_query([keyword]), user_id, 'fake-token', page=1)
                self.assertEqual(browser.call_args.kwargs['keywords'], [keyword])
                self.assertEqual(browser.call_args.kwargs['starred_ids'], {user_id})

    def test_slow_model_does_not_block_other_async_work(self):
        self.stars()

        def slow_model(repos):
            time.sleep(0.15)
            return ['python']

        async def check():
            task = asyncio.create_task(main.analyse_and_open_search())
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertTrue(await task)

        with patch.object(main, 'generate_search_keywords', side_effect=slow_model):
            asyncio.run(check())

    def test_two_pages_share_one_model_call(self):
        self.stars()
        calls = []

        def model(repos):
            calls.append(1)
            time.sleep(0.1)
            return ['python']

        async def scenario():
            with patch.object(main, 'generate_search_keywords', side_effect=model):
                results = await asyncio.gather(
                    main.analyse_and_open_search(),
                    main.analyse_and_open_search(),
                )
            await asyncio.gather(*list(analysis_jobs._tasks))
            return results

        results = asyncio.run(scenario())
        self.assertEqual(results, [True, True])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
