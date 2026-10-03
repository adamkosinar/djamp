import io
import json
import math
import unittest
from unittest.mock import Mock, patch

import djamp


class Window:
    """A cell-accurate terminal surface for clipping and layout checks."""
    def __init__(self, height, width):
        self.height, self.width = height, width
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.cells = [[" "] * self.width for _ in range(self.height)]

    def addstr(self, y, x, value, attrs):
        assert 0 <= y < self.height
        assert x + djamp.cell_width(value) <= self.width, (y, x, value)
        for char in value:
            width = djamp.cell_width(char)
            if width:
                self.cells[y][x] = char
                for offset in range(1, width):
                    self.cells[y][x + offset] = ""
                x += width

    def text(self):
        return "\n".join("".join(row) for row in self.cells)


class TestSpotifyLinks(unittest.TestCase):
    def test_links_with_locale_and_tracking(self):
        identifier = "1" * 22
        self.assertEqual(djamp.spotify_uri(f"https://open.spotify.com/intl-cs/album/{identifier}?si=test"),
                         f"spotify:album:{identifier}")
        self.assertEqual(djamp.spotify_uri(f"spotify:track:{identifier}"), f"spotify:track:{identifier}")

    def test_rejects_unrelated_urls_and_commands(self):
        for value in ("https://open.spotify.com.attacker.test/track/" + "1" * 22,
                      "https://user@open.spotify.com/track/" + "1" * 22,
                      "http://127.0.0.1:80", "$(touch /tmp/no)", "spotify:track:invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                djamp.spotify_uri(value)


class TestTransport(unittest.TestCase):
    @patch("djamp.http.client.HTTPConnection")
    def test_relative_seek_and_volume_contract(self, connection):
        response = connection.return_value.getresponse.return_value
        response.status, response.read.return_value = 200, b""
        api = djamp.API()
        api.request("/player/seek", {"position": -10000, "relative": True}, post=True)
        args, kwargs = connection.return_value.request.call_args
        self.assertEqual(args, ("POST", "/player/seek"))
        self.assertEqual(json.loads(kwargs["body"]), {"position": -10000, "relative": True})
        api.request("/player/volume", {"volume": 5, "relative": True}, post=True)
        self.assertEqual(json.loads(connection.return_value.request.call_args.kwargs["body"]),
                         {"volume": 5, "relative": True})
        connection.assert_called_with("127.0.0.1", 3678, timeout=2)

    @patch("djamp.http.client.HTTPConnection")
    def test_no_session_and_server_errors(self, connection):
        response = connection.return_value.getresponse.return_value
        response.status, response.read.return_value = 204, b""
        self.assertEqual(djamp.API().request("/status"), {})
        response.status, response.read.return_value = 500, b"context cannot be played"
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            djamp.API().request("/player/play", {"uri": "spotify:track:" + "1" * 22}, post=True)
        self.assertEqual(connection.return_value.close.call_count, 2)

    @patch("djamp.subprocess.Popen")
    def test_attached_player_is_not_stopped_on_quit(self, popen):
        api = Mock()
        api.request.return_value = {"playback_ready": True}
        player = djamp.PlayerProcess(api)
        player.start()
        player.close()
        popen.assert_not_called()


class TestProgress(unittest.TestCase):
    def test_playing_paused_buffering_and_stale_status(self):
        status = {"track": {"position": 2000, "duration": 20000}, "paused": False, "stopped": False}
        self.assertEqual(djamp.playback_position(status, 10, now=10.5), 2500)
        self.assertEqual(djamp.playback_position(status, 10, now=50), 3500)
        for flag in ("paused", "buffering", "stopped"):
            self.assertEqual(djamp.playback_position(dict(status, **{flag: True}), 10, now=11), 2000)
        status["track"]["position"] = 19900
        self.assertEqual(djamp.playback_position(status, 10, now=11), 20000)


class TestVisualizer(unittest.TestCase):
    def test_silence_has_no_fake_activity(self):
        self.assertEqual(djamp.Spectrum().measure([0.0] * 1024), [0.0] * 32)

    def test_tone_lands_in_correct_frequency_band(self):
        fft = djamp.Spectrum()
        frequency = 1000
        samples = [0.8 * math.sin(2 * math.pi * frequency * i / fft.rate) for i in range(fft.size)]
        bands = fft.measure(samples)
        peak = max(range(len(bands)), key=bands.__getitem__)
        low, high = fft.ranges[peak]
        self.assertLessEqual(low * fft.rate / fft.size, frequency)
        self.assertGreaterEqual(high * fft.rate / fft.size, frequency)
        self.assertGreater(bands[peak], 0.8)

    def test_stale_monitor_data_returns_to_silence(self):
        monitor = djamp.AudioMonitor()
        monitor.bands = [0.9] * 32
        monitor.wave = [0.9] * 128
        monitor.updated = 0
        bands, wave, _ = monitor.snapshot()
        self.assertFalse(any(bands))
        self.assertFalse(any(wave))


class TestRendering(unittest.TestCase):
    def test_unicode_and_untrusted_metadata_stay_in_bounds(self):
        self.assertEqual(djamp.fit("曲曲曲", 5), "曲曲")
        self.assertNotIn("\x1b", djamp.clean("song\x1b[31m"))
        state = djamp.demo_state(10)
        state["status"]["track"]["name"] = "曲e\u0301 🌒" * 200 + "\x1b]0;evil\a"
        audio = ([0.8] * 32, [0.5] * 128, "OUTPUT AUDIO")
        for height, width in ((24, 58), (26, 80), (32, 100), (40, 150), (10, 30)):
            for mode in range(3):
                for help_open in (False, True):
                    with self.subTest(size=(height, width), mode=mode, help=help_open):
                        window = Window(height, width)
                        djamp.render(djamp.Canvas(window), state, audio, mode, demo=True, show_help=help_open)
                        self.assertNotIn("\x1b", window.text())

    def test_startup_pairing_and_missing_next_track(self):
        window = Window(38, 110)
        state = {"connected": True, "ready": False, "auth": {"code": "ABC123"}, "status": {}}
        audio = ([0.0] * 32, [0.0] * 128, "Output monitor unavailable")
        djamp.render(djamp.Canvas(window), state, audio)
        self.assertIn("spotify.com/pair", window.text())
        self.assertIn("ABC123", window.text())
        state = djamp.demo_state(10)
        state["status"].pop("next_track")
        djamp.render(djamp.Canvas(window), state, audio)
        self.assertIn("The DJ will reveal what comes next", window.text())


if __name__ == "__main__":
    unittest.main()
