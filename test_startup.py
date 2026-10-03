import copy
import threading
import unittest
from unittest.mock import Mock, patch

import djamp


class TestDJStartup(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = patch("djamp.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.root = {"playback_ready": True, "direct_dj": True}
        self.status = {"stopped": True, "paused": False, "buffering": False, "track": None}
        self.posts = []
        self.post_error = None
        self.api = Mock()
        self.api.request.side_effect = self.request

    def request(self, path, payload=None, **kwargs):
        if kwargs.get("post"):
            self.posts.append((path, payload))
            if self.post_error is not None:
                raise self.post_error
            return {}
        if path == "/":
            return copy.deepcopy(self.root)
        if path == "/status":
            return copy.deepcopy(self.status)
        if path == "/auth/code":
            return {} if self.root["playback_ready"] else {"code": "TEST"}
        raise AssertionError(f"Unexpected API request: {path}")

    def assert_dj_posts(self, count):
        self.assertEqual(self.posts, [("/player/play", {"uri": djamp.DJ_URI})] * count)

    def test_waits_for_authentication_then_starts_once(self):
        worker = djamp.PlayerWorker(self.api)
        self.root["playback_ready"] = False
        worker._poll()
        self.assert_dj_posts(0)
        self.assertEqual(worker.snapshot()["auth"], {"code": "TEST"})

        self.root["playback_ready"] = True
        worker._poll()
        self.assert_dj_posts(1)
        self.assertTrue(worker.snapshot()["dj_starting"])

        # A successful HTTP response only acknowledges the request; keep the
        # loading indication until a playback poll confirms a loaded track.
        worker._poll()
        self.assertTrue(worker.snapshot()["dj_starting"])
        self.status.update(stopped=False, track={"uri": "spotify:track:" + "1" * 22})
        worker._poll()
        self.assertTrue(worker.snapshot()["dj_starting"], "An unrelated track is not DJ startup confirmation")
        self.status["context_uri"] = djamp.DJ_URI
        worker._poll()
        self.assertFalse(worker.snapshot()["dj_starting"])

        # Finishing playback or reconnecting must not cause another auto start.
        self.status.update(stopped=True, track=None)
        worker._poll()
        self.root["playback_ready"] = False
        worker._poll()
        self.root["playback_ready"] = True
        worker._poll()
        self.assert_dj_posts(1)

    def test_preserves_existing_playing_or_paused_track(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                worker = djamp.PlayerWorker(self.api)
                self.status.update(stopped=False, paused=paused,
                                   track={"uri": "spotify:track:" + "1" * 22})
                worker._poll()
                self.status.update(stopped=True, paused=False, track=None)
                worker._poll()
                self.assert_dj_posts(0)

    def test_pairing_code_does_not_wait_for_blocked_player_endpoints(self):
        worker = djamp.PlayerWorker(self.api)
        self.root["playback_ready"] = False

        def waiting_for_auth(path, payload=None, **kwargs):
            if path in ("/", "/status"):
                raise TimeoutError("Player endpoints wait for authentication")
            return self.request(path, payload, **kwargs)

        self.api.request.side_effect = waiting_for_auth
        worker._poll()
        self.assertFalse(worker.snapshot()["ready"])
        self.assertEqual(worker.snapshot()["auth"], {"code": "TEST"})
        self.assertEqual([call.args[0] for call in self.api.request.call_args_list], ["/auth/code"])
        self.assert_dj_posts(0)

        self.root["playback_ready"] = True
        self.api.request.side_effect = self.request
        worker._poll()
        self.assertTrue(worker.snapshot()["ready"])
        self.assert_dj_posts(1)

    def test_never_starts_without_a_known_idle_status(self):
        for status in ({}, {"stopped": True, "buffering": True},
                       {"stopped": True, "paused": True},
                       {"stopped": False, "track": None}):
            with self.subTest(status=status):
                worker = djamp.PlayerWorker(self.api)
                self.status = status
                worker._poll()
                self.assert_dj_posts(0)

    def test_no_autoplay_still_allows_explicit_start(self):
        worker = djamp.PlayerWorker(self.api, autoplay=False)
        worker._poll()
        worker._poll()
        self.assert_dj_posts(0)
        worker._process_command("/player/play", {"uri": djamp.DJ_URI})
        worker._poll()
        self.assert_dj_posts(1)

    def test_manual_start_waits_for_authentication(self):
        worker = djamp.PlayerWorker(self.api, autoplay=False)
        self.root["playback_ready"] = False
        worker._process_command("/player/play", {"uri": djamp.DJ_URI})
        worker._poll()
        self.assert_dj_posts(0)
        self.root["playback_ready"] = True
        worker._poll()
        self.assert_dj_posts(1)

    def test_explicit_playback_choice_cancels_startup_autoplay(self):
        worker = djamp.PlayerWorker(self.api)
        ordinary = {"uri": "spotify:track:" + "2" * 22}
        worker._process_command("/player/play", ordinary)
        worker._poll()
        worker._poll()
        self.assertEqual(self.posts, [("/player/play", ordinary)])

    def test_volume_preserves_autoplay_while_waiting_for_authentication(self):
        worker = djamp.PlayerWorker(self.api)
        self.root["playback_ready"] = False
        worker._poll()
        volume = {"volume": 5, "relative": True}
        worker._process_command("/player/volume", volume)
        worker._poll()
        self.assertEqual(self.posts, [("/player/volume", volume)])
        self.root["playback_ready"] = True
        worker._poll()
        self.assertEqual(self.posts, [("/player/volume", volume),
                                     ("/player/play", {"uri": djamp.DJ_URI})])

    def test_volume_preserves_manual_dj_request_and_waiting_notice(self):
        worker = djamp.PlayerWorker(self.api, autoplay=False)
        self.root["playback_ready"] = False
        worker._process_command("/player/play", {"uri": djamp.DJ_URI})
        worker._poll()
        waiting_notice = worker.snapshot()["notice"]
        self.assertTrue(waiting_notice)
        volume = {"volume": 0}
        worker._process_command("/player/volume", volume)
        self.assertEqual(worker.snapshot()["notice"], waiting_notice)
        self.assertTrue(worker.snapshot()["dj_starting"])
        self.root["playback_ready"] = True
        worker._poll()
        self.assertEqual(self.posts, [("/player/volume", volume),
                                     ("/player/play", {"uri": djamp.DJ_URI})])

    def test_volume_preserves_loading_notice_and_original_startup_timeout(self):
        worker = djamp.PlayerWorker(self.api)
        worker._poll()
        starting_notice = worker.snapshot()["notice"]
        self.assertTrue(starting_notice)
        self.now += 20
        volume = {"volume": -5, "relative": True}
        worker._process_command("/player/volume", volume)
        self.assertEqual(worker.snapshot()["notice"], starting_notice)
        self.assertTrue(worker.snapshot()["dj_starting"])
        self.now += 11
        worker._poll()
        self.assertFalse(worker.snapshot()["dj_starting"])
        self.assertIn("did not start", worker.snapshot()["notice"])
        self.assertEqual(self.posts, [("/player/play", {"uri": djamp.DJ_URI}),
                                     ("/player/volume", volume)])

    def test_missing_patch_blocks_dj_and_explains_how_to_recover(self):
        for capability in (None, False):
            with self.subTest(capability=capability):
                worker = djamp.PlayerWorker(self.api)
                self.root.pop("direct_dj", None)
                if capability is not None:
                    self.root["direct_dj"] = capability
                worker._poll()
                worker._process_command("/player/play", {"uri": djamp.DJ_URI})
                worker._poll()
                self.assert_dj_posts(0)
                notice = worker.snapshot()["notice"].lower()
                self.assertTrue("backend" in notice or "player" in notice, notice)
                self.assertTrue("patch" in notice or "restart" in notice, notice)

        # Installing the DJ extension is not required for ordinary playback.
        ordinary = {"uri": "spotify:track:" + "2" * 22}
        worker._process_command("/player/play", ordinary)
        self.assertEqual(self.posts, [("/player/play", ordinary)])

    def test_failed_start_is_visible_and_is_not_retried_automatically(self):
        worker = djamp.PlayerWorker(self.api)
        self.post_error = TimeoutError("DJ request timed out")
        worker._poll()
        self.assert_dj_posts(1)
        self.assertIn("timed out", worker.snapshot()["notice"])
        self.assertFalse(worker.snapshot()["dj_starting"])
        self.post_error = None
        worker._poll()
        self.assert_dj_posts(1)

    def test_accepted_but_still_idle_start_times_out_without_retry(self):
        worker = djamp.PlayerWorker(self.api)
        worker._poll()
        self.assert_dj_posts(1)
        self.now += 31
        worker._poll()
        self.assertFalse(worker.snapshot()["dj_starting"])
        notice = worker.snapshot()["notice"].lower()
        self.assertTrue("failed" in notice or "tim" in notice or "did not" in notice, notice)
        worker._poll()
        self.assert_dj_posts(1)


class TestDJCommandQueue(unittest.TestCase):
    def test_queued_manual_start_reaches_backend_with_autoplay_disabled(self):
        posted = threading.Event()
        requests = []

        def request(path, payload=None, **kwargs):
            if kwargs.get("post"):
                requests.append((path, payload))
                posted.set()
                return {}
            if path == "/":
                return {"playback_ready": True, "direct_dj": True}
            if path == "/status":
                return {"stopped": True, "buffering": False, "track": None}
            if path == "/auth/code":
                return {}
            raise AssertionError(f"Unexpected API request: {path}")

        worker = djamp.PlayerWorker(Mock(request=request), autoplay=False)
        worker.command("/player/play", {"uri": djamp.DJ_URI})
        worker.start()
        try:
            self.assertTrue(posted.wait(1.5), "Queued DJ request did not reach the backend")
        finally:
            worker.stop_event.set()
            worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(requests, [("/player/play", {"uri": djamp.DJ_URI})])


if __name__ == "__main__":
    unittest.main()
