from contextlib import ExitStack
import subprocess
import unittest
from unittest.mock import Mock, patch

import djamp


class TestDJKeys(unittest.TestCase):
    def run_keys(self, keys, *, demo=False, ready=False):
        window = Mock()
        window.get_wch.side_effect = [*keys, "q"]
        worker = Mock()
        worker.snapshot.return_value = {"connected": ready, "ready": ready, "status": {}}
        monitor = Mock()
        monitor.snapshot.return_value = ([0.0] * 32, [0.0] * 128, "OUTPUT AUDIO")
        player = Mock()
        player.failure.return_value = ""
        with patch("djamp.curses.curs_set"), patch("djamp.palette", return_value={}), \
                patch("djamp.render"), patch("djamp.subprocess.Popen") as popen:
            # Missing service objects make any accidental demo dependency fail.
            djamp.run_ui(window, None if demo else worker, None if demo else monitor,
                         None if demo else player, demo=demo)
        return worker, popen

    def test_both_d_keys_queue_direct_start_even_before_connection(self):
        for key in ("d", "D"):
            for ready in (False, True):
                with self.subTest(key=key, ready=ready):
                    worker, popen = self.run_keys([key], ready=ready)
                    worker.command.assert_called_once_with("/player/play", {"uri": djamp.DJ_URI})
                    popen.assert_not_called()

    def test_b_opens_spotify_without_sending_playback_command(self):
        worker, popen = self.run_keys(["b"])
        worker.command.assert_not_called()
        popen.assert_called_once_with(
            ["xdg-open", djamp.DJ_URL], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def test_demo_d_and_b_keys_do_not_use_spotify_or_audio_services(self):
        worker, popen = self.run_keys(["d", "D", "b"], demo=True)
        worker.assert_not_called()
        worker.command.assert_not_called()
        worker.snapshot.assert_not_called()
        popen.assert_not_called()


class TestStartupOptions(unittest.TestCase):
    def test_no_autoplay_option_reaches_worker(self):
        for arguments, autoplay in (([], True), (["--no-autoplay"], False)):
            with self.subTest(arguments=arguments), ExitStack() as stack:
                stack.enter_context(patch("djamp.sys.argv", ["djamp", *arguments]))
                stack.enter_context(patch.object(djamp.sys.stdin, "isatty", return_value=True))
                stack.enter_context(patch.object(djamp.sys.stdout, "isatty", return_value=True))
                stack.enter_context(patch("djamp.os.umask"))
                stack.enter_context(patch("djamp.signal.signal"))
                stack.enter_context(patch("djamp.curses.wrapper"))
                api = stack.enter_context(patch("djamp.API"))
                player = stack.enter_context(patch("djamp.PlayerProcess"))
                worker = stack.enter_context(patch("djamp.PlayerWorker"))
                monitor = stack.enter_context(patch("djamp.AudioMonitor"))
                library = stack.enter_context(patch("djamp.LibraryWorker"))

                self.assertEqual(djamp.main(), 0)
                worker.assert_called_once_with(api.return_value, autoplay=autoplay)
                worker.return_value.start.assert_called_once_with()
                worker.return_value.stop_event.set.assert_called_once_with()
                player.return_value.close.assert_called_once_with()
                monitor.return_value.close.assert_called_once_with()
                library.assert_called_once_with(api.return_value)
                library.return_value.start.assert_called_once_with()
                library.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
