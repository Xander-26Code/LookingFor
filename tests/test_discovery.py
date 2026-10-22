import asyncio
import unittest
from unittest.mock import patch

from discovery import DiscoverySession
from search_guard import GuardResult, SearchError


def page(ids, more=False, limited=False):
    return GuardResult({'items': [{'id': i, 'private': False} for i in ids],
                        'has_more': more, 'limit_reached': limited, 'incomplete_results': False}, 'cache')


class DiscoveryTests(unittest.TestCase):
    def test_paging_merges_and_deduplicates_including_starred_repos(self):
        session = DiscoverySession(['python'])
        with patch('discovery.search_one', side_effect=[page([1, 2], True), page([2, 3])]) as search:
            asyncio.run(session.load_more(42, 'token', budget=1))
            self.assertTrue(session.has_more)
            asyncio.run(session.load_more(42, 'token', budget=1))
        self.assertEqual([r['id'] for r in session.repos], [1, 2, 3])
        self.assertEqual([c.kwargs['page'] for c in search.call_args_list], [1, 2])
        self.assertFalse(session.has_more)

    def test_rate_limit_retains_partial_results_and_retries_same_page(self):
        session = DiscoverySession(['python'])
        with patch('discovery.search_one', side_effect=[page([1], True), SearchError('rate_limit', wait=30), page([2])]) as search:
            asyncio.run(session.load_more(42, 'token'))
            self.assertEqual(session.groups[0].page, 2)
            self.assertEqual(session.error, 'rate_limit')
            asyncio.run(session.load_more(42, 'token'))
            self.assertEqual(search.call_count, 2)
            session.retry_at = 0
            asyncio.run(session.load_more(42, 'token'))
        self.assertEqual([c.kwargs['page'] for c in search.call_args_list], [1, 2, 2])
        self.assertEqual([r['id'] for r in session.repos], [1, 2])

    def test_repeated_click_during_fetch_does_not_duplicate_request(self):
        session = DiscoverySession(['python'])

        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            async def fetch(*args, **kwargs):
                started.set()
                await release.wait()
                return page([1], True)

            with patch('discovery.search_one', side_effect=fetch) as search:
                task = asyncio.create_task(session.load_more(42, 'token', budget=1))
                await started.wait()
                await session.load_more(42, 'token', budget=1)
                release.set()
                await task
                self.assertEqual(search.call_count, 1)
        asyncio.run(scenario())

    def test_end_limit_empty_page_and_account_switch(self):
        session = DiscoverySession(['python'])
        with patch('discovery.search_one', return_value=page([], limited=True)):
            asyncio.run(session.load_more(42, 'token'))
        self.assertFalse(session.has_more)
        self.assertTrue(session.reached_limit)
        other = DiscoverySession(['tools'])
        with patch('discovery.search_one') as fetch:
            asyncio.run(other.load_more(42, 'token', is_current=lambda: False))
            fetch.assert_not_called()

    def test_page_limit_ends_group_instead_of_retrying_forever(self):
        session = DiscoverySession(['python'])
        with patch('discovery.search_one', side_effect=[page([1], True), SearchError('page_limit')]):
            asyncio.run(session.load_more(42, 'token', budget=2))
        self.assertFalse(session.has_more)
        self.assertTrue(session.reached_limit)
        self.assertEqual(session.error, 'page_limit')
        with patch('discovery.search_one') as fetch:
            asyncio.run(session.load_more(42, 'token'))
            fetch.assert_not_called()

    def test_new_batches_are_numbered_and_duplicates_do_not_create_new_batches(self):
        session = DiscoverySession(['python'])
        with patch('discovery.search_one', side_effect=[page([1, 2], True), page([2, 3]), page([3])]):
            asyncio.run(session.load_more(42, 'token', budget=1))
            self.assertEqual(session.batch, 1)
            self.assertEqual([repo['_batch'] for repo in session.repos], [1, 1])

            asyncio.run(session.load_more(42, 'token', budget=1))
            self.assertEqual(session.batch, 2)
            self.assertEqual([repo['_batch'] for repo in session.repos], [1, 1, 2])

            asyncio.run(session.load_more(42, 'token', budget=1))
            self.assertEqual(session.batch, 2)

        self.assertEqual([repo['id'] for repo in session.repos], [1, 2, 3])

    def test_groups_are_visited_fairly(self):
        session = DiscoverySession(['a', 'b', 'c', 'd', 'e'])
        with patch('discovery.search_one', return_value=page([1], True)) as fetch:
            asyncio.run(session.load_more(42, 'token', budget=3))
        self.assertEqual([c.kwargs['page'] for c in fetch.call_args_list], [1, 1, 2])
