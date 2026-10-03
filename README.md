# DJamp

A compact, CLIamp-inspired terminal Spotify player. It starts Spotify DJ X
directly, including narration, using your existing go-librespot login. It has a progress bar,
volume, keyboard controls, output spectrum/waveform views, and recent tracks
from the current session. It is a custom front end, not a CLIamp plugin.

## Requirements

- Linux with Python 3.10 or later, including the standard `curses` module.
- DJamp's patched [go-librespot](https://github.com/devgianlu/go-librespot)
  v0.10.2 backend, built with the command below.
- Spotify Premium and a working go-librespot login for live playback.
- PipeWire's PulseAudio compatibility service or PulseAudio. The optional
  visualizer requires `parec`.
- A terminal of at least 58 columns by 24 rows.

The Python application has no third-party runtime dependencies. Its offline
preview and unit tests do not require Spotify, the audio tools, or network access.

## Backend setup

Build and install the DJamp backend from this checkout:

```bash
./scripts/build-backend
```

The build needs Git, Go 1.25+, a C compiler, pkg-config and audio development
libraries. On Arch/Omarchy the packages are `base-devel git go alsa-lib libogg
libvorbis flac mpg123`. Source and Go modules are downloaded as needed.

The script applies the versioned patch to a pinned upstream commit, runs its
unit tests, and atomically installs `~/.local/bin/go-librespot-djamp`.
An existing `~/.local/bin/go-librespot` is left available for rollback.
See [backend/README.md](backend/README.md) for source pins and patch details.

The existing Omarchy setup is already configured. For a new installation, use
[examples/go-librespot.yml](examples/go-librespot.yml) as
`~/.config/go-librespot/config.yml`. If that file already exists, merge the
settings into it instead of replacing it. Keep this directory private because
go-librespot stores login credentials there:

```bash
mkdir -p ~/.config/go-librespot
chmod 700 ~/.config/go-librespot
# For a new installation only:
cp -n examples/go-librespot.yml ~/.config/go-librespot/config.yml
chmod 600 ~/.config/go-librespot/config.yml
```

The local API must listen on `127.0.0.1:3678`, with `metadata.enabled` set to
`true` for upcoming track information. On first launch, DJamp displays the code
to enter at [spotify.com/pair](https://spotify.com/pair). Subsequent launches
reuse go-librespot's saved credentials.

## Run from this checkout

```bash
python3 djamp.py
```

For an offline, visibly labelled preview:

```bash
python3 djamp.py --demo
```

The existing local launcher also runs this checkout. Stop an older
`try-spotify-dj` process with Ctrl+C, then run:

```bash
~/.local/bin/djamp
```

DJamp starts DJ X automatically once Spotify is ready and the player is idle.
You do not need to select **Omarchy DJ** or start DJ in Spotify. An existing
playing or paused session is preserved. On later launches, DJamp starts the next
Spotify-curated DJ set with its narration, instead of replaying the same opening.
Press **d** (or **D**) to start the next set yourself or return to DJ after playing
another Spotify link. You can also
launch with `djamp --no-autoplay` to leave playback idle until you choose.
Autoplay happens at most once per launch; stopping or reconnecting does not
restart it. A failed start displays an error; **d** retries and **b** opens
Spotify in the browser as a fallback.

DJamp starts its patched backend when needed and stops that child on exit. If it attaches to an
already-running player with the local API enabled, quitting leaves that player
running. A terminal hangup or termination signal also shuts down a backend that
DJamp started. Existing credentials remain in `~/.config/go-librespot`.

After upgrading, quit any older DJamp or `try-spotify-dj` instance before
relaunching. An attached stock backend still supports ordinary playback, but
DJamp will ask you to restart it before using direct DJ startup.

## Install the Python command

From the repository root, use a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/djamp
```

This installs the `djamp` console command in the virtual environment. It does
not install or change go-librespot, its credentials, or your audio service.

## Keys

| Key | Action |
| --- | --- |
| Space | Play/pause |
| n / p | Next/previous |
| Left / Right | Seek backward/forward 10 seconds |
| + / - | Volume up/down |
| m | Mute/restore volume |
| v | Spectrum, waveform, visualizer off |
| o | Paste a Spotify link or URI to play |
| d / D | Start the next Spotify DJ set |
| b | Open Spotify in the browser (fallback) |
| ? | Help |
| q / Ctrl+C | Quit |

Direct startup uses Spotify's undocumented DJ session endpoint and its
continuation pages, which may change. Spotify can reuse a recent session, so
DJamp remembers the continuation of the set that actually started playing and
asks Spotify for the next set on relaunch. This advances Spotify's curated
session; it does not force Spotify to regenerate a whole session or prevent
individual songs from reappearing. A new session from Spotify replaces the old
continuation. Prefetching upcoming music does not advance the saved position.

This requires DJ availability on your account. Voice followed by music
has been verified live on the existing account; first-ever DJ setup has not.
This version does not browse your full Spotify library or change the DJ's mood. Track links,
playlist links and the current DJ session use the same playback engine.
An upcoming title is shown only when go-librespot provides it; the recent
list is session history, not a fabricated DJ queue.

The visualizer uses `parec --device=@DEFAULT_MONITOR@`: it samples your current
audio output in memory, including other sounds playing through that output.
It does not select the microphone or save audio. An unavailable monitor does
not disable playback controls.

## Local setup

- Source in this checkout: `djamp.py`.
- Existing Omarchy launcher: `~/.local/bin/djamp`.
- DJamp backend: `~/.local/bin/go-librespot-djamp`.
- Original backend, if installed: `~/.local/bin/go-librespot`.
- Backend config: `~/.config/go-librespot/config.yml`.
- Saved login and DJ continuation: `~/.config/go-librespot/state.json` (private).
- API: `127.0.0.1:3678`, with metadata enabled for the next track.
- Backend logs: `~/.local/state/djamp/player.log`, readable only by your user.

The example config also prefers Spotify's firewall-friendly access point ports
to avoid a connection-refused delay on port 4070. Actual configuration, login
credentials, downloaded executables, and playback logs live outside this
repository.

To use the original backend again, quit DJamp, run
`~/.local/bin/go-librespot --config_dir ~/.config/go-librespot` in another
terminal, then run `djamp --no-autoplay`. Start DJ through Spotify as before.
The same saved login and configuration are used.

API contracts follow the [go-librespot v0.10.2 specification](https://github.com/devgianlu/go-librespot/blob/v0.10.2/api-spec.yml).

## Checks

```bash
python3 -m unittest discover -v
# Or:
make test
```

The tests check API requests, link validation, playback timing, audio frequency
analysis, terminal layout/clipping, demo pause/mute controls, and process cleanup
on shutdown signals, including leaving attached players running. Startup tests
cover waiting for authentication, preserving active sessions, explicit DJ
requests, unsupported backends, and failed starts without automatic retries. Live Spotify
and desktop audio require testing from a normal desktop terminal.
