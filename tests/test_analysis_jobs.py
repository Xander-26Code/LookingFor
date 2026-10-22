import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import analysis_jobs
import database


class AnalysisJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(database, 'DATABASE_PATH', Path(temp.name) / 'jobs.db')
        patcher.start(); self.addCleanup(patcher.stop)
        database.init_db()
        for user_id in (1, 2, 3):
            database.save_user({'id': user_id, 'login': f'user{user_id}'})
            database.save_star_snapshot(user_id, [{'id': user_id}])
        poll = patch.object(analysis_jobs, 'POLL_SECONDS', .01)
        poll.start(); self.addCleanup(poll.stop)

    def test_atomic_admission_only_one_owner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: database.claim_analysis(1), range(8)))
        self.assertEqual(sum(not c['joined'] for c in claims), 1)
        self.assertEqual(len({c['job_id'] for c in claims}), 1)

    def test_global_concurrency_and_failed_attempt_cooldown(self):
        first = database.claim_analysis(1)
        database.claim_analysis(2)
        self.assertEqual(database.claim_analysis(3)['status'], 'busy')
        database.finish_analysis(1, first['job_id'], None, 'model')
        self.assertEqual(database.claim_analysis(1)['status'], 'limited')
        self.assertFalse(database.claim_analysis(3)['joined'])

    def test_daily_limit_and_expired_job_recovery(self):
        claim = database.claim_analysis(1)
        with database.connect() as connection:
            connection.execute('UPDATE analysis_jobs SET lease_until = 0 WHERE user_id = 1')
        self.assertEqual(database.get_analysis_job(1)['status'], 'interrupted')
        self.assertEqual(database.claim_analysis(1, cooldown=0, daily_limit=1)['status'], 'limited')
        new = database.claim_analysis(1, cooldown=0)
        self.assertNotEqual(new['job_id'], claim['job_id'])
        database.finish_analysis(1, claim['job_id'], ['old'])
        self.assertIsNone(database.load_user_data(1)['keywords'])
        database.finish_analysis(1, new['job_id'], ['new'])
        self.assertEqual(database.load_user_data(1)['keywords'], ['new'])

    def test_page_cancellation_does_not_cancel_model_and_second_page_joins(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def model(repos):
            calls.append(repos)
            started.set()
            if not release.wait(3):
                raise RuntimeError('test did not release model')
            return ['tools']

        async def scenario():
            first = await analysis_jobs.start_analysis(1, model)
            await asyncio.to_thread(started.wait, 1)
            waiter = asyncio.create_task(analysis_jobs.wait_for_analysis(1, first['job_id']))
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
            second = await analysis_jobs.start_analysis(1, model)
            self.assertTrue(second['joined'])
            release.set()
            result = await analysis_jobs.wait_for_analysis(1, second['job_id'])
            self.assertEqual(result['status'], 'succeeded')
            cached = await analysis_jobs.start_analysis(1, model)
            self.assertEqual(cached['status'], 'succeeded')
            # 等待内部后台任务结束，避免测试数据库先被删除。
            await asyncio.gather(*list(analysis_jobs._tasks))

        try:
            asyncio.run(scenario())
        finally:
            release.set()
        self.assertEqual(len(calls), 1)
