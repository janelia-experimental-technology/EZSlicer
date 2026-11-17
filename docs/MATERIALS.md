# Loaded resin and the material service

Elegoo printers (the Mars 5 Ultra included) have no setting for "which resin is in the vat", and
their firmware is closed. EZSlicer keeps track of the loaded resin itself, checks it against every
file it sends, and can watch the printers for prints that were started some other way.

## What's in a sliced file

Every `.goo` and `.ctb` that EZSlicer writes stores the resin profile's name (the **Resin** field):

- GOO: a 32-character field in the header, so long names get cut to 32 characters.
- CTB v4/v5: the resin parameters block, right after the CBD disclaimer. ChituBox lays it out the same way.

`resin_slicer/material_tags.py` reads it back (`read_file_material`). Names match loosely: case,
spacing and punctuation don't count, and a GOO name cut to 32 characters matches the full name it
starts with.

## In the app

Under **Printers**, each printer has a **Resin** field (it suggests your resin profiles). When you
**Send to Printer**, EZSlicer reads the resin from the sliced file. If that resin differs from the
loaded one, it asks before sending: **Send Anyway** or **Cancel**. If no resin is set for the
printer, there's no check.

The loaded resin lives in one of two places:

- **This PC only** (Material service left blank): it's stored with the printer list on this PC.
  Nothing watches the printers.
- **A material service** (address, plus token if the service needs one): shared by everyone who
  connects to that service. The service also watches the SDCP printers (described below), and
  each SDCP printer gets a **Watch** setting.

  When you connect, printers that are new to the service are added to it, carrying any resin you
  had set locally. After each send, EZSlicer tells the service which file it uploaded and which
  resin that file is for. Watcher warnings for printers in your list show up as messages. If the
  watcher paused a print, you get a pop-up.

## The material service

```powershell
python -m resin_slicer.material_service --host 0.0.0.0 --port 8070 --data C:\ProgramData\EZSlicer `
    [--token <secret>] [--webhook <url>] [--poll 5] [--stats-url <url>] [--stats-token-file <file>]
