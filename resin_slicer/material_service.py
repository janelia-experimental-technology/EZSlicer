"""Printer material service: which resin is in each printer, and a watcher that checks jobs.

    python -m resin_slicer.material_service [--host 127.0.0.1] [--port 8070] [--data DIR]
                                            [--token SECRET] [--webhook URL] [--poll 5]
                                            [--stats-url URL] [--stats-token-file FILE]

The Mars 5 Ultra (and the other SDCP printers) can't be told what resin is in the vat, so
this service keeps that for them and is what EZSlicer (and anything else) asks instead:

* ``GET  /api/v1/printers``           every printer with its loaded material and live state
* ``PUT  /api/v1/printers/{key}``     add/update a printer; ``{"material": "..."}`` sets the resin
* ``DELETE /api/v1/printers/{key}``   forget a printer
* ``POST /api/v1/jobs``               ``{"printerKey", "remoteName", "material"}``: what a sent file is,
                                      plus optional ``name``, ``parts``, ``volumeMl``, ``seconds`` for jET's stats
* ``GET  /api/v1/events?since=N``     mismatch warnings, newest last
* ``GET  /api/health``

``key`` is the printer's SDCP MainboardID (or its IP for boards without one).

The watcher keeps a connection to every watched SDCP printer. When a print starts - from
EZSlicer, a USB stick, or another slicer - it looks up the file's material (from the job
ledger EZSlicer fills on send) and compares it with the loaded material. A mismatch is an
event (and a webhook POST); with policy ``pause`` the print is paused too, and with ``strict``
a print of a file it knows nothing about is paused as well. Prints already running when the
watcher connects are reported, never paused.

With ``--stats-url`` the service also reports every resin job to jET's numbers (the console
agent's ``POST /api/v1/stats/jobs``): ``sent`` when EZSlicer records a file, ``printing`` when a
print starts (from anywhere), then ``done``, ``failed`` or ``cancelled`` with the real time. It
sends the token in ``--stats-token-file`` (on jetliquid, the console token: no new key). A report
that can't be delivered is dropped; stats never hold up a print.

When ``--token`` (or MATERIAL_SERVICE_TOKEN) is set, every write needs
``Authorization: Bearer <token>``; reads stay open. Who made a change comes from the
``X-Jet-User`` header (the jET gateway sets it) or the body's ``by``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import network_printers
from .material_tags import materials_match

API = "/api/v1"
POLICIES = ("warn", "pause", "strict")
MAX_EVENTS = 1000
MAX_JOBS_PER_PRINTER = 500
EDITABLE = ("name", "ip", "protocol", "mainboardId", "material", "policy", "watch")
PAUSE_ATTEMPTS = 6
# EZSlicer records a job right after the upload, which may be just after the print starts;
# a file the ledger doesn't know yet gets this long before it counts as unknown.
LEDGER_GRACE_S = 20.0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def remote_basename(name: str) -> str:
    return str(name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def new_job_id(key: str) -> str:
    return f"{key}-{uuid.uuid4().hex[:12]}"


def job_stats(body: dict[str, Any]) -> dict[str, Any]:
    """The optional numbers a sender gives for jET's stats, cleaned; missing ones are left out."""
    out: dict[str, Any] = {}
    if str(body.get("name") or "").strip():
        out["name"] = str(body["name"]).strip()[:200]
    for field, kind in (("parts", int), ("volumeMl", float), ("seconds", float)):
        value = body.get(field)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = kind(value)
        except (TypeError, ValueError):
            raise ValueError(f"\"{field}\" must be a number.") from None
        if number < 0:
            raise ValueError(f"\"{field}\" can't be negative.")
        out[field] = round(number, 3) if kind is float else number
    return out


def job_outcome(status: network_printers.PrinterStatus, layer: int | None, total: int | None) -> str:
    """How a print that just stopped ended: done, failed or cancelled."""
    info = ((status.raw or {}).get("status") or {}).get("PrintInfo") or {}
    code = info.get("Status")
    if code == 9:  # Complete
        return "done"
    if code in (7, 8):  # Stopping, Stopped
        return "cancelled"
    if info.get("ErrorNumber"):
        return "failed"
    if total and layer is not None:
        return "done" if layer >= total else "cancelled"
    return "done"  # the printer didn't say; it ran until it stopped on its own


