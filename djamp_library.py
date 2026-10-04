"""Liked Songs requests, isolated from the playback worker.

The local backend uses the existing Spotify login. Library HTTP calls run on a
separate thread, never on the curses or playback threads. No tokens leave the
backend or get written to disk by this module.
"""

from collections import deque
import copy
import http.client
import json
import re
import threading
import socket
from urllib.parse import urlencode


TRACK_URI = re.compile(r"spotify:track:[A-Za-z0-9]{22}\Z")
PAGE_SIZE = 20
MAX_RESPONSE = 2_000_000


class LibraryError(Exception):
    """A safe, user-facing error that contains no token or response body."""


class Cancelled(LibraryError):
    pass


def track_uri(value):
    return isinstance(value, str) and bool(TRACK_URI.fullmatch(value))


def empty_state():
    return {"items": [], "offset": 0, "limit": PAGE_SIZE, "total": 0,
            "has_next": False, "loading": False, "loaded": False, "error": "",
            "notice": "", "current_uri": "", "liked": None, "like_pending": False, "pending_uri": ""}


def normalize_page(data, offset, limit):
    """Validate the backend contract before any metadata reaches the UI."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise LibraryError("The player returned an invalid Liked Songs page.")
    if any(type(data.get(key)) is not int or data[key] < 0
           for key in ("offset", "limit", "total")):
        raise LibraryError("The player returned invalid library pagination.")
    if (data["offset"] != offset or data["limit"] != limit or len(data["items"]) > limit
            or type(data.get("has_next")) is not bool):
        raise LibraryError("The player returned an unexpected library page.")
    items = []
    for song in data["items"]:
        if not isinstance(song, dict):
            raise LibraryError("The player returned an invalid saved song.")
        uri = song.get("uri", "")
        artists = song.get("artist_names", [])
        if (not isinstance(artists, list) or any(not isinstance(name, str) for name in artists)
                or type(song.get("playable")) is not bool):
            raise LibraryError("The player returned invalid song metadata.")
        duration = song.get("duration", 0)
        items.append({
            "uri": uri if track_uri(uri) else "",
            "name": song.get("name") if isinstance(song.get("name"), str) else "Unavailable song",
            "artist_names": list(artists),
            "album_name": song.get("album_name") if isinstance(song.get("album_name"), str) else "",
            "duration": max(0, duration) if type(duration) is int else 0,
            "playable": track_uri(uri) and song["playable"],
        })
    return {"items": items, "offset": offset, "limit": limit, "total": data["total"],
            "has_next": data["has_next"]}


class BackendLibrary:
    """Library transport with its own timeout and no effect on playback polls."""

    def __init__(self, api, *, connection_factory=http.client.HTTPConnection):
        self.api = api
        self.connection_factory = connection_factory
        self.username = ""
        self.supported = False
        self._connection = None
        self._connection_lock = threading.Lock()

    def reset(self, username):
        self.username = username
        self.supported = False

    def close(self):
        with self._connection_lock:
            if self._connection is not None:
                # shutdown also interrupts a socket read in the worker thread.
                sock = self._connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                self._connection.close()

    def _capability(self, valid):
        if not valid():
            raise Cancelled()
        if not self.supported:
            try:
                root = self.api.request("/")
            except Exception:
                raise LibraryError("Cannot connect to the Spotify player. Try again.") from None
            if not isinstance(root, dict) or root.get("library") is not True:
                raise LibraryError("Library support needs an updated player. Run make backend, then restart DJamp.")
            self.supported = True
        if not valid():
            raise Cancelled()

    def _request(self, path, params, valid, *, payload=None):
        self._capability(valid)
        connection = self.connection_factory("127.0.0.1", self.api.port, timeout=30)
        try:
            with self._connection_lock:
                if not valid():
                    raise Cancelled()
                self._connection = connection
            if params:
                path += "?" + urlencode(params)
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request("POST" if payload is not None else "GET", path, body=body,
                               headers={"Content-Type": "application/json"} if body else {})
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE + 1)
            if not valid():
                raise Cancelled()
            if response.status == 409:
                raise LibraryError("Spotify account changed. Try again after reconnecting.")
            if response.status == 404:
                self.supported = False
                raise LibraryError("Library support needs an updated player. Run make backend, then restart DJamp.")
            if not 200 <= response.status < 300:
                raise LibraryError(f"Spotify library request failed (HTTP {response.status}). Try again.")
            if len(body) > MAX_RESPONSE:
                raise LibraryError("The player returned too much library data.")
            try:
                return json.loads(body) if body.strip() else None
            except (UnicodeDecodeError, ValueError):
                raise LibraryError("The player returned invalid library data.") from None
        except (OSError, http.client.HTTPException):
            raise LibraryError("Spotify library connection failed. Try again.") from None
        finally:
            connection.close()
            with self._connection_lock:
                if self._connection is connection:
                    self._connection = None

    def page(self, offset, limit, valid, *, refresh=False):
        data = self._request("/library/tracks", {"offset": offset, "limit": limit, "refresh": "true" if refresh else "false",
                                                "username": self.username}, valid)
        return normalize_page(data, offset, limit)

    def contains(self, uri, valid):
        if not track_uri(uri):
            raise LibraryError("Only Spotify songs can be liked.")
        data = self._request("/library/contains", {"uri": uri, "username": self.username}, valid)
        if not isinstance(data, dict) or type(data.get("saved")) is not bool:
            raise LibraryError("The player returned an invalid like status.")
        return data["saved"]

    def set_saved(self, uri, saved, valid):
        if not track_uri(uri):
            raise LibraryError("Only Spotify songs can be liked.")
        data = self._request("/library/save", None, valid,
                             payload={"uri": uri, "saved": saved, "username": self.username})
        if not isinstance(data, dict) or data.get("saved") is not saved:
            raise LibraryError("Spotify did not confirm the like change. Refresh and try again.")


class LibraryWorker(threading.Thread):
    """Coalesced reads and serialized, explicitly requested like changes."""

    def __init__(self, api, *, transport=None):
        super().__init__(daemon=True, name="djamp-library")
        self.client = transport or BackendLibrary(api)
        self.condition = threading.Condition()
        self.stopping = False
        self.username = ""
        self.ready = False
        self.epoch = 0
        self.page_epoch = 0
        self.commands = deque()
        self.state = empty_state()
        self._client_epoch = -1

    def snapshot(self):
        with self.condition:
            return copy.deepcopy(self.state)

    def _queue(self, kind, value=None, *, refresh=False):
        # At most one pending page/check, and one mutation. Rapid navigation
        # never creates an unbounded queue or sends a write twice.
        if kind != "toggle":
            self.commands = deque(job for job in self.commands if job[0] != kind)
        self.commands.append((kind, self.epoch, self.page_epoch, self.username, value, refresh))
        self.condition.notify()

    def observe(self, username, uri, ready):
        username = username if isinstance(username, str) else ""
        ready = bool(ready and username)
        uri = uri if track_uri(uri) else ""
        with self.condition:
            if self.stopping:
                return
            if username != self.username or ready != self.ready:
                self.epoch += 1
                self.page_epoch += 1
                self.commands.clear()
                self.state = empty_state()
                self.username, self.ready = username, ready
            if self.state["current_uri"] != uri:
                self.state.update(current_uri=uri, liked=None)
                if ready and uri:
                    self._queue("check", uri)

    def browse(self, offset=0, *, refresh=False):
        with self.condition:
            if self.stopping:
                return
            if not self.ready:
                self.state["error"] = "Connect to Spotify before opening Liked Songs."
                return
            if type(offset) is not int or offset < 0:
                self.state["error"] = "Invalid Liked Songs page."
                return
            self.page_epoch += 1
            self.state.update(loading=True, error="", notice="")
            self._queue("page", offset, refresh=refresh)

    def toggle(self, uri):
        with self.condition:
            if self.stopping or self.state["like_pending"]:
                return
            if not self.ready:
                self.state["error"] = "Connect to Spotify before liking a song."
                return
            if not track_uri(uri):
                self.state["error"] = "Only Spotify songs can be liked."
                return
            self.state.update(like_pending=True, pending_uri=uri, error="", notice="")
            self._queue("toggle", uri)

    def _valid(self, epoch):
        with self.condition:
            return not self.stopping and self.ready and self.epoch == epoch

    def _perform(self, job):
        kind, epoch, page_epoch, username, value, refresh = job
        if not self._valid(epoch):
            return
        if self._client_epoch != epoch:
            self.client.reset(username)
            self._client_epoch = epoch
        valid = lambda: self._valid(epoch)
        try:
            if kind == "page":
                result = self.client.page(value, PAGE_SIZE, valid, refresh=refresh)
                # Removing the last item of a final page moves back one page.
                if not result["items"] and value and result["total"] < value + 1:
                    result = self.client.page(max(0, ((result["total"] - 1) // PAGE_SIZE) * PAGE_SIZE),
                                              PAGE_SIZE, valid)
            elif kind == "check":
                result = self.client.contains(value, valid)
            else:
                # Recheck on the worker: external clients may have changed the
                # saved state since this song first appeared on screen.
                result = not self.client.contains(value, valid)
                if not valid():
                    return
                self.client.set_saved(value, result, valid)
            with self.condition:
                if not self._valid(epoch):
                    return
                if kind == "page":
                    if page_epoch == self.page_epoch:
                        self.state.update(result, loading=False, loaded=True, error="")
                    if refresh and self.state["current_uri"]:
                        # The refreshed collection may have changed in another
                        # client. Check the song now displayed after refreshing
                        # the cache, even if navigation superseded this page.
                        self._queue("check", self.state["current_uri"])
                elif kind == "check" and self.state["current_uri"] == value:
                    self.state["liked"] = result
                elif kind == "toggle":
                    self.state.update(like_pending=False, pending_uri="", error="",
                                      notice="Saved to Liked Songs." if result else "Removed from Liked Songs.")
                    if self.state["current_uri"] == value:
                        self.state["liked"] = result
                    if self.state["loaded"] or self.state["loading"]:
                        self.page_epoch += 1
                        self.state["loading"] = True
                        pending_page = next((job[4] for job in self.commands if job[0] == "page"),
                                            self.state["offset"])
                        self._queue("page", pending_page)
        except LibraryError as error:
            with self.condition:
                if not self._valid(epoch):
                    return
                if kind == "page" and page_epoch != self.page_epoch:
                    return
                if kind == "check" and self.state["current_uri"] != value:
                    return
                self.state["error"] = str(error) or "Spotify account changed. Try again."
                if kind == "page":
                    self.state["loading"] = False
                elif kind == "toggle":
                    self.state["like_pending"] = False
                    self.state["pending_uri"] = ""
        except Exception:
            # Neither a transport exception nor malformed upstream content may
            # leak bearer tokens/URLs or kill the worker without a UI error.
            with self.condition:
                if self._valid(epoch):
                    self.state["error"] = "Could not load the Spotify library. Try again."
                    if kind == "page" and page_epoch == self.page_epoch:
                        self.state["loading"] = False
                    elif kind == "toggle":
                        self.state["like_pending"] = False
                        self.state["pending_uri"] = ""

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.commands)
                if self.stopping:
                    return
                job = self.commands.popleft()
            self._perform(job)

    def close(self):
        with self.condition:
            self.stopping = True
            self.epoch += 1
            self.commands.clear()
            self.condition.notify_all()
        self.client.close()
        if self.is_alive():
            self.join(timeout=2)
