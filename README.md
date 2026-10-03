# DJamp

A compact, CLIamp-inspired terminal controller for go-librespot. It keeps the
existing Spotify DJ X playback and login, with now playing, a progress bar,
volume, keyboard controls, output spectrum/waveform views, and recent tracks
from the current session. It is a custom front end, not a CLIamp plugin.

## Requirements

- Linux with Python 3.10 or later, including the standard `curses` module.
- [go-librespot](https://github.com/devgianlu/go-librespot) v0.10.2; other versions
  have not been validated. Install its executable at `~/.local/bin/go-librespot`.
- Spotify Premium and a working go-librespot login for live playback.
- PipeWire's PulseAudio compatibility service or PulseAudio. The optional
  visualizer requires `parec`.
- A terminal of at least 58 columns by 24 rows.

The Python application has no third-party runtime dependencies. Its offline
preview and unit tests do not require Spotify, the audio tools, or network access.

## Backend setup

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

Choose **Omarchy DJ** in Spotify and start DJ X as before. DJamp starts
go-librespot when needed and stops that child on exit. If it attaches to an
already-running player with the local API enabled, quitting leaves that player
running. Existing credentials remain in `~/.config/go-librespot`.

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
| d | Open DJ X in Spotify |
| ? | Help |
| q / Ctrl+C | Quit |

Starting a DJ session still happens in Spotify. This first version does not
browse your full Spotify library or change the DJ's mood. Track links,
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
- Backend: `~/.local/bin/go-librespot`.
- Backend config: `~/.config/go-librespot/config.yml`.
- API: `127.0.0.1:3678`, with metadata enabled for the next track.
- Backend logs: `~/.local/state/djamp/player.log`, readable only by your user.

The example config also prefers Spotify's firewall-friendly access point ports
to avoid a connection-refused delay on port 4070. Actual configuration, login
credentials, downloaded executables, and playback logs live outside this
repository.

API contracts follow the [go-librespot v0.10.2 specification](https://github.com/devgianlu/go-librespot/blob/v0.10.2/api-spec.yml).

## Checks

```bash
python3 -m unittest discover -v
# Or:
make test
```

The tests check API requests, link validation, playback timing, audio frequency
analysis, terminal layout/clipping, and attached-player lifecycle. Live Spotify
and desktop audio require testing from a normal desktop terminal.
