# DJamp contributor guidance

## Project

DJamp is a CLIamp-inspired Linux terminal controller for go-librespot, written
in Python 3.10+ with standard-library `curses` and no third-party Python runtime
dependencies. Preserving Spotify DJ X playback is a core requirement.
go-librespot handles authentication and streaming; DJamp provides the interface.
DJamp's pinned backend patch starts DJ sessions directly through Spotify's
session resolver. Relaunches advance to the next Spotify-curated set. Keep
narration and continuation metadata intact; this is not a forced regeneration
of Spotify's whole session.

## Repository map

- `djamp.py`: application and console entry point. `API` handles local HTTP;
  `PlayerProcess` manages the backend; `PlayerWorker` polls playback and sends
  controls; `Spectrum` and `AudioMonitor` drive the visualizer; rendering and
  keyboard handling use curses.
- `test_djamp.py`: standard-library unittest suite with mocked backend calls and
  a terminal window double.
- `test_demo.py` and `test_shutdown.py`: demo controls and isolated subprocess
  checks for shutdown signals and backend ownership.
- `test_startup.py` and `test_controls.py`: direct DJ startup and keyboard controls.
- `test_playback_recovery.py`: playback failures, cooldowns, retries and queued controls.
- `djamp_library.py`: Spotify library transport and its separate background worker.
- `test_library.py` and `test_library_ui.py`: library requests, likes and browsing.
- `backend/`: pinned upstream revision, DJ resolver patch and backend guidance.
- `scripts/build-backend`: tested build and atomic backend installation.
- `examples/go-librespot.yml`: shareable backend configuration template.
- `pyproject.toml`: setuptools packaging and the `djamp` console command.
- `README.md`: installation, controls, backend setup, and current limitations.
- `Makefile`: shortcuts for tests, live launch, and offline preview.

## Commands and validation

Run commands from this repository root, not from the home directory.

```bash
make test                         # python3 -m unittest discover -v
make backend                      # build/install patched go-librespot
make demo                         # offline preview; requires an interactive TTY
make run                          # live playback; requires configured backend
python3 djamp.py --help
```

For installation checks, create a virtual environment and install the project
with `.venv/bin/python -m pip install -e .`, as described in the README.

Run the unittest suite for code changes and add focused coverage when behavior
changes. For layout or input changes, also exercise the demo in a real terminal,
including resizing to 58 columns by 24 rows, help, and quitting. Documentation
changes alone do not require runtime tests. Packaging changes should include an
installation and console-entry-point check outside the checkout.

Offline tests and demo mode do not verify Spotify streaming, DJ narration, or
desktop audio capture. Report live checks separately; they need a normal desktop
terminal and an authenticated backend.

## Implementation constraints

- Keep curses operations on the main thread. Backend requests and audio capture
  must not block terminal input; preserve bounded requests and reliable cleanup.
- Keep library requests off the playback worker. Capture the displayed track
  URI when liking, confirm writes before changing the like indicator, and reject
  stale account/page responses. Keep access tokens in memory and out of logs.
- Preserve the committed song's library identity when reusing a relinked stream.
  Library writes require JSON and explicit browser-origin authorization; native
  clients without an Origin header remain supported.
- Reuse the Unicode sanitizing and cell-width helpers for displayed metadata.
  Keep narrow terminals, missing metadata, and pairing prompts usable.
- Follow the go-librespot v0.10.2 API specification linked in the README. The
  backend CLI option is `--config_dir`, with two leading hyphens.
- Require `direct_dj: true` before requesting a DJ session. Autoplay waits for
  authentication, runs once when idle, and preserves playing/paused sessions.
  Backend changes must apply cleanly to the pinned commit and pass its unit tests.
- Persist DJ continuation only for the page that actually starts playback,
  never a prefetched page. Bind saved continuations to the account and session;
  failed loads must not consume them. Keep ordinary playback unchanged.
- Treat opaque audio-key refusals as playback failures, not proof that a song is
  restricted. Preserve the selected song for manual retry, keep cooldowns shared
  across controls, and discard pending playback commands after such a failure.
- Keep the backend API on loopback (`127.0.0.1:3678`). Reuse saved credentials.
  Stop only backend processes that DJamp starts; leave attached players running.
- Capture the output monitor, never the microphone, for visualization. Keep
  samples in memory, make missing capture tools nonfatal, and display silence
  or stale samples honestly. Simulated activity belongs only in labelled demo
  mode, which must remain independent of Spotify and audio services.
- Recent tracks are session history. Show upcoming tracks only when the backend
  supplies them; do not invent a DJ queue.
- Prefer the existing standard-library approach. Update the README and example
  configuration when controls, requirements, or backend settings change.

## Local state

Live configuration and credentials are under `~/.config/go-librespot`; the
backend executable and local launcher are under `~/.local/bin`; backend logs
are under `~/.local/state/djamp`. These are outside the repository. Use temporary
files and mocks for tests, preserve existing user configuration, and keep
credentials, pairing codes, logs, downloaded binaries, and build output out of
version control.
