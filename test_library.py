import json
import threading
import unittest
from unittest.mock import Mock

from djamp_library import (BackendLibrary, LibraryError, LibraryWorker, PAGE_SIZE,
                           normalize_page)


SONG = 'spotify:track:' + 'a' * 22
OTHER = 'spotify:track:' + 'b' * 22


def page(offset=0, total=1, uri=SONG):
    return {'items': [{'uri': uri, 'name': 'A song', 'artist_names': ['An artist'],
                      'album_name': 'An album', 'duration': 1000, 'playable': True}],
            'offset': offset, 'limit': PAGE_SIZE, 'total': total,
            'has_next': offset + 1 < total}


class Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self.body = json.dumps(data).encode() if data is not None else b''

    def read(self, size):
        return self.body[:size]


class Connections:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = 0
        self.sock = None

    def __call__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        return self

    def request(self, method, path, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed += 1


class TestTransport(unittest.TestCase):
    def client(self, *responses):
        self.api = Mock(port=3678)
        self.api.request.return_value = {'library': True}
        self.connections = Connections(*responses)
        client = BackendLibrary(self.api, connection_factory=self.connections)
        client.reset('one')
        return client

    def test_page_uses_loopback_and_own_timeout_with_no_token(self):
        client = self.client(Response(data=page(total=100)))
        result = client.page(0, PAGE_SIZE, lambda: True)
        self.assertTrue(result['has_next'])
        self.assertEqual(result['items'][0]['artist_names'], ['An artist'])
        self.assertEqual(self.connections.host, '127.0.0.1')
        self.assertEqual(self.connections.port, 3678)
        self.assertEqual(self.connections.requests[0][:2],
                         ('GET', '/library/tracks?offset=0&limit=20&refresh=false&username=one'))
        self.assertEqual(self.connections.timeout, 30)
        self.api.request.assert_called_once_with('/')
        self.assertNotIn('Authorization', self.connections.requests[0][3])

    def test_save_is_confirmed_and_bound_to_captured_account(self):
        client = self.client(Response(data={'saved': False}), Response(data={'saved': True}),
                             Response(data={'saved': False}))
        self.assertFalse(client.contains(SONG, lambda: True))
        client.set_saved(SONG, True, lambda: True)
        client.set_saved(SONG, False, lambda: True)
        self.assertEqual([r[0] for r in self.connections.requests], ['GET', 'POST', 'POST'])
        self.assertTrue(self.connections.requests[0][1].startswith('/library/contains?uri=spotify%3Atrack%3A'))
        self.assertTrue(self.connections.requests[0][1].endswith('&username=one'))
        self.assertEqual(json.loads(self.connections.requests[1][2]),
                         {'uri': SONG, 'saved': True, 'username': 'one'})
        self.assertEqual(json.loads(self.connections.requests[2][2])['saved'], False)

    def test_old_backend_has_actionable_error_without_requesting_tokens(self):
        client = self.client()
        self.api.request.return_value = {'direct_dj': True}
        with self.assertRaisesRegex(LibraryError, 'make backend'):
            client.contains(SONG, lambda: True)
        self.assertEqual(self.connections.requests, [])
        self.api.request.assert_called_once_with('/')

    def test_account_change_is_visible_without_retrying_write(self):
        client = self.client(Response(409))
        with self.assertRaisesRegex(LibraryError, 'account changed'):
            client.set_saved(SONG, True, lambda: True)
        self.assertEqual(len(self.connections.requests), 1)

    def test_errors_do_not_leak_backend_body_or_repeat_mutation(self):
        client = self.client(Response(500, {'error': 'secret-token-private-track'}))
        with self.assertRaisesRegex(LibraryError, 'HTTP 500') as caught:
            client.set_saved(SONG, True, lambda: True)
        self.assertNotIn('secret-token', str(caught.exception))
        self.assertEqual(len(self.connections.requests), 1)

    def test_unconfirmed_write_is_not_a_success(self):
        client = self.client(Response(data={'saved': False}))
        with self.assertRaisesRegex(LibraryError, 'did not confirm'):
            client.set_saved(SONG, True, lambda: True)

    def test_cancelled_job_never_contacts_backend(self):
        client = self.client()
        with self.assertRaises(LibraryError):
            client.set_saved(SONG, True, lambda: False)
        self.api.request.assert_not_called()
        self.assertEqual(self.connections.requests, [])

    def test_invalid_contains_or_nontrack_does_not_become_liked(self):
        client = self.client(Response(data={'saved': 'true'}))
        with self.assertRaisesRegex(LibraryError, 'invalid like status'):
            client.contains(SONG, lambda: True)
        with self.assertRaisesRegex(LibraryError, 'Only Spotify songs'):
            client.set_saved('spotify:episode:' + 'c' * 22, True, lambda: True)
        self.assertEqual(len(self.connections.requests), 1)

    def test_unavailable_tracks_and_bad_page_shape(self):
        raw = page(total=3)
        raw['items'] += [{'uri': '', 'playable': False}, {'uri': OTHER, 'playable': False}]
        result = normalize_page(raw, 0, PAGE_SIZE)
        self.assertEqual([row['playable'] for row in result['items']], [True, False, False])
        for change in ({'offset': 20}, {'items': 'invalid'}, {'total': True}, {'limit': 100}):
            with self.subTest(change=change), self.assertRaises(LibraryError):
                normalize_page(dict(raw, **change), 0, PAGE_SIZE)


class FakeTransport:
    def __init__(self):
        self.saved = False
        self.writes = []
        self.accounts = []
        self.pages = []
        self.refreshes = []
        self.on_page = None
        self.on_contains = None
        self.on_write = None
        self.closed = False

    def reset(self, username):
        self.accounts.append(username)

    def close(self):
        self.closed = True

    def page(self, offset, limit, valid, *, refresh=False):
        self.pages.append(offset)
        self.refreshes.append(refresh)
        if self.on_page:
            return self.on_page(offset)
        return normalize_page(page(offset, offset + 1), offset, limit)

    def contains(self, uri, valid):
        if self.on_contains:
            self.on_contains(uri)
        return self.saved

    def set_saved(self, uri, saved, valid):
        if self.on_write:
            self.on_write(uri, saved)
        self.writes.append((uri, saved))
        self.saved = saved


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.client = FakeTransport()
        self.worker = LibraryWorker(None, transport=self.client)
        self.worker.observe('one', SONG, True)

    def drain(self):
        while self.worker.commands:
            self.worker._perform(self.worker.commands.popleft())

    def test_like_is_confirmed_captures_song_and_repeated_key_is_ignored(self):
        self.drain()
        self.assertFalse(self.worker.snapshot()['liked'])
        self.worker.toggle(SONG)
        self.worker.toggle(SONG)
        self.assertFalse(self.worker.snapshot()['liked'])
        self.assertTrue(self.worker.snapshot()['like_pending'])
        self.worker.observe('one', OTHER, True)
        self.drain()
        self.assertEqual(self.client.writes, [(SONG, True)])
        self.assertFalse(self.worker.snapshot()['like_pending'])
        self.assertEqual(self.worker.snapshot()['current_uri'], OTHER)

    def test_failed_like_does_not_optimistically_update_or_retry(self):
        self.drain()
        self.client.on_write = Mock(side_effect=LibraryError('Denied.'))
        self.worker.toggle(SONG)
        self.drain()
        state = self.worker.snapshot()
        self.assertFalse(state['liked'])
        self.assertFalse(state['like_pending'])
        self.assertEqual(state['error'], 'Denied.')
        self.assertEqual(self.client.writes, [])

    def test_account_change_discards_pending_old_account_mutation(self):
        self.worker.toggle(SONG)
        self.worker.observe('two', OTHER, True)
        self.drain()
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.client.accounts, ['two'])
        self.assertEqual(self.worker.snapshot()['items'], [])

    def test_account_change_during_contains_prevents_write(self):
        self.drain()
        self.client.on_contains = lambda uri: self.worker.observe('two', '', True)
        self.worker.toggle(SONG)
        self.drain()
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.worker.snapshot()['like_pending'])

    def test_stale_account_page_never_publishes(self):
        def account_changed(offset):
            self.worker.observe('two', '', True)
            return normalize_page(page(), 0, PAGE_SIZE)
        self.client.on_page = account_changed
        self.worker.browse()
        self.drain()
        self.assertEqual(self.worker.snapshot()['items'], [])
        self.assertFalse(self.worker.snapshot()['loaded'])

    def test_new_page_request_supersedes_old_inflight_result(self):
        def navigate(offset):
            self.worker.browse(20)
            return normalize_page(page(), 0, PAGE_SIZE)
        self.drain()
        self.worker.browse(0)
        first = self.worker.commands.popleft()
        self.client.on_page = navigate
        self.worker._perform(first)
        self.assertEqual(self.worker.snapshot()['items'], [])
        self.client.on_page = None
        self.drain()
        self.assertEqual(self.worker.snapshot()['offset'], 20)

    def test_reads_coalesce_and_write_refreshes_current_page(self):
        self.worker.browse(0)
        self.worker.browse(20)
        self.worker.browse(40)
        self.assertEqual(sum(job[0] == 'page' for job in self.worker.commands), 1)
        self.drain()
        self.worker.toggle(SONG)
        self.drain()
        self.assertEqual(self.client.pages, [40, 40])
        self.assertTrue(self.worker.snapshot()['liked'])
        self.assertTrue(self.worker.snapshot()['loaded'])

    def test_only_explicit_refresh_bypasses_backend_collection_cache(self):
        self.worker.browse()
        self.drain()
        self.worker.browse(20)
        self.drain()
        self.worker.browse(20, refresh=True)
        self.drain()
        self.assertEqual(self.client.refreshes, [False, False, True])

    def test_refresh_updates_like_status_after_song_is_removed_elsewhere(self):
        self.client.saved = True
        self.worker.browse()
        self.drain()
        self.assertTrue(self.worker.snapshot()['liked'])

        calls = []

        def refreshed_page(offset):
            calls.append('page')
            self.client.saved = False
            return dict(page(), items=[], total=0, has_next=False)

        self.client.on_page = refreshed_page
        self.client.on_contains = lambda uri: calls.append(('contains', uri))
        self.worker.browse(refresh=True)
        self.drain()
        state = self.worker.snapshot()
        self.assertEqual(calls, ['page', ('contains', SONG)])
        self.assertEqual(state['items'], [])
        self.assertFalse(state['liked'])
        self.assertEqual(self.client.writes, [])

    def test_refreshed_like_check_does_not_publish_for_a_previous_track_or_account(self):
        for username, uri in (('one', OTHER), ('two', SONG)):
            with self.subTest(username=username, uri=uri):
                self.setUp()
                self.drain()
                self.worker.browse(refresh=True)
                self.worker._perform(self.worker.commands.popleft())
                check = self.worker.commands.popleft()
                self.assertEqual(check[0], 'check')

                self.client.saved = True
                self.client.on_contains = lambda _: self.worker.observe(username, uri, True)
                self.worker._perform(check)
                self.assertEqual(self.worker.snapshot()['current_uri'], uri)
                self.assertIsNone(self.worker.snapshot()['liked'])

                self.client.on_contains = None
                self.client.saved = False
                self.drain()
                self.assertFalse(self.worker.snapshot()['liked'])
                self.assertEqual(self.client.accounts[-1], username)
                self.assertEqual(self.client.writes, [])

    def test_superseded_refreshed_page_checks_the_current_track(self):
        self.drain()

        def navigate_and_change_track(offset):
            self.worker.observe('one', OTHER, True)
            self.worker.browse(20)
            self.client.saved = True
            return normalize_page(page(), 0, PAGE_SIZE)

        self.client.on_page = navigate_and_change_track
        self.worker.browse(refresh=True)
        self.worker._perform(self.worker.commands.popleft())
        self.assertEqual(self.worker.snapshot()['items'], [])
        self.assertEqual([job[4] for job in self.worker.commands if job[0] == 'check'], [OTHER])
        self.client.on_page = None
        self.drain()
        self.assertEqual(self.worker.snapshot()['offset'], 20)
        self.assertEqual(self.worker.snapshot()['current_uri'], OTHER)
        self.assertTrue(self.worker.snapshot()['liked'])
        self.assertEqual(self.client.writes, [])

    def test_remove_last_item_moves_back_one_page(self):
        def pages(offset):
            raw = page(offset, 20)
            if offset == 20:
                raw['items'] = []
            return normalize_page(raw, offset, PAGE_SIZE)
        self.client.on_page = pages
        self.worker.browse(20)
        self.drain()
        self.assertEqual(self.client.pages, [20, 0])
        self.assertEqual(self.worker.snapshot()['offset'], 0)

    def test_snapshots_are_isolated_and_close_discards_queued_writes(self):
        self.worker.browse()
        self.drain()
        snapshot = self.worker.snapshot()
        snapshot['items'][0]['name'] = 'mutated'
        self.assertNotEqual(self.worker.snapshot()['items'][0]['name'], 'mutated')
        self.worker.toggle(SONG)
        self.worker.close()
        self.assertTrue(self.client.closed)
        self.assertEqual(list(self.worker.commands), [])
        self.assertEqual(self.client.writes, [])

    def test_worker_thread_wakes_and_closes_without_playback_worker(self):
        completed = threading.Event()
        self.client.on_contains = lambda uri: completed.set()
        self.worker.start()
        self.assertTrue(completed.wait(1))
        self.worker.close()
        self.assertFalse(self.worker.is_alive())


if __name__ == '__main__':
    unittest.main()
