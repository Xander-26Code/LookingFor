"""独立于页面的分析任务；刷新或关闭页面不会再次启动模型调用。"""
import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable

from openai import OpenAIError
import database

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
POLL_SECONDS = 0.5


def _perform(user_id: int, claim: dict, generate: Callable) -> None:
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                if not database.renew_analysis(user_id, claim['job_id']):
                    return
            except sqlite3.Error:
                logger.warning('Analysis lease renewal failed for user %s', user_id)
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    keywords, error = None, ''
    try:
        keywords = generate(claim['data']['repositories'])
    except OpenAIError:
        error = 'model'
    except (ValueError, UnicodeError):
        error = 'invalid'
    except Exception:
        error = 'internal'
        logger.warning('Analysis failed for user %s (details omitted to protect input data)', user_id)
    finally:
        try:
            database.finish_analysis(user_id, claim['job_id'], keywords, error)
        finally:
            stopped.set()
            thread.join(timeout=1)


async def start_analysis(user_id: int, generate: Callable) -> dict:
    claim = await asyncio.to_thread(database.claim_analysis, user_id)
    if claim['status'] == 'running' and not claim['joined']:
        # 保留强引用；任务不由某个页面的生命周期拥有。
        task = asyncio.create_task(asyncio.to_thread(_perform, user_id, claim, generate))
        _tasks.add(task)

        def done(finished):
            _tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.warning('Analysis result could not be persisted; lease will expire')

        task.add_done_callback(done)
    return claim


async def wait_for_analysis(user_id: int, job_id: str) -> dict:
    while True:
        job = await asyncio.to_thread(database.get_analysis_job, user_id)
        if job is None or job['job_id'] != job_id:
            return {'status': 'stale'}
        if job['status'] != 'running':
            return job
        await asyncio.sleep(POLL_SECONDS)