class StatsReporter:
    """Sends job reports to jET's stats in the background, one try each. A 404 (the console
    doesn't take stats yet) or no answer is dropped quietly; stats must never block a print."""

    def __init__(self, url: str = "", token_file: str = "") -> None:
        self.url = url
        self.token_file = Path(token_file) if token_file else None
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self.warned: set[str] = set()
        if url:
            threading.Thread(target=self._run, name="stats", daemon=True).start()

    def report(self, body: dict[str, Any]) -> None:
        if not self.url:
            return
        try:
            self.queue.put_nowait(body)
        except queue.Full:
            pass

    def _token(self) -> str:
        try:
            return self.token_file.read_text(encoding="utf-8-sig").strip() if self.token_file else ""
        except OSError:
            return ""

    def _run(self) -> None:
        while True:
            self.post(self.queue.get())

    def post(self, body: dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        try:
            urllib.request.urlopen(request, timeout=10).close()
            return True
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            quiet = exc.code == 404
        except Exception as exc:  # noqa: BLE001 - the console may be down; that's fine
            reason, quiet = type(exc).__name__, True
        if not quiet and reason not in self.warned:
            self.warned.add(reason)
            print(f"material service: stats report refused ({reason}); dropping reports like it", file=sys.stderr)
        return False


class Store:
    """Printers, the job ledger and events in one JSON file, written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        # running: the print each printer is on now, so a restarted service keeps its job id
        self.data: dict[str, Any] = {"printers": {}, "jobs": {}, "events": [], "nextEventId": 1, "running": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError) as exc:
                print(f"material service: ignoring unreadable {path}: {exc}", file=sys.stderr)
        self.live: dict[str, dict[str, Any]] = {}  # not saved: what the watcher sees now

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(temp, self.path)

    def printers(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.printer(key) for key in self.data["printers"]]

    def printer(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.data["printers"].get(key)
            if record is None:
                return None
            return {**record, "key": key, "live": dict(self.live.get(key, {}))}

    def upsert(self, key: str, changes: dict[str, Any], by: str) -> dict[str, Any]:
        with self.lock:
            record = self.data["printers"].setdefault(key, {"policy": "warn", "watch": True, "material": ""})
            for field in EDITABLE:
                if field not in changes:
                    continue
                value = changes[field]
                if field == "watch":
                    value = bool(value)
                elif field == "policy":
                    value = str(value or "warn").lower()
                    if value not in POLICIES:
                        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
                else:
                    value = str(value or "").strip()
                if field == "material" and value != record.get("material"):
                    record["materialSetAt"] = now_iso()
                    record["materialSetBy"] = by
                record[field] = value
            record["updatedAt"] = now_iso()
            self.save()
            return self.printer(key) or {}

    def remove(self, key: str) -> bool:
        with self.lock:
            found = self.data["printers"].pop(key, None) is not None
            self.data["jobs"].pop(key, None)
            self.data.setdefault("running", {}).pop(key, None)
            self.live.pop(key, None)
            if found:
                self.save()
            return found

    def record_job(self, key: str, remote_name: str, material: str, by: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            jobs = self.data["jobs"].setdefault(key, {})
            entry = {"remoteName": remote_basename(remote_name), "material": material, "at": now_iso(), "by": by,
                     "jobId": new_job_id(key), "started": False, **(stats or {})}
            jobs[entry["remoteName"].lower()] = entry
            if len(jobs) > MAX_JOBS_PER_PRINTER:
                for old in sorted(jobs, key=lambda name: jobs[name].get("at", ""))[: len(jobs) - MAX_JOBS_PER_PRINTER]:
                    jobs.pop(old, None)
            self.save()
            return entry

    def job(self, key: str, remote_name: str) -> dict[str, Any] | None:
        with self.lock:
            return (self.data["jobs"].get(key) or {}).get(remote_basename(remote_name).lower())

    def take_job(self, key: str, remote_name: str) -> dict[str, Any]:
        """The ledger entry for a print that just started. The first print of a sent file is
        that send's job; a reprint is a new job with the same numbers."""
        with self.lock:
            entry = self.job(key, remote_name)
            if not entry:
                return {"jobId": new_job_id(key)}
            taken = dict(entry)
            if entry.get("jobId") and not entry.get("started"):
                entry["started"] = True
                self.save()
            else:
                taken["jobId"] = new_job_id(key)
            return taken

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            event = {"id": self.data["nextEventId"], "at": now_iso(), **event}
            self.data["nextEventId"] += 1
            self.data["events"].append(event)
            del self.data["events"][:-MAX_EVENTS]
            self.save()
            return event

    def events(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.data["events"] if event["id"] > since][-limit:]


# --- the watcher ----------------------------------------------------------------------------

class PrinterWatch(threading.Thread):
    """One thread per watched SDCP printer: poll status, check each new print once."""

    def __init__(self, service: "MaterialService", key: str, poll_s: float, grace_s: float = LEDGER_GRACE_S) -> None:
        super().__init__(name=f"watch-{key}", daemon=True)
        self.grace_s = grace_s
        self.service = service
        self.key = key
        self.poll_s = poll_s
        self.stop_event = threading.Event()
        record = service.store.printer(key) or {}
        self.ip = record.get("ip", "")
        self.mainboard_id = record.get("mainboardId", "")
        self.client: network_printers.SdcpPrinter | None = None
        self.in_job = False
        self.checked_file = ""
        self.first_look = True
        self.waiting_since: tuple[str, float] | None = None
        self.job: dict[str, Any] | None = None  # the print being counted for jET's stats

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 - an offline printer must not end the watch
                self._close()
                self._live(state="unreachable", error=str(exc))
            self.stop_event.wait(self.poll_s)
        self._close()

    def _close(self) -> None:
        if self.client:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    def _live(self, **values: Any) -> None:
        with self.service.store.lock:
            live = self.service.store.live.setdefault(self.key, {})
            live.update(values, seenAt=now_iso())
            if "error" not in values:
                live.pop("error", None)

    def _connect(self) -> network_printers.SdcpPrinter:
        if not self.client:
            self.client = network_printers.SdcpPrinter(self.ip, self.mainboard_id)
        return self.client

    def poll(self) -> None:
        client = self._connect()
        client.last_status = {}
        status = client.status()
        filename = remote_basename(status.filename)
        self._live(state=status.state, detail=status.detail, filename=filename)
        running = status.state in ("printing", "paused")
        if not running:
            if self.job and status.state == "idle":  # "busy" (a file transfer) isn't the end of a print
                self._finish(status)
            if status.state != "idle" and self.job:
                return
            self.in_job = False
            self.checked_file = ""
            self.first_look = False
            self.waiting_since = None
            return
        if self.job and self.job.get("filename") == filename:
            self.job["layer"], self.job["totalLayers"] = status.current_layer, status.total_layers
        if self.in_job and filename == self.checked_file:
            return
        if not self.first_look and self.service.store.job(self.key, filename) is None:
            if not self.waiting_since or self.waiting_since[0] != filename:
                self.waiting_since = (filename, time.monotonic())
            if time.monotonic() - self.waiting_since[1] < self.grace_s:
                return  # give EZSlicer a moment to record what it just sent
        self.waiting_since = None
        if self.job:  # another print took over without the printer going idle in between
            self._finish(status)
        self.in_job = True
        self.checked_file = filename
        was_running = self.first_look
        self.first_look = False
        self.check(filename, already_running=was_running, paused=status.state == "paused")
        self.job = self.service.start_job(self.key, filename, already_running=was_running)
        self.job["layer"], self.job["totalLayers"] = status.current_layer, status.total_layers

    def _finish(self, status: network_printers.PrinterStatus) -> None:
        job, self.job = self.job, None
        if job:
            self.service.finish_job(self.key, job, job_outcome(status, job.get("layer"), job.get("totalLayers")))

    def check(self, filename: str, *, already_running: bool, paused: bool) -> None:
        store = self.service.store
        record = store.printer(self.key) or {}
        loaded = record.get("material", "")
        job = store.job(self.key, filename)
        job_material = (job or {}).get("material", "")
        verdict = materials_match(loaded, job_material)
        self._live(check={"filename": filename, "jobMaterial": job_material, "loadedMaterial": loaded,
                          "match": verdict, "at": now_iso()})
        if verdict is True or not loaded:
            return
        policy = record.get("policy", "warn")
        name = record.get("name") or self.ip
        if verdict is False:
            kind = "mismatch"
            message = f"{name} is printing {filename}, sliced for {job_material}, but {loaded} is loaded."
            should_pause = policy in ("pause", "strict")
        else:
            kind = "unknown-material"
            message = f"{name} started {filename}; its material isn't known (not sent from EZSlicer). {loaded} is loaded."
            should_pause = policy == "strict"
        if already_running:
            should_pause = False
            message += " (It was already printing when the watcher connected.)"
        action = "none"
        if should_pause and not paused:
            action = "paused" if self._pause() else "pause-failed"
            message += " The print was paused." if action == "paused" else " Pausing it failed - check the printer."
        self.service.emit({
            "kind": kind, "printerKey": self.key, "printerName": name, "filename": filename,
            "jobMaterial": job_material, "loadedMaterial": loaded, "policy": policy, "action": action,
            "message": message,
        })

    def _pause(self) -> bool:
        # Right after a start the printer is homing/checking the file and may refuse a pause.
        for _ in range(PAUSE_ATTEMPTS):
            try:
                self._connect().pause()
                return True
            except Exception:  # noqa: BLE001
                self._close()
                if self.stop_event.wait(2.0):
                    return False
        return False


class MaterialService:
    def __init__(self, data_dir: Path, *, token: str = "", webhook: str = "", poll_s: float = 5.0,
                 stats: StatsReporter | None = None) -> None:
        self.store = Store(data_dir / "printer-materials.json")
        self.token = token
        self.webhook = webhook
        self.poll_s = poll_s
        self.stats = stats or StatsReporter()
        self.watches: dict[str, PrinterWatch] = {}
        self.stop_event = threading.Event()

    # --- jET's stats: one report per change of a job ---

    def report(self, key: str, job: dict[str, Any], status: str, **extra: Any) -> None:
        record = self.store.printer(key) or {}
        body: dict[str, Any] = {
            "app": "ezslicer", "job_id": job["jobId"], "kind": "resin", "status": status,
            "printer": record.get("name") or record.get("ip") or key,
            # what's in the vat is what got used; else what the file was sliced for
            "material": record.get("material") or job.get("material") or "",
            "name": job.get("name") or job.get("remoteName") or job.get("filename") or "",
        }
        for field, name in (("parts", "parts"), ("volumeMl", "volume_cm3"), ("seconds", "seconds")):
            if job.get(field) is not None:
                body[name] = job[field]
        body.update({k: v for k, v in extra.items() if v is not None})
        self.stats.report(body)

    def start_job(self, key: str, filename: str, *, already_running: bool) -> dict[str, Any]:
        store = self.store
        with store.lock:
            running = store.data.setdefault("running", {})
            current = running.get(key)
            if already_running and current and current.get("filename") == filename:
                return dict(current)  # the service restarted mid-print: still the same job
            job = {**store.take_job(key, filename), "filename": filename, "startedAt": now_iso(),
                   "startedTs": time.time(), "fromStart": not already_running}
            job.pop("started", None)
            running[key] = job
            store.save()
        self.report(key, job, "printing")
        return dict(job)

    def finish_job(self, key: str, job: dict[str, Any], outcome: str) -> None:
        with self.store.lock:
            running = self.store.data.setdefault("running", {})
            if (running.get(key) or {}).get("jobId") == job["jobId"]:
                running.pop(key)
                self.store.save()
        # the real time, when the watcher saw the print start
        seconds = round(time.time() - job["startedTs"]) if job.get("fromStart") and job.get("startedTs") else None
        self.report(key, {k: v for k, v in job.items() if k != "seconds"}, outcome, seconds=seconds)

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        saved = self.store.add_event(event)
        print(f"material service: {saved['message']}", file=sys.stderr, flush=True)
        if self.webhook:
            threading.Thread(target=self._post_webhook, args=(saved,), daemon=True).start()
        return saved

    def _post_webhook(self, event: dict[str, Any]) -> None:
        body = json.dumps({"text": event["message"], "event": event}).encode("utf-8")
        request = urllib.request.Request(self.webhook, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:  # noqa: BLE001 - a dead webhook mustn't stop the watcher
            print(f"material service: webhook failed: {exc}", file=sys.stderr)

    def supervise(self) -> None:
        """Keep exactly one watch per watched SDCP printer, restarting on address changes."""
        while not self.stop_event.is_set():
            wanted: dict[str, tuple[str, str]] = {}
            for record in self.store.printers():
                if record.get("watch") and record.get("ip") and record.get("protocol", "sdcp") == "sdcp":
                    wanted[record["key"]] = (record["ip"], record.get("mainboardId", ""))
            for key, watch in list(self.watches.items()):
                if key not in wanted or (watch.ip, watch.mainboard_id) != wanted[key] or not watch.is_alive():
                    watch.stop()
                    del self.watches[key]
            for key in wanted:
                if key not in self.watches:
                    watch = PrinterWatch(self, key, self.poll_s)
                    self.watches[key] = watch
                    watch.start()
            self.stop_event.wait(2.0)
        for watch in self.watches.values():
            watch.stop()

    def stop(self) -> None:
        self.stop_event.set()


# --- HTTP -----------------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    service: MaterialService  # set on the subclass by make_server()
    protocol_version = "HTTP/1.1"
    server_version = "EZSlicerMaterials/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("MATERIAL_SERVICE_LOG"):
            sys.stderr.write("material service: " + (format % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query)
        store = self.service.store
        try:
            if method != "GET":
                self._authorize()
            body = self._body() if method in ("PUT", "POST") else {}
            by = self.headers.get("X-Jet-User") or str(body.get("by") or "") or "unknown"
            match = re.fullmatch(rf"{API}/printers/([^/]+)", path)
            if method == "GET" and path == "/api/health":
                self._json(HTTPStatus.OK, {"ok": True, "printers": len(store.data["printers"]),
                                           "watching": sorted(self.service.watches)})
            elif method == "GET" and path == f"{API}/printers":
                self._json(HTTPStatus.OK, {"printers": store.printers()})
            elif match and method == "GET":
                record = store.printer(unquote(match.group(1)))
                if record is None:
                    raise HttpError(HTTPStatus.NOT_FOUND, "No such printer.")
                self._json(HTTPStatus.OK, record)
            elif match and method == "PUT":
                try:
                    record = store.upsert(unquote(match.group(1)), body, by)
                except ValueError as exc:
                    raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
                self._json(HTTPStatus.OK, record)
            elif match and method == "DELETE":
                if not store.remove(unquote(match.group(1))):
                    raise HttpError(HTTPStatus.NOT_FOUND, "No such printer.")
                self._json(HTTPStatus.OK, {"ok": True})
            elif method == "POST" and path == f"{API}/jobs":
                key, remote = str(body.get("printerKey") or ""), str(body.get("remoteName") or "")
                if not key or not remote:
                    raise HttpError(HTTPStatus.BAD_REQUEST, "\"printerKey\" and \"remoteName\" are required.")
                entry = store.record_job(key, remote, str(body.get("material") or ""), by, job_stats(body))
                self.service.report(key, entry, "sent")
                self._json(HTTPStatus.OK, entry)
            elif method == "GET" and path == f"{API}/events":
                since = int((query.get("since") or ["0"])[0] or 0)
                limit = max(1, min(1000, int((query.get("limit") or ["100"])[0] or 100)))
                events = store.events(since, limit)
                self._json(HTTPStatus.OK, {"events": events, "last": store.data["nextEventId"] - 1})
            else:
                raise HttpError(HTTPStatus.NOT_FOUND, f"There's no {method} {path} here.")
        except HttpError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _authorize(self) -> None:
        if not self.service.token:
            return
        scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), self.service.token.encode()):
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Changes need the service token (Authorization: Bearer ...).")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, f"Body isn't JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object.")
        return body

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def make_server(service: MaterialService, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def default_data_dir() -> Path:
    base = os.environ.get("PROGRAMDATA") if os.name == "nt" else None
    return Path(base) / "EZSlicer" if base else Path.home() / ".ezslicer"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EZSlicer printer material service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8070)
    parser.add_argument("--data", type=Path, default=default_data_dir())
    parser.add_argument("--token", default=os.environ.get("MATERIAL_SERVICE_TOKEN", ""))
    parser.add_argument("--webhook", default=os.environ.get("MATERIAL_SERVICE_WEBHOOK", ""))
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between printer status checks")
    parser.add_argument("--no-watch", action="store_true", help="serve the registry only")
    parser.add_argument("--stats-url", default=os.environ.get("MATERIAL_SERVICE_STATS_URL", ""),
                        help="report each resin job here (jET console: .../api/v1/stats/jobs)")
    parser.add_argument("--stats-token-file", default=os.environ.get("MATERIAL_SERVICE_STATS_TOKEN_FILE", ""),
                        help="file holding the bearer token for --stats-url (read on every report)")
    args = parser.parse_args(argv)
    service = MaterialService(args.data, token=args.token, webhook=args.webhook, poll_s=args.poll,
                              stats=StatsReporter(args.stats_url, args.stats_token_file))
    if not args.no_watch:
        threading.Thread(target=service.supervise, name="supervisor", daemon=True).start()
    server = make_server(service, args.host, args.port)
    print(json.dumps({"service": "ezslicer-materials", "host": args.host, "port": server.server_address[1],
                      "data": str(args.data)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
