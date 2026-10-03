# DJamp backend patch

`upstream.env` pins go-librespot v0.10.2 to commit
`6a3e25019de8d2893b3fa26b0273d8cc376241c5`. The local build reports
`0.10.2-djamp.2`; it is an unofficial DJamp extension.

The patch changes resolution of exactly
`spotify:playlist:37i9dQZF1EYkqdzj48dyYq`: `POST /player/play` asks Spotify's
Lexicon session provider for a DJ session, then uses go-librespot's existing
narration, track loading and continuation handling. Other URIs keep their
original resolver. Resolver errors propagate without falling back to the empty
DJ playlist. Spotify's session endpoint is undocumented and may change.

The request includes `reason=interactive`, matching an interactive DJ launch
rather than a request to restore old session contents. Spotify still controls
the returned selection; this parameter does not guarantee different tracks
on every call. To avoid repeatedly opening the same set, the backend remembers
the continuation of the last DJ page that successfully started playing, scoped
to the authenticated account. On the next DJ start, an unchanged root selection
advances through that server-issued continuation to the next Spotify-curated
set. A changed root starts normally. This advances Spotify's existing session;
it does not force Spotify to generate a new global session or shuffle its tracks.

Root comparison combines the returned `correlation-id` with the ordered opening
track URIs/GIDs. These values were stable in observed responses; dynamic clocks
and occurrence UIDs are excluded. Lazy opening pages are resolved through a
maximum of three validated server pointers; their correlation ID is used when
absent from the root, preserving identity if Spotify later returns that opening
directly. Narration, restrictions, page order and
continuation URLs remain Spotify's originals. Saved continuation URLs are
validated for the exact DJ service and context before being requested.

Bookmarks live in the private backend `state.json` as `dj_cursors`, alongside
existing state. Prefetching, failed loads, queued tracks and paused-only loads
do not advance them. Starting a track on a later page advances the bookmark
past that page, including during a long listening session. State is saved
atomically and flushed when the backend closes. Malformed/loading contexts and
expired or malformed continuations fail visibly; they never silently restart
the previously heard opening. A new root selection supersedes old bookmarks.

`GET /` advertises `direct_dj: true`, including when a session is not yet ready.
Clients must still wait for `playback_ready: true` before starting playback.
The OpenAPI source and generated Go model are updated together. Unit tests
cover routing, result preservation, errors, consecutive DJ sets, account/root
changes, playback versus prefetch, private persistence and capability serialization.

From the DJamp repository root, run:

```sh
./scripts/build-backend
# Or stage a binary elsewhere without replacing the installed DJamp backend:
./scripts/build-backend --output /tmp/go-librespot-djamp
```

The build requires Linux, Git, Go 1.25+ (automatic toolchain download is allowed),
a C compiler, pkg-config, ALSA, Ogg, Vorbis, FLAC and mpg123 development libraries.
On Arch/Omarchy their packages are `base-devel git go alsa-lib libogg libvorbis
flac mpg123`. It downloads pinned source and Go modules as needed, applies the
patch to a fresh temporary source tree, runs offline unit tests in `daemon`,
`spclient`, `tracks` and `cmd/daemon`, then builds with a version label and atomic installation.
The upstream `go-librespot` executable and Spotify configuration are untouched.

Source objects are cached outside the repository at
`${XDG_CACHE_HOME:-~/.cache}/djamp/backend`; set `DJAMP_BACKEND_CACHE` to use another
directory. Temporary source trees are removed on exit. The source commit and
Go dependencies are pinned; bit-identical binaries also require matching Go
and C toolchains and system libraries.

To update the patch, edit a checkout at the pinned commit, regenerate the API
model with the upstream generator, and export the changes including new files:

```sh
go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.5.0 --config=api-codegen.yml api-spec.yml
gofmt -w daemon/dj_context*.go
go test -mod=readonly -tags test_unit ./daemon ./spclient ./tracks ./cmd/daemon
git add -N daemon/dj_context.go daemon/dj_context_test.go daemon/dj_start_test.go tracks/dj_cursor_test.go
git diff --binary --src-prefix=a/ --dst-prefix=b/ > /path/to/djamp/backend/go-librespot-dj-start.patch
```

Keep the API capability and version label aligned with the Python client, and
rerun `scripts/build-backend --output ...` to check application to clean source.
Offline tests do not verify Spotify access or audible DJ narration.
