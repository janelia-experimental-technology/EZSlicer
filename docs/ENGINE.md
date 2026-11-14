# EZSlicer engine

EZSlicer's slicer and resin-printer connections without the window, for jET Hangar and for
EZSlicer's own window. API version **1.0** (`resin_slicer/engine_info.py`).

## Starting it

| How | Command |
| --- | --- |
| From the engine package (any Python 3.10+, embeddable too) | `python <package folder>/ezslicer_engine.py --engine` |
| From the source | `python -m resin_slicer.engine --engine` (run in the folder that holds `resin_slicer`) |
| From the portable app | `EZSlicer.exe --engine` |

- **Embeddable Python:** python.org's embeddable build has a `._pth` file, so it imports only from
  the folders listed there, never the current folder. `python -m resin_slicer.engine` then fails
  with "No module named 'resin_slicer'"; use `ezslicer_engine.py`, which finds its own folder.
  (Adding the package folder to the `._pth` file works too.) The engine's own child processes
  find the package the same way, so they work either way.
- Set `JET_ENGINE_TOKEN` to a secret first. The engine won't start without it.
- It listens on `127.0.0.1` on a free port, then prints one JSON line on stdout:
  `{"engine": "ezslicer", "version": "0.2.0", "api": "1.0", "port": 51234}`.
  `EZSlicer.exe --engine` prints a blank line first (Electron does that on Windows), so read the
  first non-blank line.
- It exits when the process that started it exits. `--parent-pid N` or `JET_ENGINE_PARENT_PID`
  watches a different process instead.
- Optional: `--port`, `--host` (or `EZSLICER_ENGINE_PORT` and `EZSLICER_ENGINE_HOST`), and
  `RESIN_SLICER_STEP_PYTHON` (a Python with the CAD kernel, for STEP models).
- `EZSLICER_ENGINE_LOG=1` logs each request to stderr.

## Sign-in

`/api/health`, `/api/version`, `/api/capabilities` and `/api/openapi.json` answer anyone. Every
other route needs `Authorization: Bearer <JET_ENGINE_TOKEN>` and answers `401 unauthorized`
without it.

## Routes

The full description is at `GET /api/openapi.json`. Results have the same shape as the JSON-lines
bridge's messages, including the `type` field.

| Route | Does | Bridge command |
| --- | --- | --- |
| `GET /api/v1/info` | engine id, version, API version, commit, date, Python | `info` |
| `GET /api/v1/profiles` | built-in machine profiles | `profiles` |
| `POST /api/v1/preview` | plans supports for the viewport (streams `support` and `progress`) | `preview` |
| `POST /api/v1/slice` | writes a `.ctb` or `.goo` file; `previewDir` and `previewScale` choose where the layer images go and their size | `slice` |
| `POST /api/v1/printers/discover` | `{targets, timeout, broadcast}` → `{printers}` | `printer-discover` |
| `POST /api/v1/printers/status` | `{printer}` → `{status}` | `printer-status` |
| `POST /api/v1/printers/status-all` | `{printers}` → `{statuses}`; a printer that doesn't answer gets `ok:false`, the request itself doesn't fail | `printer-status-all` |
| `POST /api/v1/printers/control` | `{printer, action: pause, resume or stop}` | `printer-control` |
| `POST /api/v1/printers/send` | `{printer, path, start}`; refuses the wrong format for that printer, and refuses while it's printing (streams `progress` with `fraction`) | `printer-send` |

- **Long jobs** (preview, slice, send): send `Accept: application/x-ndjson`, or add `?stream=1`,
  to get every message as it happens, one per line, with the result last. Blank lines are
  keep-alives. Without either, the response is just the result.
- **Hanging up:** preview and slice each run in their own Python process. A client that hangs up
  during a stream stops the job.
- **Printer connections:** the engine owns them, and talks to each printer one call at a time.
- **Errors** always look like `{"error": {"code", "message", "details"}}`:

  | Status | Codes |
  | --- | --- |
  | 400 | `invalid_request`, `invalid_json` |
  | 401 | `unauthorized` |
  | 404 | `not_found` |
  | 405 | `method_not_allowed` |
  | 422 | `failed` (the slicer couldn't do it) |
  | 502 | `printer_error`, `unreachable` |

  In a stream, the last line is `{"type": "error", "message", "error": {...}}`.

## Capabilities

`engine.info`, `profiles.list`, `supports.preview`, `slice.run`, `slice.layer-images`,
`formats.ctb`, `formats.goo`, `printers.discover`, `printers.status`, `printers.status-all`,
`printers.send`, `printers.control`.

## JSON-lines bridge (unchanged, still supported)

`python -m resin_slicer.electron_bridge <command>` (or, from the engine package with any Python,
`python <package folder>/ezslicer_engine.py bridge <command>`), with the JSON request on stdin. It accepts
the same commands as the table above (`info`, `profiles`, `preview`, `slice`,
`printer-discover`, `printer-status`, `printer-status-all`, `printer-send`, `printer-control`)
and prints the same messages. These are the commands jET Hangar's `hangar_bridge.py` copy
provided, so Hangar can run them straight from the engine package.

## Contract tests

```bash
python -m pytest tests/contract
```

That starts a local engine and tests it. To test an engine that's already running:

```bash
EZSLICER_ENGINE_URL=http://127.0.0.1:51234 JET_ENGINE_TOKEN=<secret> python -m pytest tests/contract
```

They never reach a real printer; printer calls go to `127.0.0.1`. `npm run release` runs them
(they're part of `pytest`) before it tags.

## Release packages

A `v*` tag builds these in GitHub Actions (`.github/workflows/release.yml`) and attaches them to
the release:

| Package | Contents |
| --- | --- |
| `EZSlicer-Engine-<version>.zip` + `.sha256` | `resin_slicer/` (with `BUILD.json`: version, commit, date, tag), `ezslicer_engine.py`, `version.txt`, `ENGINE.md`, `LICENSE`. Runs on any Python 3.10+, with nothing to install. |
| `EZSlicer-Windows-Portable-<version>.zip` + `.sha256` | the app with its own Python. `version.txt` is next to `EZSlicer.exe`. |

To build the engine package locally, run `python tools/package_engine.py`. It refuses
uncommitted changes in `resin_slicer` unless you pass `--allow-dirty`.

## For jET Hangar: moving off the vendored copy

- Hangar's `hangar_bridge.py` commands map one to one onto the routes above, and
  `resin_slicer.electron_bridge` accepts them too.
- **Differences from `hangar_bridge.py`:**
  - `slice` defaults to `format: "goo"`, where Hangar's copy defaulted to `"ctb"`. Always send
    `format`; the Jupiter needs `ctb`.
  - `info` no longer reads `UPSTREAM.json`. It returns `app`, `version`, `api`, `commit`, `date`
    and `python`, and still includes `engine: "EZSlicer"`.
  - `slice` also returns `previewLayerCount`.
- **Nothing was removed or renamed:** every message type Hangar handles (`progress`, `support`,
  `preview`, `done`, `info`, `printer-statuses`, `printers`, `printer-status`, `printer-sent`,
  `error`) is unchanged.
