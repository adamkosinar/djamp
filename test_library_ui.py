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


class TestLibraryControls(unittest.TestCase):
    def run_keys(self, keys, *, state=None, saved=None, demo=False, browse=None):
        window = Mock()
        window.get_wch.side_effect = [*keys, "q"]
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
        worker.command.assert_called_once_with("/player/play", {
            "uri": "spotify:user:test-user:collection", "skip_to_uri": "spotify:track:" + str(1).zfill(22)})
        self.assertFalse(rendered[-1][0]["library_open"])

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
                self.assertTrue(rendered[-1][0]["library_open"])
                self.assertTrue(rendered[-1][3])
        state = playback_state()
        state["status"].pop("username")
        worker, _, _, _ = self.run_keys(["L", "\n"], state=state)
        worker.command.assert_not_called()

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
