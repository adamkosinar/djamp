import copy
import unittest
from unittest.mock import Mock, patch

import djamp


class TestLikedShuffleWorker(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch("djamp.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.username = "test-user"
        self.collection = f"spotify:user:{self.username}:collection"
        self.uri = "spotify:track:" + "1" * 22
        self.status = {"username": self.username, "context_uri": djamp.DJ_URI,
                       "stopped": False, "paused": False, "buffering": False,
                       "shuffle_context": False, "track": {"uri": self.uri}}
        self.posts = []
        self.post_error = None
        self.auth = {}
        self.ready = True
        self.supported = True
        self.worker = djamp.PlayerWorker(Mock(request=self.request), autoplay=False)
        self.worker._poll()

    def request(self, path, payload=None, **kwargs):
        if kwargs.get("post"):
            self.posts.append((path, copy.deepcopy(payload)))
            if self.post_error:
                raise self.post_error
            if path == "/player/play":
                # The backend acknowledges after claiming the new context,
                # before loading the selected track has necessarily finished.
                self.status.update(context_uri=payload["uri"], buffering=True)
            return None
        if path == "/status":
            return copy.deepcopy(self.status)
        if path == "/auth/code":
            return copy.deepcopy(self.auth)
        if path == "/":
            return {"playback_ready": self.ready, "direct_dj": True, "liked_shuffle": self.supported}
        raise AssertionError(path)

    def process(self, *, poll=True):
        self.worker._process_command(*self.worker.commands.get_nowait())
        if poll:
            self.worker._poll()

    def active(self, shuffle=False):
        self.status.update(context_uri=self.collection, shuffle_context=shuffle,
                           buffering=False, stopped=False)
        self.worker._poll()

    def enable_preference(self):
        self.assertTrue(self.worker.shuffle_liked(self.username))
        self.process()
        self.assertTrue(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertEqual(self.posts, [])

    def start_liked(self, *, loaded=False):
        self.assertTrue(self.worker.play_liked(self.username, self.uri))
        self.assertTrue(self.worker.snapshot()["liked_shuffle_pending"])
        self.process()
        if loaded:
            self.status["buffering"] = False
            self.worker._poll()

    def test_passive_preference_does_not_shuffle_or_cancel_dj_startup(self):
        self.worker.autoplay_pending = True
        self.worker.dj_requested = True
        self.worker.state.update(dj_starting=True, notice="Waiting for Spotify to start DJ X…")
        self.assertTrue(self.worker.shuffle_liked(self.username))
        self.process(poll=False)
        self.assertTrue(self.worker.autoplay_pending)
        self.assertTrue(self.worker.dj_requested)
        self.assertTrue(self.worker.snapshot()["dj_starting"])
        self.assertEqual(self.worker.snapshot()["notice"], "Waiting for Spotify to start DJ X…")
        self.assertEqual(self.posts, [])
        self.assertTrue(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])

    def test_toggle_is_visible_on_acceptance_and_rapid_toggles_use_last_value(self):
        self.active()
        self.assertTrue(self.worker.shuffle_liked(self.username))
        self.assertTrue(self.worker.snapshot()["liked_shuffle"])
        self.assertTrue(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertTrue(self.worker.shuffle_liked(self.username))
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.process()
        self.process()
        self.assertEqual(self.posts, [])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])

    def test_play_waits_for_loaded_collection_then_preserves_selected_start(self):
        self.enable_preference()
        self.start_liked()
        self.assertEqual(self.posts, [("/player/play", {"uri": self.collection, "skip_to_uri": self.uri})])
        self.assertTrue(self.worker.snapshot()["liked_shuffle_pending"])
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(self.posts[-1], ("/player/shuffle_context", {"shuffle_context": True, "context_uri": self.collection}))
        self.assertEqual(self.status["track"]["uri"], self.uri)
        self.assertTrue(self.worker.snapshot()["liked_shuffle_pending"])
        self.status["shuffle_context"] = True
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])

    def test_default_off_is_applied_after_inheriting_another_contexts_shuffle(self):
        self.status["shuffle_context"] = True
        self.worker._poll()
        self.start_liked(loaded=True)
        self.assertEqual(self.posts[-1], ("/player/shuffle_context", {"shuffle_context": False, "context_uri": self.collection}))

    def test_active_toggle_follows_backend_confirmation_and_later_revert(self):
        self.active()
        self.worker.shuffle_liked(self.username)
        self.process()
        self.assertEqual(self.posts, [("/player/shuffle_context", {"shuffle_context": True, "context_uri": self.collection})])
        self.worker._poll()
        self.assertEqual(len(self.posts), 1, "An unconfirmed toggle must not be retried on every poll")
        self.status["shuffle_context"] = True
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.status["shuffle_context"] = False
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])

    def test_active_off_toggle_preserves_paused_playback(self):
        self.active(True)
        self.status["paused"] = True
        self.worker.shuffle_liked(self.username)
        self.process()
        self.assertEqual(self.posts, [("/player/shuffle_context", {"shuffle_context": False, "context_uri": self.collection})])

    def test_fresh_status_prevents_queued_toggle_from_affecting_dj(self):
        self.active()
        self.worker.shuffle_liked(self.username)
        self.status["context_uri"] = djamp.DJ_URI
        self.process()
        self.assertEqual(self.posts, [])
        self.assertTrue(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])

    def test_new_dj_choice_cancels_deferred_apply_before_queued_dj_runs(self):
        self.enable_preference()
        self.start_liked()
        self.worker.command("/player/play", {"uri": djamp.DJ_URI})
        self.status["buffering"] = False
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.process()
        self.assertEqual([path for path, _ in self.posts], ["/player/play", "/player/play"])
        self.assertEqual(self.posts[-1][1], {"uri": djamp.DJ_URI})

    def test_liked_play_queued_after_dj_still_runs(self):
        self.enable_preference()
        self.worker.command("/player/play", {"uri": djamp.DJ_URI})
        self.worker.play_liked(self.username, self.uri)
        self.process()
        self.process()
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(self.posts[-1], ("/player/shuffle_context", {"shuffle_context": True, "context_uri": self.collection}))
        self.assertEqual(self.posts[-2][1], {"uri": self.collection, "skip_to_uri": self.uri})

    def test_new_ordinary_play_cancels_deferred_collection_preference(self):
        self.enable_preference()
        self.start_liked()
        self.worker.command("/player/play", {"uri": "spotify:track:" + "2" * 22})
        self.status["buffering"] = False
        self.worker._poll()
        self.process()
        self.assertNotIn("/player/shuffle_context", [path for path, _ in self.posts])

    def test_external_context_switch_cancels_even_if_collection_returns_later(self):
        self.enable_preference()
        self.start_liked()
        self.status.update(context_uri=djamp.DJ_URI, buffering=False)
        self.worker._poll()
        self.active()
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])

    def test_account_change_resets_preference_and_rejects_old_queued_play(self):
        self.enable_preference()
        self.worker.play_liked(self.username, self.uri)
        self.status["username"] = "other-user"
        self.process()
        self.assertEqual(self.posts, [])
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertFalse(self.worker.shuffle_liked(self.username))

    def test_fresh_account_change_rejects_stale_ui_before_next_poll(self):
        self.worker.shuffle_liked(self.username)
        self.status["username"] = "other-user"
        self.process(poll=False)
        self.assertEqual(self.worker.snapshot()["status"]["username"], self.username)
        self.assertFalse(self.worker.shuffle_liked(self.username))
        self.assertFalse(self.worker.play_liked(self.username, self.uri))
        self.assertEqual(self.posts, [])

    def test_cooldown_rejects_toggle_and_play_without_changing_preference(self):
        self.status["playback_error"] = {"retry_after_ms": 10000}
        self.worker._poll()
        self.assertFalse(self.worker.shuffle_liked(self.username))
        self.assertFalse(self.worker.play_liked(self.username, self.uri))
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertTrue(self.worker.commands.empty())

    def test_full_queue_does_not_change_desired_preference(self):
        for _ in range(32):
            self.worker.command("/player/volume", {"volume": 0})
        self.assertFalse(self.worker.shuffle_liked(self.username))
        self.assertFalse(self.worker.play_liked(self.username, self.uri))
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertIn("busy", self.worker.snapshot()["notice"])

    def test_playback_error_cancels_pending_and_discards_queued_controls(self):
        self.enable_preference()
        self.start_liked()
        self.worker.command("/player/next")
        self.worker.command("/player/volume", {"volume": 0})
        self.status["playback_error"] = {"retry_after_ms": 10000}
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertEqual(self.worker.commands.get_nowait(), ("/player/volume", {"volume": 0}))
        self.assertTrue(self.worker.commands.empty())

    def test_pairing_cancels_pending_and_resets_account_preference(self):
        self.enable_preference()
        self.start_liked()
        self.auth = {"code": "PAIRING"}
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.auth = {}
        self.active()
        self.assertEqual(len(self.posts), 1)

    def assert_bounded_confirmation(self, *, loaded, elapsed):
        self.enable_preference()
        self.start_liked(loaded=loaded)
        count = len(self.posts)
        self.now += elapsed
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertIn("Could not confirm", self.worker.snapshot()["notice"])
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(len(self.posts), count)

    def test_deferred_play_is_bounded(self):
        self.assert_bounded_confirmation(loaded=False, elapsed=31)

    def test_toggle_confirmation_is_bounded_and_successful_retry_clears_notice(self):
        self.assert_bounded_confirmation(loaded=True, elapsed=11)
        self.worker.shuffle_liked(self.username)
        self.process()
        self.status["shuffle_context"] = True
        self.worker._poll()
        self.assertEqual(self.worker.snapshot()["notice"], "")

    def test_disconnect_cancels_pending_without_reapplying_after_reconnect(self):
        self.enable_preference()
        self.start_liked()

        def disconnected(*args, **kwargs):
            self.worker.stop_event.set()
            raise ConnectionError("backend disconnected")

        self.worker.api.request = disconnected
        self.worker.run()
        self.assertFalse(self.worker.snapshot()["connected"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.worker.api.request = self.request
        self.worker.stop_event.clear()
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(len(self.posts), 1)

    def test_unsupported_backend_explains_toggle_and_still_plays_liked_song(self):
        self.supported = False
        self.worker._poll()
        self.assertFalse(self.worker.shuffle_liked(self.username))
        self.assertIn("Update", self.worker.snapshot()["notice"])
        self.assertTrue(self.worker.play_liked(self.username, self.uri))
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.process()
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(self.posts, [("/player/play", {"uri": self.collection, "skip_to_uri": self.uri})])

    def test_lost_capability_resets_preference_and_pending_application(self):
        self.enable_preference()
        self.start_liked()
        self.supported = False
        self.status["buffering"] = False
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_supported"])
        self.assertEqual(len(self.posts), 1)

    def test_unsupported_poll_preserves_queued_ordinary_liked_play(self):
        self.supported = False
        self.worker._poll()
        self.worker.play_liked(self.username, self.uri)
        self.worker._poll()
        self.process()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "/player/play")

    def test_unsupported_active_context_still_reports_its_observed_shuffle(self):
        self.supported = False
        self.active(True)
        self.assertTrue(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.shuffle_liked(self.username))

    def test_failed_active_toggle_restores_observed_state_and_rejects_retry_toggle(self):
        self.active()
        self.worker.shuffle_liked(self.username)
        self.process()
        self.status["playback_error"] = {"retry_after_ms": 0}
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertFalse(self.worker.shuffle_liked(self.username))
        self.assertTrue(self.worker.play_liked(self.username, self.uri))
        self.worker._poll()
        self.process()
        self.assertEqual(self.posts[-1][0], "/player/play")

    def test_fresh_capability_check_rejects_downgrade_before_toggle_runs(self):
        self.active()
        self.worker.shuffle_liked(self.username)
        self.supported = False
        self.process()
        self.assertEqual(self.posts, [])
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertIn("Update", self.worker.snapshot()["notice"])

    def test_stale_toggle_cannot_cancel_new_liked_play_on_backend_downgrade(self):
        self.worker.shuffle_liked(self.username)
        self.worker.play_liked(self.username, self.uri)
        self.supported = False
        self.process()
        self.process()
        self.assertEqual(self.posts, [("/player/play", {
            "uri": self.collection, "skip_to_uri": self.uri})])

    def test_capability_loss_cancels_queued_toggle_even_if_support_returns(self):
        self.active()
        self.worker.shuffle_liked(self.username)
        self.supported = False
        self.worker._poll()
        self.supported = True
        self.worker._poll()
        self.process()
        self.assertEqual(self.posts, [])
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])

    def test_toggle_queued_after_play_overrides_its_captured_preference(self):
        self.worker.play_liked(self.username, self.uri)
        self.worker.shuffle_liked(self.username)
        self.process()
        self.process()
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(self.posts[-1], ("/player/shuffle_context", {
            "shuffle_context": True, "context_uri": self.collection}))

    def test_http_failure_cancels_pending_and_keeps_error_notice(self):
        self.active()
        self.post_error = TimeoutError("shuffle timed out")
        self.worker.shuffle_liked(self.username)
        self.process(poll=False)
        self.assertFalse(self.worker.snapshot()["liked_shuffle_pending"])
        self.assertIn("shuffle timed out", self.worker.snapshot()["notice"])
        self.post_error = None
        self.worker._poll()
        self.assertFalse(self.worker.snapshot()["liked_shuffle"])
        self.assertEqual(len(self.posts), 1)

    def test_volume_preserves_deferred_shuffle(self):
        self.enable_preference()
        self.start_liked()
        self.worker.command("/player/volume", {"volume": 5, "relative": True})
        self.process()
        self.assertTrue(self.worker.snapshot()["liked_shuffle_pending"])
        self.status["buffering"] = False
        self.worker._poll()
        self.assertEqual(self.posts[-1], ("/player/shuffle_context", {"shuffle_context": True, "context_uri": self.collection}))


if __name__ == "__main__":
    unittest.main()
