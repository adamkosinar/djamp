import copy
import curses
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, call, patch

import djamp
from test_djamp import Window


PLAYING_URI = "spotify:track:" + "0" * 22


def playback_state():
    state = djamp.demo_state(0)
    state["status"]["username"] = "test-user"
    return state


def library_state(**overrides):
    state = djamp.demo_library_state(current_uri=PLAYING_URI)
    state.update(overrides)
    if state.get("like_pending"):
        state["pending_uri"] = PLAYING_URI
    return state


def initialized_worker():
    status = playback_state()["status"]
    responses = {"/auth/code": {},
                 "/": {"playback_ready": True, "direct_dj": True, "liked_shuffle": True},
                 "/status": status}
    api = Mock()
    api.request.side_effect = lambda path, *args, **kwargs: copy.deepcopy(responses[path])
    worker = djamp.PlayerWorker(api, autoplay=False)
    worker._poll()
    api.reset_mock()
    return worker


class TestLibraryControls(unittest.TestCase):
    def run_keys(self, keys, *, state=None, saved=None, demo=False, browse=None, worker=None):
        window = Mock()
        window.get_wch.side_effect = [*keys, "q"]
        if worker is None:
            worker = Mock()
            worker.snapshot.return_value = state if state is not None else playback_state()
        library = Mock()
        saved = saved if saved is not None else library_state()
        library.snapshot.side_effect = lambda: copy.deepcopy(saved)
        if browse is not None:
            library.browse.side_effect = lambda offset, **kwargs: browse(saved, offset)
        monitor = Mock()
        monitor.snapshot.return_value = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        player = Mock()
        player.failure.return_value = ""
        rendered = []
        with patch("djamp.curses.curs_set"), patch("djamp.palette", return_value={}), \
                patch("djamp.render", side_effect=lambda *args: rendered.append(copy.deepcopy(args[1:]))), \
                patch("djamp.subprocess.Popen") as popen:
            djamp.run_ui(window, None if demo else worker, None if demo else monitor,
                         None if demo else player, demo=demo, library=None if demo else library)
        return worker, library, rendered, popen

    def test_like_targets_playing_song_even_when_another_row_is_selected(self):
        worker, library, rendered, _ = self.run_keys(["L", "j", "l"])
        library.toggle.assert_called_once_with(PLAYING_URI)
        worker.command.assert_not_called()
        self.assertEqual(rendered[-1][0]["library_selection"], 1)
        library.observe.assert_called_with("test-user", PLAYING_URI, True)

    def test_like_uses_the_uri_from_the_displayed_frame(self):
        state = playback_state()
        other = copy.deepcopy(state)
        other["status"]["track"]["uri"] = "spotify:track:" + "9" * 22
        worker, library, monitor = Mock(), Mock(), Mock()
        worker.snapshot.side_effect = [state, other]
        library.snapshot.return_value = library_state()
        window = Mock()
        window.get_wch.side_effect = ["l", "q"]
        with patch("djamp.curses.curs_set"), patch("djamp.palette", return_value={}), patch("djamp.render"):
            djamp.run_ui(window, worker, monitor, None, library=library)
        library.toggle.assert_called_once_with(PLAYING_URI)
        library.observe.assert_called_with("test-user", other["status"]["track"]["uri"], True)

    def test_relinked_song_likes_use_original_uri(self):
        state = playback_state()
        alias = "spotify:track:" + "9" * 22
        state["status"]["track"].update(uri=alias, requested_uri=PLAYING_URI)
        worker, library, rendered, _ = self.run_keys(["l"], state=state)
        library.observe.assert_called_with("test-user", PLAYING_URI, True)
        library.toggle.assert_called_once_with(PLAYING_URI)
        self.assertEqual(rendered[-1][0]["status"]["track"]["uri"], alias)
        worker.command.assert_not_called()

    def test_selection_plays_collection_at_selected_uri_and_returns_to_player(self):
        worker, library, rendered, _ = self.run_keys(["L", curses.KEY_DOWN, "\n"])
        library.browse.assert_called_once_with(0)
        worker.play_liked.assert_called_once_with("test-user", "spotify:track:" + str(1).zfill(22))
        worker.command.assert_not_called()
        self.assertFalse(rendered[-1][0]["library_open"])

    def test_accepted_selection_with_real_worker_queues_song_and_returns_to_player(self):
        worker = initialized_worker()
        with patch.object(worker, "play_liked", wraps=worker.play_liked) as play_liked:
            _, _, rendered, _ = self.run_keys(["L", "j", "\n"], worker=worker)
        play_liked.assert_called_once_with("test-user", "spotify:track:" + str(1).zfill(22))
        self.assertEqual(worker.commands.qsize(), 1)
        worker.commands.get_nowait()
        self.assertTrue(worker.commands.empty())
        self.assertFalse(rendered[-1][0]["library_open"])
        self.assertEqual(rendered[-1][3], "Opening your liked song…")
        worker.api.request.assert_not_called()

    def test_rejected_playback_keeps_library_selection_and_shows_worker_feedback(self):
        for reason in ("queue_full", "cooldown"):
            for key in ("\n", "d", "D", "o"):
                with self.subTest(reason=reason, key=key), patch("djamp.time.monotonic", return_value=100):
                    worker = initialized_worker()
                    if reason == "queue_full":
                        for _ in range(worker.commands.maxsize):
                            self.assertTrue(worker.command("/player/volume", {"volume": 0}))
                        expected_feedback = "Player busy; command was not queued."
                    else:
                        worker.state["status"].update(stopped=True, paused=True, playback_error={
                            "kind": "audio_key_refused", "message": "Spotify refused the audio key.",
                            "uri": PLAYING_URI, "retry_after_ms": 10000})
                        expected_feedback = "Retry in 10s"
                    queued_before = list(worker.commands.queue)
                    saved = library_state()
                    saved["items"][0]["playable"] = False
                    with patch("djamp.prompt_link", return_value=PLAYING_URI):
                        # First leave a transient notice, then select a playable song.
                        _, _, rendered, _ = self.run_keys(["L", "\n", "j", key], saved=saved, worker=worker)
                    frame = rendered[-1]
                    self.assertTrue(frame[0]["library_open"])
                    self.assertEqual(frame[0]["library_selection"], 1)
                    self.assertEqual(list(worker.commands.queue), queued_before)
                    self.assertEqual(frame[3], "")
                    window = Window(24, 58)
                    djamp.render(djamp.Canvas(window), *frame)
                    screen = window.text()
                    self.assertIn("LIKED SONGS", screen)
                    self.assertIn(expected_feedback, screen)
                    self.assertNotIn("Opening", screen)
                    self.assertNotIn("This liked song is unavailable", screen)
                    if reason == "cooldown":
                        self.assertIn("FAILED", screen)
                        self.assertIn("Spotify refused the audio key.", screen)
                    worker.api.request.assert_not_called()

    def test_page_keys_and_refresh_use_the_displayed_offset(self):
        def browse(saved, offset):
            saved.update(djamp.demo_library_state(offset, PLAYING_URI))
        worker, library, rendered, _ = self.run_keys(["L", "]", "r", "["], browse=browse)
        self.assertEqual(library.browse.call_args_list, [call(0), call(20), call(20, refresh=True), call(0)])
        worker.command.assert_not_called()
        self.assertEqual(rendered[-1][0]["library_selection"], 0)
        _, library, _, _ = self.run_keys(["L", "]"], saved=library_state(has_next=False))
        library.browse.assert_called_once_with(0)

    def test_loading_empty_unavailable_and_disconnected_library_cannot_play(self):
        cases = [({}, library_state(loading=True)), ({}, library_state(items=[])),
                 ({}, library_state(items=[dict(library_state()["items"][0], playable=False)])),
                 ({"ready": False}, library_state())]
        for changes, saved in cases:
            with self.subTest(changes=changes, loading=saved["loading"], rows=len(saved["items"])):
                state = playback_state()
                state.update(changes)
                worker, _, rendered, _ = self.run_keys(["L", "\n"], state=state, saved=saved)
                worker.command.assert_not_called()
                worker.play_liked.assert_not_called()
                self.assertTrue(rendered[-1][0]["library_open"])
                self.assertTrue(rendered[-1][3])
        state = playback_state()
        state["status"].pop("username")
        worker, _, _, _ = self.run_keys(["L", "\n"], state=state)
        worker.command.assert_not_called()
        worker.play_liked.assert_not_called()

    def test_shuffle_in_library_targets_liked_preference_while_dj_is_playing(self):
        state = playback_state()
        state["status"]["context_uri"] = djamp.DJ_URI
        worker, library, rendered, _ = self.run_keys(["L", "s"], state=state)
        worker.shuffle_liked.assert_called_once_with("test-user")
        worker.command.assert_not_called()
        worker.play_liked.assert_not_called()
        library.toggle.assert_not_called()
        self.assertTrue(rendered[-1][0]["library_open"])

    def test_shuffle_on_player_targets_only_own_liked_collection(self):
        for context, allowed in (("spotify:user:test-user:collection", True),
                                 ("spotify:user:another-user:collection", False),
                                 (djamp.DJ_URI, False), ("", False)):
            with self.subTest(context=context):
                state = playback_state()
                state["status"]["context_uri"] = context
                worker, _, rendered, _ = self.run_keys(["s"], state=state)
                worker.command.assert_not_called()
                if allowed:
                    worker.shuffle_liked.assert_called_once_with("test-user")
                else:
                    worker.shuffle_liked.assert_not_called()
                    self.assertEqual(rendered[-1][3], "Open Liked Songs with L to choose shuffle.")

    def test_shuffle_waits_for_authenticated_connected_player(self):
        for changes in ({"connected": False}, {"ready": False}, {"username": None}):
            with self.subTest(changes=changes):
                state = playback_state()
                if "username" in changes:
                    state["status"].pop("username")
                else:
                    state.update(changes)
                worker, _, rendered, _ = self.run_keys(["L", "s"], state=state)
                worker.shuffle_liked.assert_not_called()
                worker.command.assert_not_called()
                self.assertTrue(rendered[-1][0]["library_open"])
                self.assertTrue(rendered[-1][3])

    def test_rejected_shuffle_keeps_library_selection_and_worker_feedback(self):
        worker = Mock()
        state = playback_state()
        state["notice"] = "Player busy; command was not queued. Try again."
        worker.snapshot.return_value = state
        worker.shuffle_liked.return_value = False
        saved = library_state()
        saved["items"][0]["playable"] = False
        _, _, rendered, _ = self.run_keys(["L", "\n", "j", "s"], saved=saved, worker=worker)
        worker.shuffle_liked.assert_called_once_with("test-user")
        self.assertTrue(rendered[-1][0]["library_open"])
        self.assertEqual(rendered[-1][0]["library_selection"], 1)
        self.assertEqual(rendered[-1][3], "")
        window = Window(24, 58)
        djamp.render(djamp.Canvas(window), *rendered[-1])
        self.assertIn("Player busy; command was not queued.", window.text())
        self.assertNotIn("This liked song is unavailable", window.text())

    def test_like_does_not_send_duplicate_or_non_track_requests(self):
        for uri, pending in ((PLAYING_URI, True), (None, False), ("spotify:episode:" + "1" * 22, False)):
            with self.subTest(uri=uri, pending=pending):
                state = playback_state()
                state["status"]["track"]["uri"] = uri
                _, library, _, _ = self.run_keys(["l"], state=state, saved=library_state(like_pending=pending))
                library.toggle.assert_not_called()

    def test_escape_closes_help_before_library(self):
        _, _, rendered, _ = self.run_keys(["L", "?", "\x1b", "\x1b"])
        self.assertEqual([frame[0]["library_open"] for frame in rendered], [False, True, True, True, False])
        self.assertEqual([frame[5] for frame in rendered], [False, False, True, False, False])

    def test_dj_and_transport_controls_remain_available_in_library(self):
        for key in ("d", "D"):
            with self.subTest(key=key):
                worker, _, rendered, popen = self.run_keys(["L", key])
                worker.command.assert_called_once_with("/player/play", {"uri": djamp.DJ_URI})
                self.assertFalse(rendered[-1][0]["library_open"])
                popen.assert_not_called()
        worker, _, _, _ = self.run_keys(["L", " ", "n", curses.KEY_LEFT, "+"])
        self.assertEqual([entry.args[0] for entry in worker.command.call_args_list],
                         ["/player/playpause", "/player/next", "/player/seek", "/player/volume"])

    def test_demo_browse_like_select_and_page_are_local(self):
        worker, library, rendered, popen = self.run_keys(["l", "L", "j", "\n", "L", "]", "r", "[", "d", "D", "b"], demo=True)
        self.assertFalse(rendered[1][0]["library"]["liked"])
        self.assertEqual(rendered[4][0]["status"]["track"]["uri"], "spotify:track:" + str(2).zfill(22))
        self.assertTrue(any(frame[0]["library"].get("offset") == 20 for frame in rendered))
        worker.command.assert_not_called()
        library.observe.assert_not_called()
        library.browse.assert_not_called()
        library.toggle.assert_not_called()
        popen.assert_not_called()

    def test_demo_shuffle_is_local_and_available_after_liked_selection(self):
        worker, library, rendered, popen = self.run_keys(["L", "s", "j", "\n", "s"], demo=True)
        self.assertFalse(rendered[1][0].get("liked_shuffle", False))
        self.assertTrue(rendered[2][0]["liked_shuffle"])
        selected = rendered[4][0]
        self.assertFalse(selected["library_open"])
        self.assertTrue(selected["liked_shuffle"])
        self.assertEqual(selected["status"]["context_uri"],
                         f"spotify:user:{selected['status']['username']}:collection")
        self.assertFalse(rendered[5][0]["liked_shuffle"])
        worker.command.assert_not_called()
        worker.shuffle_liked.assert_not_called()
        worker.play_liked.assert_not_called()
        library.observe.assert_not_called()
        popen.assert_not_called()

    def test_demo_main_does_not_construct_any_live_services(self):
        with ExitStack() as stack:
            stack.enter_context(patch("djamp.sys.argv", ["djamp", "--demo"]))
            stack.enter_context(patch.object(djamp.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(djamp.sys.stdout, "isatty", return_value=True))
            stack.enter_context(patch("djamp.os.umask"))
            stack.enter_context(patch("djamp.signal.signal"))
            stack.enter_context(patch("djamp.curses.wrapper"))
            services = [stack.enter_context(patch("djamp." + name))
                        for name in ("API", "PlayerProcess", "PlayerWorker", "AudioMonitor", "LibraryWorker")]
            self.assertEqual(djamp.main(), 0)
            for service in services:
                service.assert_not_called()


class TestLibraryRendering(unittest.TestCase):
    def test_library_shuffle_state_and_pending_indicator_fit_narrow_heading(self):
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for enabled in (False, True):
            for pending in (False, True):
                with self.subTest(enabled=enabled, pending=pending):
                    state = playback_state()
                    state.update(library=library_state(offset=999980, total=1000000),
                                 library_open=True, library_selection=0,
                                 liked_shuffle=enabled, liked_shuffle_pending=pending)
                    window = Window(24, 58)
                    djamp.render(djamp.Canvas(window), state, audio)
                    heading = next(line for line in window.text().splitlines() if "LIKED SONGS" in line)
                    self.assertIn("SHUFFLE ON" if enabled else "SHUFFLE OFF", heading)
                    self.assertEqual("…" in heading, pending)
                    self.assertIn("s shuffle", window.text())

    def test_player_shuffle_indicator_is_visible_despite_temporary_notices(self):
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                state = playback_state()
                state["status"]["context_uri"] = "spotify:user:test-user:collection"
                state.update(liked_shuffle=enabled, liked_shuffle_pending=False)
                window = Window(24, 58)
                djamp.render(djamp.Canvas(window), state, audio, notice="Volume changed")
                self.assertIn("LIKED · SHUFFLE ON" if enabled else "LIKED · SHUFFLE OFF", window.text())
                self.assertIn("Volume changed", window.text())

    def test_player_does_not_show_liked_shuffle_indicator_for_dj_or_other_account(self):
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for context in (djamp.DJ_URI, "spotify:user:another-user:collection"):
            with self.subTest(context=context):
                state = playback_state()
                state["status"]["context_uri"] = context
                state.update(liked_shuffle=True, liked_shuffle_pending=False)
                window = Window(24, 58)
                djamp.render(djamp.Canvas(window), state, audio)
                self.assertNotIn("LIKED · SHUFFLE", window.text())

    def test_shuffle_help_fits_minimum_terminal(self):
        state = playback_state()
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        window = Window(24, 58)
        djamp.render(djamp.Canvas(window), state, audio, show_help=True)
        self.assertIn("shuffle", window.text().lower())
        self.assertIn("Press ? or Esc to close", window.text())

    def test_relinked_song_badge_uses_original_uri(self):
        state = playback_state()
        alias = "spotify:track:" + "9" * 22
        state["status"]["track"].update(uri=alias, requested_uri=PLAYING_URI)
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for liked, pending, expected in ((True, False, "[♥] LIKED"),
                                         (False, False, "[ ] l LIKE"),
                                         (True, True, "[..] SAVING")):
            with self.subTest(liked=liked, pending=pending):
                window = Window(24, 58)
                state["library"] = library_state(liked=liked, like_pending=pending)
                djamp.render(djamp.Canvas(window), state, audio)
                self.assertIn(expected, window.text())
        window = Window(24, 58)
        state["library"] = library_state(current_uri=alias, liked=True)
        djamp.render(djamp.Canvas(window), state, audio)
        self.assertNotIn("[♥] LIKED", window.text())

    def test_library_and_help_fit_minimum_size_with_untrusted_metadata(self):
        current = library_state()
        current["items"][0]["name"] = "曲e\u0301 🌒" * 60 + "\x1b]0;bad\a"
        variants = [current, library_state(items=[], loaded=False, loading=True),
                    library_state(items=[]), library_state(items=[], error="Library unavailable")]
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for height, width in ((24, 58), (38, 100), (40, 150), (10, 30)):
            for saved in variants:
                for selection in (0, 19):
                    for help_open in (False, True):
                        with self.subTest(size=(height, width), selection=selection, help=help_open):
                            window = Window(height, width)
                            state = playback_state()
                            state.update(library=saved, library_open=True, library_selection=selection)
                            djamp.render(djamp.Canvas(window), state, audio, show_help=help_open)
                            self.assertNotIn("\x1b", window.text())
                            if height >= 24 and width >= 58:
                                self.assertIn("Press ? or Esc to close" if help_open else "LIKED SONGS", window.text())

    def test_current_song_like_badge_matches_uri_and_confirmed_state(self):
        window = Window(24, 58)
        state = playback_state()
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        for liked, pending, expected in ((True, False, "[♥] LIKED"), (False, False, "[ ] l LIKE"), (True, True, "[..] SAVING")):
            with self.subTest(liked=liked, pending=pending):
                state["library"] = library_state(liked=liked, like_pending=pending)
                djamp.render(djamp.Canvas(window), state, audio)
                self.assertIn(expected, window.text())
        state["library"]["current_uri"] = "spotify:track:" + "9" * 22
        djamp.render(djamp.Canvas(window), state, audio)
        self.assertNotIn("[..] SAVING", window.text())


    def test_old_track_like_request_does_not_mark_new_track_saving(self):
        window = Window(24, 58)
        state = playback_state()
        next_uri = "spotify:track:" + "9" * 22
        state["status"]["track"]["uri"] = next_uri
        state["library"] = library_state(current_uri=next_uri, liked=None, like_pending=True)
        audio = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        djamp.render(djamp.Canvas(window), state, audio)
        self.assertNotIn("[..] SAVING", window.text())
        self.assertIn("l LIKE", window.text())


if __name__ == "__main__":
    unittest.main()
