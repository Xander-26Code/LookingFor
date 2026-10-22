"""运行：python -m unittest discover -s tests -v（不请求真实 API）。"""

import hashlib
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import analyse
from openai import OpenAIError

# 导入页面函数用于测试，但不启动 NiceGUI 服务，也不读取真实 .env。
with patch('nicegui.ui.run'), patch('dotenv.load_dotenv'):
    import main


class AnalysisFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patcher = patch.object(analyse, 'project_dir', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(analyse, 'load_dotenv')
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(analyse, 'OpenAI')
        self.client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.create = self.client_factory.return_value.chat.completions.create
        self.set_response({'interests': [{'search_keywords': [' python ', 'python', 'agent tools']}]})

    def set_response(self, payload):
        self.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    def save_stars(self, name='example/first', ensure_ascii=True):
        content = json.dumps([{'full_name': name, 'description': '中文简介',
                               'language': 'Python', 'topics': ['agents']}],
                             ensure_ascii=ensure_ascii).encode('utf-8')
        (self.root / 'starred_repos.json').write_bytes(content)
        return content

    def test_import_without_stars_or_api_key_has_no_side_effects(self):
        copied = self.root / 'analyse.py'
        copied.write_text(Path(analyse.__file__).read_text())
        spec = importlib.util.spec_from_file_location('isolated_analysis', copied)
        module = importlib.util.module_from_spec(spec)
        with patch('openai.OpenAI') as constructor, patch('dotenv.load_dotenv') as dotenv:
            spec.loader.exec_module(module)
        constructor.assert_not_called()
        dotenv.assert_not_called()
        self.assertEqual([p.name for p in self.root.glob('*.json')], [])

    def test_each_call_reads_latest_stars(self):
        self.save_stars('example/first')
        self.assertEqual(analyse.generate_search_keywords(), ['python', 'agent tools'])
        self.save_stars('example/second')
        analyse.generate_search_keywords()
        prompt = self.create.call_args.kwargs['messages'][1]['content']
        self.assertIn('example/second', prompt)
        self.assertNotIn('example/first', prompt)

    def test_hash_matches_exact_file_bytes_with_both_encodings(self):
        for escaped in (True, False):
            with self.subTest(ensure_ascii=escaped):
                content = self.save_stars(ensure_ascii=escaped)
                analyse.generate_search_keywords()
                saved = json.loads((self.root / 'search_keywords.json').read_text())
                self.assertEqual(saved['source_sha256'], hashlib.sha256(content).hexdigest())

    def test_failed_model_response_preserves_existing_data(self):
        content = self.save_stars()
        keywords = self.root / 'search_keywords.json'
        keywords.write_text('{"previous": true}')
        self.create.return_value.choices[0].message.content = 'not JSON'
        with self.assertRaises(ValueError):
            analyse.generate_search_keywords()
        self.assertEqual((self.root / 'starred_repos.json').read_bytes(), content)
        self.assertEqual(keywords.read_text(), '{"previous": true}')

    def test_empty_stars_do_not_call_model(self):
        (self.root / 'starred_repos.json').write_text('[]')
        with self.assertRaises(ValueError):
            analyse.generate_search_keywords()
        self.client_factory.assert_not_called()

    def test_failures_do_not_redirect_and_retry_can_succeed(self):
        with patch.object(main, 'generate_search_keywords') as generate, \
                patch.object(main, 'notice') as notice, patch.object(main.ui.navigate, 'to') as navigate, \
                patch.object(main.ui, 'row'), patch.object(main.ui, 'spinner'), patch.object(main.ui, 'label'):
            for error in (OpenAIError('mock service failure'), ValueError('invalid JSON'), OSError('disk error')):
                with self.subTest(error=type(error).__name__):
                    generate.side_effect = error
                    self.assertFalse(asyncio.run(main.analyse_and_open_search()))
                    navigate.assert_not_called()
            self.assertEqual(notice.call_count, 3)
            generate.side_effect = None
            generate.return_value = []
            self.assertFalse(asyncio.run(main.analyse_and_open_search()))
            navigate.assert_not_called()
            generate.return_value = ['python']
            self.assertTrue(asyncio.run(main.analyse_and_open_search()))
            navigate.assert_called_once_with('/search')

    def test_slow_model_does_not_block_other_async_work(self):
        def slow_model():
            time.sleep(0.15)
            return ['python']

        async def check():
            task = asyncio.create_task(main.analyse_and_open_search())
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), 'The event loop was blocked until model completion.')
            self.assertTrue(await task)

        with patch.object(main, 'generate_search_keywords', side_effect=slow_model), \
                patch.object(main.ui.navigate, 'to'), patch.object(main.ui, 'row'), \
                patch.object(main.ui, 'spinner'), patch.object(main.ui, 'label'):
            asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