```

It's standard library only and needs no extra packages. Data is kept in
`<data>\printer-materials.json`. If `--token` (or `MATERIAL_SERVICE_TOKEN`) is set, every change
needs `Authorization: Bearer <token>`; reads stay open. Changes are attributed to the
`X-Jet-User` header (the jET gateway sets it), else to `by` in the body.

| Route | Does |
| --- | --- |
| `GET /api/health` | `{ok, printers, watching}` |
| `GET /api/v1/printers` | every printer: `key, name, ip, protocol, mainboardId, material, materialSetBy, materialSetAt, policy, watch, live` |
| `GET /api/v1/printers/{key}` | one printer |
| `PUT /api/v1/printers/{key}` | add or update one; any of `name, ip, protocol, mainboardId, material, policy, watch` |
| `DELETE /api/v1/printers/{key}` | forget a printer |
| `POST /api/v1/jobs` | `{printerKey, remoteName, material}`: what a file sent to that printer was sliced for. Optional, for jET's stats: `name`, `person` (who sent it), `parts` (models on the plate), `volumeMl` (the slice's resin), `seconds` (estimated print time) |
| `GET /api/v1/events?since=N&limit=M` | `{events, last}`: watcher warnings, oldest first |

`key` is the printer's SDCP MainboardID, or its IP for ChiTu boards (which don't report one).

### The watcher

The watcher keeps a connection open to each SDCP printer that has `watch` on, and checks the
printer every `--poll` seconds. When a print starts, whether from EZSlicer, another slicer, or a
USB stick, it looks up the file in the job ledger and compares that file's resin with the
loaded resin:

| Policy | Wrong resin | File it doesn't know (e.g. USB) |
| --- | --- | --- |
| `warn` (default) | event | event |
| `pause` | event + pause | event |
| `strict` | event + pause | event + pause |

- **Already printing when the watcher connects:** such prints are reported, never paused.
- **Late ledger entries:** EZSlicer records a job right after the upload, so a file the ledger
  doesn't know yet gets a 20 s grace period before it counts as unknown.
- **Pausing:** if the printer refuses a pause while it's still homing, the watcher retries for
  about 12 s.
- **Webhook:** each event is also POSTed to `--webhook` as `{"text": message, "event": {...}}`,
  which suits Teams/Slack-style incoming webhooks.

ChiTu-board printers (e.g. the Jupiter) don't report which file is printing, so they get the
send-time check only.

### jET's numbers (stats)

With `--stats-url`, the service reports each resin job to jET's stats, which jET Hangar, the
kiosk tablet and the foyer TV show. On jetliquid that's the console agent's
`POST /api/v1/stats/jobs`, with the console token from `--stats-token-file`, so there's no new
key. Each report is
`{app: "ezslicer", job_id, kind: "resin", status, printer, material, name, parts, volume_cm3, seconds}`,
and later reports with the same `job_id` update the job:

| When | `status` | Numbers |
| --- | --- | --- |
| EZSlicer records a sent file (`POST /api/v1/jobs`) | `sent` | the slice's parts, resin ml and estimated time |
| The watcher sees a print start, however it was started | `printing` | the same, for a file it knows |
| The printer goes idle again | `done`, `failed` or `cancelled` | the real time, timed from the start the watcher saw |

- **How a print ended:** the printer says so (Complete is `done`, Stopped is `cancelled`, an
  error number is `failed`). If it doesn't say, a print that reached its last layer is `done`.
- **Reprints:** the first print of a sent file is that send's job. Printing the same file again
  from the printer's screen is a new job with the same numbers.
- **Files it doesn't know** (USB, another slicer): counted as jobs, with no resin volume.
- **Restarts:** the print in progress is kept in the data file, so it stays one job.
- **`material`** is the printer's loaded resin, or the file's resin if none is set.
- **`person`** (for the leaderboards) is whoever's EZSlicer window sent the file: its Windows
  account (`HHMI\name`), sent as `person` with `POST /api/v1/jobs`, else the `X-Jet-User` header.
  It rides on that send's reports. Reprints started at the printer and files it doesn't know
  have no person, so they count for nobody.
- **Delivery:** one try per report. A 404 or no answer is dropped quietly, so stats never hold
  up a print.

Only EZSlicer windows pointed at the service (Printers → Loaded resin) send the numbers. Prints
started any other way are still counted, but without resin volume or parts.

### Deployment (jetliquid-ww1)

```powershell
powershell -File deploy\material-service\deploy.ps1   # from Windows PowerShell in the repo
```

This copies only the four modules the service needs (`errors`, `material_tags`,
`network_printers`, `material_service`) to `C:\ProgramData\EZSlicerMaterials\app`, then runs
`install.ps1` on the host. That script:

- creates `token.txt` the first time (readable by SYSTEM and Administrators only);
- opens TCP 8070 (firewall rule "EZSlicer Material Service 8070");
- registers the scheduled task "EZSlicer Material Service" (SYSTEM, at startup) and restarts it.

The task runs the `run-material-service.ps1` supervisor, which restarts the service whenever it
exits. Logs go to `logs\service-YYYYMMDD.log` and data to `data\printer-materials.json`. To add a
webhook, put its URL in `webhook.txt` and rerun the deploy.

Address: `http://jetliquid-ww1:8070`. To read the token:
`ssh jetliquid-ww1 type C:\ProgramData\EZSlicerMaterials\token.txt`.

## Engine (jET Hangar)

- `POST /api/v1/files/material` `{path}` returns `{material}`.
- `POST /api/v1/printers/send` takes `loadedMaterial` (and `allowMaterialMismatch`). A file sliced
  for another resin is refused with `409 material_mismatch`, with details
  `{fileMaterial, loadedMaterial}`, before anything is uploaded.
- The send result now includes `material`.
- Capabilities: `printers.material-check`, `files.material`.
