# AGENTS.md

Guide for coding agents working in this repository. `CLAUDE.md` only points here.

## What this is

`vispek-hc-viewer` is a local web viewer for the Vispek HC1500 multispectral camera. A
Python program serves one page on 127.0.0.1; the page shows the live picture, switches
LEDs, records scans and white references, and shows pictures and spectra of what was
recorded. Everything that touches the device or the data goes through the `vispek-hc`
SDK ([vispek/vispek-hc-camera](https://github.com/vispek/vispek-hc-camera), on PyPI as
`vispek-hc`). `uv.lock` pins it, like every other dependency, to a release on PyPI with
its hashes. To work against an SDK checkout, run `uv pip install -e ../vispek-hc-camera`
after `uv sync`; the next `uv sync` puts the locked release back.

Status: pre-release (0.1). Tried on one real device on macOS. `--simulate` runs
everything without hardware.

## Rules that must never be weakened

Device safety:

1. **UV gate.** LEDs 1-4 are lit only when the program was started with `--allow-uv`. The
   server enforces it (`Hub.allow_uv` is passed to the SDK); the page only mirrors it.
2. **One driver.** While a job runs, every other operation that switches an LED or sets a
   camera parameter is refused (`Hub._idle`).
3. **Nothing stays lit.** A job that fails or is stopped ends with all LEDs off; one
   that does not stop in time is overruled (`Hub._halt`). An LED lit by hand goes off
   after `IDLE_OFF_S` without contact from a page (a request with the token, or a frame
   delivered to an open live stream). Ending the program closes the device. An OFF that
   is not confirmed is reported and shown as unknown, never as off, also after the
   device was closed.

Security of the server:

4. It binds 127.0.0.1 only. There is no option for another address; do not add one.
5. Every `/api/` route needs `Authorization: Bearer <token>`. The token is never accepted
   in a query string and never logged. The live stream takes a single-use ticket.
6. Requests with a foreign `Host` or `Origin` are refused. No CORS header is sent.
7. A capture or calibration is addressed by its folder name inside the data folder
   (`library.NAME`). No request carries a path, and links are not followed.
8. The page keeps to its content security policy: no inline script or style, nothing
   loaded from the network, no `innerHTML`. Text from the device or the data folder is
   set with `textContent`.

Nothing is ever deleted or overwritten in the data folder (thumbnails in `.cache`
excepted): a name that is taken is refused, also when two requests ask for it at once.

## Commands

```bash
uv sync --group dev
uv run pytest                              # no hardware, no network, about 25 s
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run vispek-hc-viewer --simulate         # the page on the simulated device
uv run vispek-hc-viewer --simulate --no-browser --port 0 --data-dir /tmp/data
```

`node --check src/vispek_hc_viewer/static/app.js` is run by the tests when `node` exists.
CI (`.github/workflows/ci.yml`) runs the same on Linux and macOS with `--locked`; so does
the release workflow, so that a release is built only with locked, hash-checked packages.

## Release

Version in `pyproject.toml` (then `uv lock`), a `[x.y.z] - date` section in
`CHANGELOG.md`, annotated tag `vx.y.z`. `.github/workflows/release.yml` checks tag against
version, tests, builds, tries the wheel in a clean environment, and waits for a
maintainer to approve the `pypi` environment; PyPI trusts that workflow (trusted
publishing, no token). A viewer release that needs a newer SDK raises the lower bound
of `vispek-hc` first, after that SDK version is on PyPI.

## Repository map

| Path | Responsibility |
|---|---|
| `src/vispek_hc_viewer/__main__.py` | the command: options, start, shutdown; runs device opening on the main thread |
| `src/vispek_hc_viewer/server.py` | HTTP: who may ask, the route table, errors as JSON, static files, the live stream |
| `src/vispek_hc_viewer/hub.py` | the device: connection, LEDs, camera parameters, jobs, live picture, idle switch-off |
| `src/vispek_hc_viewer/library.py` | the data folder: captures, calibrations, pictures, spectra, exports, thumbnails |
| `src/vispek_hc_viewer/simrig.py` | the SDK's simulated device on a real-time clock, readable from several threads |
| `src/vispek_hc_viewer/static/` | the page: `index.html`, `app.js`, `style.css`; no framework, no build step |
| `tests/` | `test_hub.py` and `test_server.py` run on the SDK's virtual-clock simulator: a scan takes milliseconds |
| `docs/http-api.md` | every route |
| `docs/security.md` | what the server defends against, and what it does not |

## How it hangs together

- `Hub` is the only caller of the SDK's `Session`. Manual operations and the start of a
  job take `Hub._ops`; a job then runs on its own thread and makes `_idle()` fail for
  everyone else until it ends. The live picture is read without that lock: the camera
  hands frames to any thread.
- A job is a closure built and checked before its thread starts (`_scan_work`,
  `_frame_work`, `_check_work`): a request that must be refused creates no job and
  lights nothing.
- `Library` loads cubes through the SDK and keeps the last three in memory. A picture is
  `vispek_hc.render(..., banner=False)`; the page shows RAW or CALIBRATED itself.
- The page polls `/api/state` once a second (three times while a job runs) and redraws
  from it. Lists are rebuilt only when their content changes, so that inputs keep focus.
- Errors are the SDK's: `{"error": {"code", "message", "hint", "details"}}`. The page
  shows `message` and `hint`; the HTTP status is secondary (`server.status_of`).

## Recipes

- **Add a route.** A handler `(app, request) -> Response` and a line in `server.ROUTES`;
  a row in `docs/http-api.md`; a test in `tests/test_server.py`.
- **Add a job kind.** A name in `hub.JOB_KINDS`, a `_work` builder that validates before
  it returns, a branch in `Hub.start_job`, a label in `jobName` of `app.js`.
- **Add a view.** Views come from the SDK (`vispek_hc.views`). Add the button to
  `VIEW_BUTTONS` in `app.js`; if the view has a single scale, `Library.scale` reports it.
- **Try a change by hand.** `--simulate`, then scan the "White board" scene with "White
  reference", switch to "Sample", and scan.

## Do not

- Light an LED, or let one stay lit, outside the three device rules above.
- Add an option to bind another address, accept the token anywhere but the header, or
  send CORS headers.
- Take a path from a request.
- Use `innerHTML`, inline handlers or inline styles in the page, or load anything from
  the network.
- Introduce names or code from any other camera software (`tests/test_provenance.py`,
  [docs/provenance.md](docs/provenance.md)).
- Commit recordings. The data folder is ignored by git.
