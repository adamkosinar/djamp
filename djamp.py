#!/usr/bin/env python3
"""A terminal Spotify player that starts DJ X through go-librespot."""

from __future__ import annotations

import argparse
import array
import cmath
import curses
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

ROOT = Path.home()
CONFIG = ROOT / ".config/go-librespot"
LOG = ROOT / ".local/state/djamp/player.log"
PORT = 3678
DJ_URI = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
DJ_URL = "https://open.spotify.com/playlist/37i9dQZF1EYkqdzj48dyYq"


def clean(value):
    """Do not allow metadata to inject terminal controls."""
    return "".join(c for c in str(value or "") if not unicodedata.category(c).startswith("C"))


def cell_width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in clean(value))


def fit(value, width):
    result = ""
    used = 0
    for char in clean(value):
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + cells > max(0, width):
            break
        result += char
        used += cells
    return result


def clock_text(milliseconds):
    seconds = max(0, int(milliseconds or 0) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def spotify_uri(value):
    value = value.strip()
    pattern = r"spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]{22}"
    if re.fullmatch(pattern, value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) == 2:
            uri = "spotify:" + ":".join(parts)
            if re.fullmatch(pattern, uri):
                return uri
    raise ValueError("Paste a Spotify track, album, playlist, artist, show or episode link.")


class API:
    def __init__(self, port=PORT):
        self.port = port

    def request(self, path, payload=None, *, post=False):
        # A direct loopback connection: never send requests through a proxy.
        # Context resolution can take longer than a status poll. It runs on the
        # worker thread, so this bounded wait never blocks terminal input.
        timeout = 20 if post and path == "/player/play" else 2
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            data = json.dumps(payload or {}).encode() if post else None
            conn.request("POST" if post else "GET", path, body=data,
                         headers={"Content-Type": "application/json"} if post else {})
            response = conn.getresponse()
            body = response.read(2_000_000)
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Player returned HTTP {response.status}: {clean(body.decode(errors='replace'))[:160]}")
            return json.loads(body) if body.strip() else {}
        finally:
            conn.close()


class PlayerProcess:
    """Own only the child we start; never stop an already-running player."""
    def __init__(self, api):
        self.api = api
        self.process = None
        self.output = None

    def start(self):
        try:
            root = self.api.request("/")
        except (OSError, http.client.HTTPException):
            root = None
        if root is not None:
            if "playback_ready" not in root:
                raise RuntimeError("Port 3678 is already used by another application.")
            return
        lockfile = CONFIG / "lockfile"
        if lockfile.exists():
            with lockfile.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Stop the previous try-spotify-dj with Ctrl+C, then run djamp again.") from None
                # Closing the handle releases our lock, if acquired.
        binary = ROOT / ".local/bin/go-librespot-djamp"
        if not binary.is_file():
            raise RuntimeError("DJamp backend missing. Run ./scripts/build-backend from the DJamp checkout.")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        self.output = LOG.open("w")
        LOG.chmod(0o600)
        self.process = subprocess.Popen(
            [str(binary), "--config_dir", str(CONFIG)], stdin=subprocess.DEVNULL,
            stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True)

    def failure(self):
        if self.process is not None and self.process.poll() is not None:
            return "Player stopped. Check ~/.local/state/djamp/player.log, then restart djamp."
        return ""

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.output is not None:
            self.output.close()


def playback_position(status, received_at, now=None):
    track = status.get("track") or {}
    position = track.get("position") or 0
    if not any(status.get(k) for k in ("paused", "stopped", "buffering")):
        # Never invent progress during a prolonged connection failure.
        position += min(1500, max(0, ((now or time.monotonic()) - received_at) * 1000))
    duration = track.get("duration") or 0
    return max(0, min(position, duration)) if duration else max(0, position)


class PlayerWorker(threading.Thread):
    def __init__(self, api, autoplay=True):
        super().__init__(daemon=True)
        self.api = api
        self.stop_event = threading.Event()
        self.commands = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.autoplay_pending = autoplay
        self.dj_requested = False
        self.dj_started_at = None
        self.previous_track = None
        self.state = {"connected": False, "ready": False, "status": {}, "auth": {},
                      "received": time.monotonic(), "error": "", "notice": "", "recent": [],
                      "direct_dj": False, "dj_starting": False}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def command(self, path, payload=None):
        try:
            self.commands.put_nowait((path, payload))
        except queue.Full:
            with self.lock:
                self.state["notice"] = "Waiting for the player to finish previous commands."

    def _notice(self, message, starting=False):
        with self.lock:
            self.state.update(notice=message, dj_starting=starting)

    def _process_command(self, path, payload):
        # A playback choice supersedes startup autoplay. Volume adjustments
        # should still work without cancelling a queued or loading DJ session.
        if path != "/player/volume":
            self.autoplay_pending = False
            self.dj_started_at = None
            self.dj_requested = path == "/player/play" and (payload or {}).get("uri") == DJ_URI
            if self.dj_requested:
                self._notice("Waiting for Spotify to start DJ X…", starting=True)
                return
        try:
            self.api.request(path, payload, post=True)
            if not self.dj_requested and self.dj_started_at is None:
                self._notice("")
        except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
            self._notice(clean(exc), starting=self.dj_requested or self.dj_started_at is not None)

    def _start_dj(self, supported):
        self.dj_requested = False
        if not supported:
            self._notice("Close the previous player and restart DJamp to enable DJ startup. b opens Spotify.")
            return
        self._notice("Starting DJ X…", starting=True)
        try:
            self.api.request("/player/play", {"uri": DJ_URI}, post=True)
            self.dj_started_at = time.monotonic()
        except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
            self._notice("Could not start DJ X: " + clean(exc) + " · Press d to retry.")

    def _poll(self):
        # This endpoint bypasses the backend's player request queue. During
        # device pairing that queue cannot answer even the readiness endpoint.
        auth = self.api.request("/auth/code")
        if auth.get("code"):
            with self.lock:
                self.state.update(connected=True, ready=False, status={}, auth=auth, error="")
            return
        root = self.api.request("/")
        ready = bool(root.get("playback_ready"))
        status = self.api.request("/status") if ready else {}
        track = status.get("track") or {}
        with self.lock:
            recent = list(self.state["recent"])
            if track.get("uri") and self.previous_track and track["uri"] != self.previous_track.get("uri"):
                recent = [self.previous_track] + [t for t in recent if t.get("uri") != self.previous_track.get("uri")]
            if track.get("uri"):
                self.previous_track = track
            self.state.update(connected=True, ready=ready, direct_dj=bool(root.get("direct_dj")),
                              status=status, auth=auth, received=time.monotonic(), error="",
                              recent=recent[:40])

        if self.dj_started_at is not None:
            if status.get("context_uri") == DJ_URI and track and not status.get("stopped") and not status.get("buffering"):
                self.dj_started_at = None
                self._notice("")
            elif time.monotonic() - self.dj_started_at >= 30:
                self.dj_started_at = None
                self._notice("DJ X did not start. Press d to retry or b to open Spotify.")

        # Consume this once per launch, even when there is already a session.
        # A later pause, stop, or reconnect must not unexpectedly restart DJ.
        if self.autoplay_pending and ready and "stopped" in status:
            self.autoplay_pending = False
            if status["stopped"] and not track and not status.get("paused") and not status.get("buffering"):
                self.dj_requested = True
        if self.dj_requested and ready and not self.stop_event.is_set():
            self._start_dj(bool(root.get("direct_dj")))

    def run(self):
        next_poll = 0
        while not self.stop_event.is_set():
            try:
                path, payload = self.commands.get(timeout=max(0, min(0.1, next_poll - time.monotonic())))
                self._process_command(path, payload)
                next_poll = 0
            except queue.Empty:
                pass
            if time.monotonic() < next_poll:
                continue
            next_poll = time.monotonic() + 0.5
            try:
                self._poll()
            except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
                with self.lock:
                    self.state.update(connected=False, error=clean(exc))


class Spectrum:
    """Hann-windowed FFT; no external Python dependencies."""
    def __init__(self, size=1024, rate=22050, bands=32):
        self.size, self.rate, self.count = size, rate, bands
        bits = int(math.log2(size))
        self.order = [int(f"{i:0{bits}b}"[::-1], 2) for i in range(size)]
        self.window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (size - 1)) for i in range(size)]
        self.twiddles = {length: [cmath.exp(-2j * math.pi * j / length) for j in range(length // 2)]
                         for length in (2 ** p for p in range(1, bits + 1))}
        edges = [int(40 * (10000 / 40) ** (i / bands) * size / rate) for i in range(bands + 1)]
        self.ranges = [(max(1, edges[i]), min(size // 2, max(edges[i] + 1, edges[i + 1])))
                       for i in range(bands)]

    def measure(self, samples):
        values = [complex(samples[i] * self.window[i]) for i in self.order]
        for length, factors in self.twiddles.items():
            half = length // 2
            for start in range(0, self.size, length):
                for j, factor in enumerate(factors):
                    a, b = values[start + j], values[start + j + half] * factor
                    values[start + j], values[start + j + half] = a + b, a - b
        magnitudes = [abs(v) * 4 / self.size for v in values[:self.size // 2]]
        bands = []
        for low, high in self.ranges:
            amplitude = max(magnitudes[low:high], default=0)
            db = 20 * math.log10(max(amplitude, 1e-10))
            bands.append(min(1.0, max(0.0, (db + 65) / 65)))
        return bands


class AudioMonitor(threading.Thread):
    """Capture output monitor samples in memory; never select an input/microphone."""
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.bands, self.wave = [0.0] * 32, [0.0] * 128
        self.updated = 0
        self.message = "Connecting to output monitor"

    def snapshot(self):
        with self.lock:
            fresh = time.monotonic() - self.updated < 0.75
            return (list(self.bands) if fresh else [0.0] * 32,
                    list(self.wave) if fresh else [0.0] * 128, self.message)

    def run(self):
        if not shutil.which("parec"):
            self.message = "Output visualizer unavailable (parec is missing)"
            return
        fft = Spectrum()
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(
                    ["parec", "--device=@DEFAULT_MONITOR@", "--raw", "--format=s16le",
                     "--rate=22050", "--channels=1", "--latency-msec=40", "--client-name=DJamp visualizer"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if self.stop_event.is_set():
                    self.process.terminate()
                buffer = bytearray()
                while not self.stop_event.is_set():
                    data = self.process.stdout.read(2048 - len(buffer))
                    if not data:
                        break
                    buffer.extend(data)
                    if len(buffer) != 2048:
                        continue
                    raw = array.array("h", buffer)
                    if sys.byteorder != "little":
                        raw.byteswap()
                    samples = [v / 32768 for v in raw]
                    bands = fft.measure(samples)
                    with self.lock:
                        self.bands = [max(new, old * 0.78) for old, new in zip(self.bands, bands)]
                        self.wave = samples[::8]
                        self.updated = time.monotonic()
                        self.message = "OUTPUT AUDIO"
                    buffer.clear()
            except OSError:
                pass
            finally:
                if self.process is not None:
                    if self.process.poll() is None:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                    if self.process.stdout:
                        self.process.stdout.close()
            with self.lock:
                self.message = "Output monitor unavailable; playback controls still work"
            self.stop_event.wait(3)

    def close(self):
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        if self.is_alive():
            self.join(timeout=2)


class Canvas:
    def __init__(self, window, colors=None):
        self.window = window
        self.colors = colors or {}

    def put(self, y, x, text, color="text", bold=False, width=None):
        height, columns = self.window.getmaxyx()
        if not 0 <= y < height or not 0 <= x < columns:
            return
        room = columns - x - (1 if y == height - 1 else 0)
        value = fit(text, min(room, width) if width is not None else room)
        if not value:
            return
        try:
            self.window.addstr(y, x, value, self.colors.get(color, 0) | (curses.A_BOLD if bold else 0))
        except curses.error:
            pass

    def box(self, y, x, height, width, label):
        self.put(y, x, "╭" + "─" * (width - 2) + "╮", "border")
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "│", "border")
            self.put(row, x + width - 1, "│", "border")
        self.put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", "border")
        self.put(y, x + 3, f" {label} ", "accent", bold=True, width=width - 7)


def palette():
    if not curses.has_colors():
        return {}
    curses.start_color()
    curses.use_default_colors()
    colors = {"text": 252, "muted": 245, "accent": 154, "cyan": 80,
              "warm": 214, "border": 239, "error": 203}
    basic = {"text": 7, "muted": 7, "accent": 2, "cyan": 6, "warm": 3, "border": 4, "error": 1}
    result = {}
    for index, (key, value) in enumerate(colors.items(), 1):
        curses.init_pair(index, value if curses.COLORS >= 256 else basic[key], -1)
        result[key] = curses.color_pair(index)
    return result


def render(canvas, state, audio, mode=0, notice="", demo=False, show_help=False):
    screen_h, screen_w = canvas.window.getmaxyx()
    canvas.window.erase()
    if screen_h < 24 or screen_w < 58:
        canvas.put(1, 2, "DJAMP", "accent", True)
        canvas.put(3, 2, "Make the terminal at least 58 × 24.")
        canvas.put(5, 2, "Space: play/pause   n: next   q: quit", "muted")
        return
    height, width = min(screen_h, 38), min(screen_w - 4, 108)
    top, left = (screen_h - height) // 2, (screen_w - width) // 2
    status = state.get("status") or {}
    track = status.get("track") or {}
    paused, stopped = status.get("paused", False), status.get("stopped", True)
    connected = state.get("connected", False)
    ready = state.get("ready", False)
    playing = connected and ready and not paused and not stopped and bool(track)
    context = status.get("context_name") or "Spotify Connect"
    label = "PLAYING" if playing else "PAUSED" if paused else "READY" if ready else "CONNECTING"
    if status.get("buffering"):
        label = "BUFFERING"
    if not connected:
        label = "CONNECTING"
    elif state.get("dj_starting"):
        label = "STARTING DJ"

    canvas.put(top, left + 1, "D J A M P", "accent", True)
    canvas.put(top, left + 16, "SPOTIFY  /  TERMINAL PLAYER" if width >= 84 else "SPOTIFY", "muted")
    canvas.put(top, left + width - 15, "● " + label, "accent" if playing else "warm")
    canvas.put(top + 1, left + 1, "DEMO · NO SPOTIFY CONNECTION" if demo else "Your music. Your DJ. Your terminal.", "muted")
    canvas.box(top + 3, left, 8, width, "NOW PLAYING")
    canvas.put(top + 3, left + width - 21, " OMARCHY DJ ", "muted")
    auth = state.get("auth") or {}
    if auth.get("code"):
        title, artist, album = "Connect your Spotify account", "Open spotify.com/pair", "Your code: " + str(auth["code"])
    elif track:
        title = track.get("name") or "DJ narration"
        artist = " · ".join(track.get("artist_names") or []) or "Spotify DJ"
        album = track.get("album_name") or context
    elif not ready:
        title, artist, album = "Connecting to Spotify…", "Using your saved Spotify login", "This can take a moment."
    elif state.get("dj_starting"):
        title, artist, album = "Starting your DJ…", "Loading your Spotify DJ session", "Your DJ introduction and music will play here."
    else:
        title, artist, album = "Ready for your DJ", "Press d to start your next DJ set.", "o plays a Spotify link  ·  b opens Spotify"
    canvas.put(top + 5, left + 3, title, "text", True, width - 6)
    canvas.put(top + 6, left + 3, artist, "cyan", width=width - 6)
    canvas.put(top + 7, left + 3, album, "muted", width=width - 6)
    position = playback_position(status, state.get("received", time.monotonic()))
    duration = track.get("duration") or 0
    bar_width = width - 23
    filled = round(bar_width * min(1, position / duration)) if duration else 0
    canvas.put(top + 9, left + 3, clock_text(position), "accent")
    canvas.put(top + 9, left + 10, "━" * bar_width, "border")
    canvas.put(top + 9, left + 10, "━" * filled, "accent")
    if duration and filled < bar_width:
        canvas.put(top + 9, left + 10 + filled, "●", "accent")
    canvas.put(top + 9, left + width - 9, clock_text(duration), "muted")

    canvas.put(top + 11, left + 2, "[p] ◀◀   [space] " + ("Ⅱ" if playing else "▶") + "   [n] ▶▶", "text")
    volume = round(100 * (status.get("volume") or 0) / max(1, status.get("volume_steps") or 100))
    volume = min(100, max(0, volume))
    volume_bar = "▰" * round(volume / 10) + "▱" * (10 - round(volume / 10))
    canvas.put(top + 11, left + width - 21, f"{volume_bar} {volume:3d}%", "accent")

    visual_h = min(10, max(5, height - 25))
    visual_y = top + 13
    bands, wave, audio_message = audio
    canvas.box(visual_y, left, visual_h, width, ("SPECTRUM", "WAVEFORM", "VISUALIZER OFF")[mode])
    if width > 70:
        canvas.put(visual_y, left + width - 21, " OUTPUT AUDIO ", "muted")
    graph_h, graph_w = visual_h - 2, width - 6
    if mode == 0:
        count = min(32, graph_w // 2)
        spacing = graph_w / count
        blocks = " ▁▂▃▄▅▆▇█"
        for i in range(count):
            value = bands[min(len(bands) - 1, i * len(bands) // count)]
            level = value * graph_h * 8
            for row in range(graph_h):
                amount = int(min(8, max(0, level - (graph_h - row - 1) * 8)))
                if amount:
                    canvas.put(visual_y + 1 + row, left + 3 + int(i * spacing), blocks[amount] * 2,
                               "warm" if row < graph_h / 4 else "accent" if row < graph_h * 0.65 else "cyan")
    elif mode == 1:
        middle = visual_y + 1 + graph_h // 2
        canvas.put(middle, left + 3, "·" * graph_w, "border")
        for i in range(graph_w):
            value = wave[min(len(wave) - 1, int(i * len(wave) / graph_w))]
            row = min(graph_h - 1, max(0, graph_h // 2 - round(value * (graph_h - 1))))
            canvas.put(visual_y + 1 + row, left + 3 + i, "•", "cyan")
    if mode == 2 or audio_message != "OUTPUT AUDIO":
        hint = "Press v to show the visualizer" if mode == 2 else audio_message
        canvas.put(visual_y + 1, left + 3, hint, "muted", width=width - 6)

    list_y = visual_y + visual_h + 1
    list_h = top + height - 4 - list_y
    next_track = status.get("next_track") or {}
    if list_h >= 5:
        canvas.box(list_y, left, list_h, width, "DJ SESSION" if "dj" in context.lower() else "SESSION")
        next_name = next_track.get("name") or ("The DJ will reveal what comes next" if "dj" in context.lower() else "Waiting for the next track")
        canvas.put(list_y + 1, left + 3, "NEXT", "accent")
        canvas.put(list_y + 1, left + 10, next_name, "text", width=width - 13)
        for index, item in enumerate(state.get("recent", [])[:list_h - 3]):
            canvas.put(list_y + 2 + index, left + 3, f"{index + 1:02d}", "muted")
            name = item.get("name") or "DJ narration"
            names = " · ".join(item.get("artist_names") or [])
            canvas.put(list_y + 2 + index, left + 10, name + ("  /  " + names if names else ""), "muted", width=width - 13)
        if not state.get("recent") and list_h > 4:
            canvas.put(list_y + 3, left + 3, "Recently played tracks appear here as your session continues.", "muted", width=width - 6)
    else:
        canvas.put(list_y, left + 2, "NEXT  " + (next_track.get("name") or "Selected by your DJ"), "muted", width=width - 4)

    codec = (track.get("codec") or "").upper()
    bitrate = track.get("bitrate")
    quality = f"{codec} · {bitrate} kbps" if bitrate else codec
    footer = notice or state.get("error") or state.get("notice") or ("Connecting to the player…" if not connected else f"{context}   {quality}")
    canvas.put(top + height - 3, left + 2, footer, "warm" if notice or state.get("error") or state.get("notice") else "muted", width=width - 4)
    keys = "space play/pause · n/p skip · +/- vol · v view · o link · d DJ set · ? help · q quit"
    if width < 82:
        keys = "space play · n/p skip · d DJ set · ? help · q quit"
    canvas.put(top + height - 1, left + 1, keys, "cyan", width=width - 2)
    if show_help:
        lines = ["KEYBOARD CONTROLS", "", "Space       Play / pause", "n / p       Next / previous track",
                 "← / →       Seek 10 seconds", "+ / -       Volume up / down", "m           Mute / restore volume",
                 "v           Spectrum / waveform / off", "o           Play a Spotify link or URI",
                 "d / D       Start the next DJ set", "b           Open Spotify in browser", "q / Ctrl+C  Quit", "", "DJ starts automatically when idle.",
                 "The visualizer follows your audio output.", "", "Press ? or Esc to close"]
        box_w = min(width - 2, 54)
        box_x, box_y = (screen_w - box_w) // 2, (screen_h - len(lines) - 2) // 2
        for row in range(len(lines) + 2):
            canvas.put(box_y + row, box_x, " " * box_w)
        canvas.box(box_y, box_x, len(lines) + 2, box_w, "HELP")
        for i, line in enumerate(lines):
            canvas.put(box_y + 1 + i, box_x + 3, line, "accent" if i == 0 else "text", width=box_w - 6)


def prompt_link(window, colors, should_stop=lambda: False):
    value = ""
    window.timeout(100)
    try:
        curses.curs_set(1)
        while not should_stop():
            height, width = window.getmaxyx()
            window.move(max(0, height - 2), 0)
            window.clrtoeol()
            label = "Spotify link (Esc cancels): "
            visible = fit(value, max(0, width - len(label) - 2))
            Canvas(window, colors).put(height - 2, 1, label + visible, "accent")
            window.refresh()
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return value
            if key in ("\x1b", "\x03"):
                return ""
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif isinstance(key, str) and key.isprintable() and len(value) < 1000:
                value += key
        return ""
    finally:
        curses.curs_set(0)
        window.timeout(50)


def demo_state(start, paused=False, volume=65, paused_at=None):
    now = time.monotonic()
    elapsed = (paused_at if paused_at is not None else now) - start
    track = {"uri": "spotify:track:" + "0" * 22, "name": "Midnight City", "artist_names": ["M83"],
             "album_name": "Hurry Up, We're Dreaming", "position": int(elapsed * 1000) % 243000,
             "duration": 243000, "codec": "vorbis", "bitrate": 320}
    return {"connected": True, "ready": True, "received": now, "recent": [],
            "status": {"paused": paused, "stopped": False, "context_name": "DJ X", "volume": volume,
                       "volume_steps": 100, "track": track, "next_track": {"name": "Your next discovery"}}}


def run_ui(window, worker, monitor, player, demo=False, should_stop=lambda: False):
    curses.curs_set(0)
    window.keypad(True)
    window.timeout(50)
    colors = palette()
    canvas = Canvas(window, colors)
    mode, help_open, muted_volume = 0, False, None
    notice, notice_until = "", 0
    start = time.monotonic()
    demo_paused, demo_volume = False, 65
    demo_paused_at = None
    while not should_stop():
        state = demo_state(start, demo_paused, demo_volume, demo_paused_at) if demo else worker.snapshot()
        if demo:
            age = time.monotonic() - start
            audio = ([0 if demo_paused else (0.4 + 0.3 * math.sin(i * 0.4 + age * 2)) * (1 - i / 45) for i in range(32)],
                     [0 if demo_paused else 0.6 * math.sin(i * 0.15 + age * 4) for i in range(128)], "OUTPUT AUDIO")
        else:
            audio = monitor.snapshot()
        error = player.failure() if player else ""
        render(canvas, state, audio, mode, error or (notice if time.monotonic() < notice_until else ""), demo, help_open)
        window.refresh()
        try:
            key = window.get_wch()
        except curses.error:
            continue
        if key in ("q", "\x03"):
            break
        if key in ("?", "\x1b"):
            help_open = not help_open if key == "?" else False
            continue
        if help_open:
            continue
        if key == "v":
            mode = (mode + 1) % 3
            continue
        if key in ("d", "D", "b"):
            if demo:
                notice = "Demo mode: no Spotify commands are sent."
            elif key in ("d", "D"):
                worker.command("/player/play", {"uri": DJ_URI})
                notice = ""
            else:
                try:
                    subprocess.Popen(["xdg-open", DJ_URL], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    notice = "In Spotify, choose Omarchy DJ and start DJ X."
                except OSError:
                    notice = "Open Spotify, choose Omarchy DJ and start DJ X."
            notice_until = time.monotonic() + 8
            continue
        if key == "o":
            value = prompt_link(window, colors, should_stop)
            if value:
                try:
                    uri = spotify_uri(value)
                    if not demo:
                        worker.command("/player/play", {"uri": uri})
                    notice = "Opening Spotify link…" if not demo else "Demo mode: no Spotify commands are sent."
                except ValueError as exc:
                    notice = str(exc)
                notice_until = time.monotonic() + 6
            continue
        status = state.get("status") or {}
        command, payload = None, None
        if key == " ":
            command = "/player/playpause"
            if demo:
                now = time.monotonic()
                if demo_paused:
                    start += now - demo_paused_at
                    demo_paused_at = None
                else:
                    demo_paused_at = now
                demo_paused = not demo_paused
        elif key in ("n", "p"):
            command = "/player/next" if key == "n" else "/player/prev"
        elif key in ("+", "=", "-", "_"):
            step = max(1, round((status.get("volume_steps") or 100) * 0.05))
            step *= 1 if key in ("+", "=") else -1
            command, payload = "/player/volume", {"volume": step, "relative": True}
            demo_volume = max(0, min(100, demo_volume + step))
        elif key == "m":
            volume = status.get("volume") or 0
            if volume:
                muted_volume = volume
                volume = 0
            else:
                volume = muted_volume or round((status.get("volume_steps") or 100) * 0.5)
            command, payload = "/player/volume", {"volume": volume}
            demo_volume = volume
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            command, payload = "/player/seek", {"position": -10000 if key == curses.KEY_LEFT else 10000, "relative": True}
        if command and not demo:
            if state.get("ready") and state.get("connected"):
                worker.command(command, payload)
            else:
                notice, notice_until = "Waiting for Spotify to connect…", time.monotonic() + 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="preview the interface without Spotify or audio capture")
    parser.add_argument("--no-autoplay", action="store_true", help="leave playback idle on launch; press d to start DJ X")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("run djamp in an interactive terminal")
    os.umask(0o077)
    player = worker = monitor = None
    shutdown_requested = False
    previous_handlers = {}

    def request_shutdown(signum, frame):
        nonlocal shutdown_requested
        # Defer cleanup until startup/input returns; interrupting Popen could
        # leave a child running before PlayerProcess has recorded ownership.
        shutdown_requested = True

    def should_stop():
        return shutdown_requested

    try:
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        if not args.demo and not shutdown_requested:
            api = API()
            player = PlayerProcess(api)
            player.start()
            worker, monitor = PlayerWorker(api, autoplay=not args.no_autoplay), AudioMonitor()
            worker.start()
            monitor.start()
        if not shutdown_requested:
            curses.wrapper(run_ui, worker, monitor, player, args.demo, should_stop)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, curses.error) as exc:
        if shutdown_requested:
            return 0
        print(f"djamp: {clean(exc)}", file=sys.stderr)
        return 1
    finally:
        try:
            if worker:
                worker.stop_event.set()
            if monitor:
                monitor.close()
        finally:
            try:
                if player:
                    player.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
