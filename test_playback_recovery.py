import copy
import queue
import unittest
from unittest.mock import Mock, patch

import djamp
from test_djamp import Window


class TestPlaybackRecovery(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch("djamp.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.status = {"stopped": False, "paused": False, "buffering": False,
                       "context_uri": djamp.DJ_URI,
                       "track": {"uri": "spotify:track:" + "1" * 22, "name": "Desired track"}}
        self.posts = []
        self.post_error = None
        self.worker = djamp.PlayerWorker(Mock(request=self.request), autoplay=False)

    def request(self, path, payload=None, **kwargs):
        if kwargs.get("post"):
            self.posts.append((path, payload))
            if self.post_error:
                raise self.post_error
            return {}
        if path == "/":
            return {"playback_ready": True, "direct_dj": True}
        if path == "/status":
            return copy.deepcopy(self.status)
        if path == "/auth/code":
            return {}
        raise AssertionError(path)

    def fail(self, remaining=10000):
        self.status.update(stopped=True, paused=True, buffering=False,
                           playback_error={"kind": "audio_key_refused",
                                           "message": "Spotify refused the audio key.",
                                           "uri": "spotify:track:" + "1" * 22,
                                           "retry_after_ms": remaining})

    def drain(self):
        while True:
            try:
                command = self.worker.commands.get_nowait()
            except queue.Empty:
                return
            self.worker._process_command(*command)
            self.worker._poll()

    def render(self, **kwargs):
        window = Window(24, 58)
        djamp.render(djamp.Canvas(window), self.worker.snapshot(),
                     ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO"), **kwargs)
        return window.text()

    def test_failed_idle_status_consumes_autoplay_and_never_restarts_it(self):
        self.worker.autoplay_pending = True
        self.fail()
        self.status["track"] = None
        self.worker._poll()
        self.now += 15
        self.fail(0)
        self.worker._poll()
        self.status.pop("playback_error")
        self.worker._poll()
        self.assertFalse(self.worker.autoplay_pending)
        self.assertEqual(self.posts, [])

    def test_error_and_countdown_survive_polls_volume_and_new_ui_notices(self):
        self.fail()
        self.worker._poll()
        self.assertFalse(self.worker.command("/player/next"))
        self.worker._process_command("/player/volume", {"volume": 5, "relative": True})
        self.now += 2.1
        screen = self.render(notice="Opening your liked song…")
        self.assertIn("FAILED", screen)
        self.assertIn("Spotify refused the audio key.", screen)
        self.assertIn("Retry in 8s", screen)
        self.assertNotIn("Opening your liked song", screen)
        self.fail(7000)
        self.worker._poll()
        self.assertIn("Retry in 7s", self.render())
        self.assertEqual(self.posts, [("/player/volume", {"volume": 5, "relative": True})])

    def test_failed_state_overrides_stale_buffering_or_playing_flags(self):
        self.fail()
        self.status.update(stopped=False, paused=False, buffering=True)
        self.worker._poll()
        screen = self.render()
        self.assertIn("FAILED", screen)
        self.assertNotIn("BUFFERING", screen)
        self.assertNotIn("PLAYING", screen)
        status = {"track": {"position": 2000}, "playback_error": self.status["playback_error"]}
        self.assertEqual(djamp.playback_position(status, 100, now=101), 2000)

    def test_observed_failure_discards_old_playback_queue_but_keeps_volume(self):
        for path in ("/player/next", "/player/prev", "/player/playpause", "/player/seek"):
            self.worker.command(path)
        self.worker.command("/player/play", {"uri": djamp.DJ_URI})
        volume = {"volume": 20}
        self.worker.command("/player/volume", volume)
        self.fail()
        self.worker._poll()
        self.now += 11
        self.fail(0)
        self.worker._poll()
        self.drain()
        self.assertEqual(self.posts, [("/player/volume", volume)])
        self.assertFalse(self.worker.dj_requested)

    def test_failed_http_control_discards_backlog_even_before_status_error(self):
        self.worker.command("/player/next")
        self.worker.command("/player/play", {"uri": djamp.DJ_URI})
        self.worker.command("/player/volume", {"volume": 0})
        self.post_error = TimeoutError("Playback request timed out")
        self.worker._process_command("/player/next", None)
        self.post_error = None
        self.drain()
        self.assertEqual(self.posts, [("/player/next", None), ("/player/volume", {"volume": 0})])

    def test_cooldown_keys_are_discarded_and_expiry_requires_fresh_manual_retry(self):
        self.fail()
        self.worker._poll()
        for path, payload in (("/player/next", None), ("/player/playpause", None),
                              ("/player/play", {"uri": djamp.DJ_URI})):
            self.assertFalse(self.worker.command(path, payload))
            self.worker._process_command(path, payload)
        self.now += 10
        self.fail(0)
        self.worker._poll()
        self.drain()
        self.assertEqual(self.posts, [])
        self.assertIn("Press Space to retry this track.", self.render())
        self.assertTrue(self.worker.command("/player/playpause"))
        self.drain()
        self.assertEqual(self.posts, [("/player/playpause", None)])
        self.assertIn("FAILED", self.render(), "An accepted request does not confirm recovery")

    def test_fresh_dj_start_is_allowed_after_cooldown(self):
        self.fail()
        self.worker._poll()
        self.now += 11
        self.fail(0)
        self.worker._poll()
        self.assertTrue(self.worker.command("/player/play", {"uri": djamp.DJ_URI}))
        self.drain()
        self.assertEqual(self.posts, [("/player/play", {"uri": djamp.DJ_URI})])

    def test_successful_load_clears_error_and_unblocks_controls(self):
        self.fail()
        self.worker._poll()
        self.status.pop("playback_error")
        self.status.update(stopped=False, paused=False)
        self.worker._poll()
        self.assertIn("PLAYING", self.render())
        self.assertNotIn("FAILED", self.render())
        self.assertNotIn("Retry in", self.render())
        self.assertTrue(self.worker.command("/player/next"))
        self.drain()
        self.assertEqual(self.posts, [("/player/next", None)])

    def test_active_retry_keeps_failure_reason_without_inviting_another_retry(self):
        self.fail(0)
        self.status["buffering"] = True
        self.worker._poll()
        screen = self.render(notice="Opening Spotify link…")
        self.assertIn("RETRYING", screen)
        self.assertIn("Retrying selected track…", screen)
        self.assertIn("Spotify refused the audio key.", screen)
        self.assertNotIn("Press Space", screen)

    def test_later_disconnect_keeps_cause_but_does_not_offer_unavailable_retry(self):
        self.fail(0)
        self.worker._poll()
        self.worker.state.update(connected=False, error="Connection refused")
        screen = self.render(notice="Player stopped. Restart djamp.")
        self.assertIn("CONNECTING", screen)
        self.assertIn("Spotify refused the audio key.", screen)
        self.assertIn("Waiting for the player to reconnect", screen)
        self.assertIn("Player stopped. Restart djamp.", screen)
        self.assertNotIn("Press Space", screen)
        self.assertIn("Connection refused", self.render())

    def test_failed_dj_start_discards_commands_accumulated_during_request(self):
        def request(path, payload=None, **kwargs):
            if kwargs.get("post") and path == "/player/play":
                self.worker.command("/player/next")
                self.worker.command("/player/playpause")
                self.worker.command("/player/volume", {"volume": 0})
                self.posts.append((path, payload))
                raise TimeoutError("DJ request timed out")
            return self.request(path, payload, **kwargs)

        self.worker.api.request = request
        self.worker._process_command("/player/play", {"uri": djamp.DJ_URI})
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["dj_starting"])
        self.drain()
        self.assertEqual(self.posts, [("/player/play", {"uri": djamp.DJ_URI}),
                                     ("/player/volume", {"volume": 0})])

    def test_normal_multiple_next_commands_are_preserved(self):
        for _ in range(4):
            self.assertTrue(self.worker.command("/player/next"))
        self.drain()
        self.assertEqual(self.posts, [("/player/next", None)] * 4)

    def test_duplicate_dj_starts_are_blocked_while_queued_or_loading(self):
        self.status.update(stopped=True, track=None)
        for index in range(10):
            self.assertEqual(self.worker.command("/player/play", {"uri": djamp.DJ_URI}), index == 0)
        self.drain()
        self.assertFalse(self.worker.command("/player/play", {"uri": djamp.DJ_URI}))
        self.worker._process_command("/player/play", {"uri": djamp.DJ_URI})
        self.worker._poll()
        self.assertEqual(self.posts, [("/player/play", {"uri": djamp.DJ_URI})])
        self.status.update(stopped=False, track={"name": "Loaded track"})
        self.worker._poll()
        self.assertTrue(self.worker.command("/player/play", {"uri": djamp.DJ_URI}))


if __name__ == "__main__":
    unittest.main()
