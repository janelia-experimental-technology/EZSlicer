"""EZSlicer's headless engine: the same work as the window, over HTTP on 127.0.0.1.

    JET_ENGINE_TOKEN=<secret> python -m resin_slicer.engine --engine [--port 0] [--parent-pid N]

It listens on 127.0.0.1 on a free port it picks itself (unless --port says otherwise), then
prints one JSON line on stdout and nothing else:

    {"engine": "ezslicer", "version": "0.2.0", "api": "1.0", "port": 51234}

``EZSlicer.exe --engine`` runs this same engine. Electron prints a blank line first on Windows,
so read the first non-blank line.

Every request except the four discovery endpoints (/api/health, /api/version,
/api/capabilities, /api/openapi.json) needs ``Authorization: Bearer <JET_ENGINE_TOKEN>``, so
another program on the PC can't drive the printers. The engine exits when the process that
started it exits (or the one named by --parent-pid / JET_ENGINE_PARENT_PID).

Long jobs (preview, slice, printer send) answer with the same JSON messages the JSON-lines
bridge prints: ``progress`` / ``support`` while working, then the result. Ask for
``Accept: application/x-ndjson`` (or ``?stream=1``) to get every message as it happens, one
per line; otherwise the response is the result alone. Preview and slice run in their own
Python process (as the bridge), so a client that hangs up mid-stream stops the job. Blank lines
in a stream are keep-alives.

Errors always look like ``{"error": {"code": "...", "message": "...", "details": {...}}}`` with
a fitting HTTP status; inside a stream, the last line is ``{"type": "error", "message": ...,
"error": {...}}``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlsplit

from . import network_printers
from .config import PROFILES
from .engine_info import API_VERSION, CAPABILITIES, ENGINE_ID, PACKAGE_DIR, version_json
from .engine_openapi import openapi_document
from .errors import SlicerError

API_PREFIX = "/api/v1"
NDJSON = "application/x-ndjson"
MAX_BODY_BYTES = 64 * 1024 * 1024  # requests carry paths and settings, never model data
KEEPALIVE_S = 2.0
PUBLIC_PATHS = frozenset({"/api/health", "/api/version", "/api/capabilities", "/api/openapi.json"})
STARTED_AT = time.time()


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_json(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


# --- the machine connections: one owner per printer --------------------------------------

_printer_locks: dict[str, threading.Lock] = {}
_printer_locks_guard = threading.Lock()


def _printer_lock(printer: dict[str, Any]) -> threading.Lock:
    key = str(printer.get("ip") or "").strip()
    with _printer_locks_guard:
        return _printer_locks.setdefault(key, threading.Lock())


def _require_printer(body: dict[str, Any]) -> dict[str, Any]:
    printer = body.get("printer")
    if not isinstance(printer, dict) or not str(printer.get("ip") or "").strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"printer\" with an \"ip\" is required.")
    return printer


# --- child processes for preview / slice -------------------------------------------------

_children: set[subprocess.Popen] = set()
_children_guard = threading.Lock()


def _python_for(body: dict[str, Any]) -> str:
    """STEP models need a Python with the CAD kernel (RESIN_SLICER_STEP_PYTHON); else ours."""
    step_python = os.environ.get("RESIN_SLICER_STEP_PYTHON", "")
    paths = [body.get("inputPath")] + [m.get("inputPath") or m.get("path") for m in body.get("models") or [] if isinstance(m, dict)]
    has_step = any(str(path or "").lower().endswith((".stp", ".step")) for path in paths)
    if has_step and step_python and Path(step_python).exists():
        return step_python
    return sys.executable


# The children put the package folder on sys.path themselves instead of relying on the current
# folder: an embeddable Python (with a ._pth file) imports only from the folders listed there.
_CHILD_BOOT = (
    "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
    "runpy.run_module('resin_slicer.electron_bridge', run_name='__main__', alter_sys=True)"
)


def _bridge_messages(command: str, body: dict[str, Any], stop: threading.Event) -> Iterator[dict[str, Any] | None]:
    """Run one bridge command in its own Python and yield its messages (None = keep-alive)."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("JET_ENGINE_TOKEN", None)  # the job never needs the engine's secret
    child = subprocess.Popen(
        [_python_for(body), "-c", _CHILD_BOOT, str(PACKAGE_DIR.parent), command],
        cwd=str(PACKAGE_DIR.parent),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with _children_guard:
        _children.add(child)
    lines: queue.Queue[bytes | None] = queue.Queue()
    stderr: list[bytes] = []

    def pump_stdout() -> None:
        for raw in child.stdout:  # type: ignore[union-attr]
            lines.put(raw)
        lines.put(None)

    def pump_stderr() -> None:
        stderr.append(child.stderr.read())  # type: ignore[union-attr]

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()
    try:
        child.stdin.write(json.dumps(body).encode("utf-8"))  # type: ignore[union-attr]
        child.stdin.close()  # type: ignore[union-attr]
        final = None
        while not stop.is_set():
            try:
                raw = lines.get(timeout=KEEPALIVE_S)
            except queue.Empty:
                yield None
                continue
            if raw is None:
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                stderr.append(raw)
                continue
            if message.get("type") in ("progress", "support"):
                yield message
            else:
                final = message
        if stop.is_set():
            return
        code = child.wait()
        if final is None:
            detail = b"".join(stderr).decode("utf-8", "replace").strip()[-2000:]
            final = {"type": "error", "message": detail or f"the slicer exited with code {code} and no answer"}
        yield final
    finally:
        if child.poll() is None:
            child.kill()
        with _children_guard:
            _children.discard(child)


def _threaded_messages(work: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]], stop: threading.Event) -> Iterator[dict[str, Any] | None]:
    """Run work(emit) in a thread; yield what it emits, then its result (or an error message)."""
    messages: queue.Queue[tuple[str, Any]] = queue.Queue()

    def run() -> None:
        try:
            messages.put(("final", work(lambda message: messages.put(("message", message)))))
        except Exception as exc:  # noqa: BLE001 - turned into an error message
            messages.put(("error", exc))

    threading.Thread(target=run, daemon=True).start()
    while not stop.is_set():
        try:
            kind, value = messages.get(timeout=KEEPALIVE_S)
        except queue.Empty:
            yield None
            continue
        if kind == "message":
            yield value
        elif kind == "final":
            yield value
            return
        else:
            yield _error_message(value)
            return


def _error_message(exc: BaseException) -> dict[str, Any]:
    error = _api_error(exc)
    # "_status" is for the non-streamed answer only; it's never sent.
    return {"type": "error", "message": error.message, **error.to_json(), "_status": int(error.status)}


def _api_error(exc: BaseException) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, network_printers.PrinterError):
        return ApiError(HTTPStatus.BAD_GATEWAY, "printer_error", str(exc))
    if isinstance(exc, (SlicerError, ValueError, KeyError, TypeError)):
        return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_request", str(exc) or exc.__class__.__name__)
    if isinstance(exc, ConnectionError):
        return ApiError(HTTPStatus.BAD_GATEWAY, "unreachable", "Nothing answered at that address.", {"reason": str(exc)})
    if isinstance(exc, OSError):
        return ApiError(HTTPStatus.BAD_GATEWAY, "unreachable", str(exc))
    return ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", str(exc) or exc.__class__.__name__)


# --- printer work (in this process, one connection per printer at a time) ----------------

def _discover(body: dict[str, Any]) -> dict[str, Any]:
    targets = [str(ip).strip() for ip in body.get("targets") or [] if str(ip).strip()]
    timeout = min(10.0, max(0.1, float(body.get("timeout") or 2.5)))
    printers = network_printers.discover(targets, timeout=timeout, broadcast=bool(body.get("broadcast", True)))
    return {"type": "printers", "printers": [printer.to_json() for printer in printers]}


def _status(body: dict[str, Any]) -> dict[str, Any]:
    printer = _require_printer(body)
    with _printer_lock(printer), network_printers.connect(printer) as client:
        return {"type": "printer-status", "status": client.status().to_json()}


def _status_all(body: dict[str, Any]) -> dict[str, Any]:
    printers = body.get("printers")
    if not isinstance(printers, list):
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"printers\" must be a list.")
    from concurrent.futures import ThreadPoolExecutor

    def one(printer: Any) -> dict[str, Any]:
        if not isinstance(printer, dict):
            return {"id": None, "ok": False, "error": "not a printer"}
        try:
            with _printer_lock(printer), network_printers.connect(printer) as client:
                return {"id": printer.get("id"), "ok": True, **client.status().to_json()}
        except Exception as exc:  # noqa: BLE001 - reported per printer
            return {"id": printer.get("id"), "ok": False, "error": str(exc)}

    if not printers:
        return {"type": "printer-statuses", "statuses": []}
    with ThreadPoolExecutor(max_workers=min(16, len(printers))) as pool:
        return {"type": "printer-statuses", "statuses": list(pool.map(one, printers))}


def _control(body: dict[str, Any]) -> dict[str, Any]:
    printer = _require_printer(body)
    action = str(body.get("action") or "")
    if action not in ("pause", "resume", "stop"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"action\" must be pause, resume or stop.", {"action": action})
    with _printer_lock(printer), network_printers.connect(printer) as client:
        getattr(client, action)()
        return {"type": "printer-status", "status": client.status().to_json()}


def _check_send(body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    printer = _require_printer(body)
    path = str(body.get("path") or "")
    if not path or not Path(path).is_file():
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"path\" must be a sliced file on this PC.", {"path": path})
    return printer, path


def _send(body: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    printer, path = _check_send(body)
    with _printer_lock(printer):
        result = network_printers.send_file(
            printer,
            path,
            start=bool(body.get("start")),
            progress=lambda message, fraction: emit({"type": "progress", "message": message, "fraction": fraction}),
        )
    return {"type": "printer-sent", **result}


# --- HTTP ---------------------------------------------------------------------------------

class EngineHandler(BaseHTTPRequestHandler):
    server_version = f"EZSlicerEngine/{API_VERSION}"
    protocol_version = "HTTP/1.1"
    token = ""  # set on the server class by serve()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdout is the startup line only
        if os.environ.get("EZSLICER_ENGINE_LOG"):
            sys.stderr.write("engine: " + (format % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # routing

    def _routes(self) -> dict[tuple[str, str], Callable[[], None]]:
        return {
            ("GET", "/api/health"): lambda: self._json(HTTPStatus.OK, {"ok": True, "uptimeS": round(time.time() - STARTED_AT, 1), "jobs": len(_children)}),
            ("GET", "/api/version"): lambda: self._json(HTTPStatus.OK, version_json()),
            ("GET", "/api/capabilities"): lambda: self._json(HTTPStatus.OK, {"api": API_VERSION, "features": list(CAPABILITIES)}),
            ("GET", "/api/openapi.json"): lambda: self._json(HTTPStatus.OK, openapi_document()),
            ("GET", f"{API_PREFIX}/info"): lambda: self._json(HTTPStatus.OK, {"type": "info", "engine": "EZSlicer", **version_json()}),
            ("GET", f"{API_PREFIX}/profiles"): lambda: self._json(HTTPStatus.OK, {"type": "profiles", "profiles": {name: asdict(cfg) for name, cfg in PROFILES.items()}}),
            ("POST", f"{API_PREFIX}/preview"): lambda: self._bridge_job("preview"),
            ("POST", f"{API_PREFIX}/slice"): lambda: self._slice(),
            ("POST", f"{API_PREFIX}/printers/discover"): lambda: self._simple(_discover),
            ("POST", f"{API_PREFIX}/printers/status"): lambda: self._simple(_status),
            ("POST", f"{API_PREFIX}/printers/status-all"): lambda: self._simple(_status_all),
            ("POST", f"{API_PREFIX}/printers/control"): lambda: self._simple(_control),
            ("POST", f"{API_PREFIX}/printers/send"): lambda: self._send_job(),
        }

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        self._query = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            routes = self._routes()
            handler = routes.get((method, path))
            if handler is None:
                if any(route_path == path for _, route_path in routes):
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", f"{method} isn't allowed on {path}.")
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", f"There's no {path} here.", {"path": path})
            if path not in PUBLIC_PATHS:
                self._authorize()
            self._body = self._read_body() if method in ("POST", "PUT") else {}
            handler()
        except ApiError as exc:
            self._json(exc.status, exc.to_json())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - always answer in the error shape
            error = _api_error(exc)
            self._json(error.status, error.to_json())

    def _authorize(self) -> None:
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), self.token.encode()):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", "This engine needs the token it was started with (Authorization: Bearer ...).")

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "The request is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", f"The request body isn't JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "The request body must be a JSON object.")
        return body

    # answers

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _simple(self, work: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        try:
            result = work(self._body)
        except Exception as exc:  # noqa: BLE001 - a printer's socket error isn't our client hanging up
            raise _api_error(exc) from exc
        self._json(HTTPStatus.OK, result)

    def _wants_stream(self) -> bool:
        flag = (self._query.get("stream") or [""])[0].lower()
        return flag in ("1", "true", "yes") or NDJSON in self.headers.get("Accept", "")

    def _slice(self) -> None:
        if not str(self._body.get("outputPath") or "").strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"outputPath\" is required.")
        fmt = str(self._body.get("format") or "goo").lower()
        if fmt not in ("ctb", "goo"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"format\" must be ctb or goo.", {"format": fmt})
        self._bridge_job("slice")

    def _send_job(self) -> None:
        _check_send(self._body)  # refuse bad input with a 400 before anything starts
        self._stream_job(lambda stop: _threaded_messages(lambda emit: _send(self._body, emit), stop))

    def _bridge_job(self, command: str) -> None:
        if not (self._body.get("inputPath") or self._body.get("models")):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "\"inputPath\" or \"models\" is required.")
        self._stream_job(lambda stop: _bridge_messages(command, self._body, stop))

    def _stream_job(self, start: Callable[[threading.Event], Iterator[dict[str, Any] | None]]) -> None:
        stop = threading.Event()
        messages = start(stop)
        try:
            if not self._wants_stream():
                final = None
                for message in messages:
                    if message is not None and message.get("type") not in ("progress", "support"):
                        final = message
                if final is None or final.get("type") == "error":
                    final = final or {}
                    error = final.get("error") or {"code": "failed", "message": final.get("message") or "The job failed."}
                    self._json(HTTPStatus(final.get("_status") or HTTPStatus.UNPROCESSABLE_ENTITY), {"error": error})
                else:
                    self._json(HTTPStatus.OK, final)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{NDJSON}; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for message in messages:
                if message is not None and message.get("type") == "error":
                    message = {key: value for key, value in message.items() if key != "_status"}
                    message.setdefault("error", {"code": "failed", "message": message.get("message", "")})
                line = b"\n" if message is None else json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
                self._chunk(line)
            self._chunk(b"")
        finally:
            stop.set()  # a client that hung up stops the job
            close = getattr(messages, "close", None)
            if close:
                close()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()


class EngineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _shutdown(code: int = 0) -> None:
    with _children_guard:
        for child in list(_children):
            if child.poll() is None:
                child.kill()
    os._exit(code)


def _watch_parent(pid: int) -> None:
    while _parent_alive(pid):
        time.sleep(1.0)
    _shutdown(0)


def _announce(line: str) -> None:
    # One line ending in a bare LF, even on Windows (text-mode stdout would write CRLF).
    sys.stdout.buffer.write(line.encode("utf-8") + bytes([10]))
    sys.stdout.buffer.flush()


def serve(token: str, host: str = "127.0.0.1", port: int = 0, parent_pid: int = 0, announce: Callable[[str], None] | None = None) -> None:
    handler = type("BoundEngineHandler", (EngineHandler,), {"token": token})
    server = EngineServer((host, port), handler)
    line = json.dumps({"engine": ENGINE_ID, "version": version_json()["version"], "api": API_VERSION, "port": server.server_address[1]})
    (announce or _announce)(line)
    if parent_pid:
        threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _shutdown(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m resin_slicer.engine", description="EZSlicer's headless engine (HTTP on 127.0.0.1)")
    parser.add_argument("--engine", action="store_true", help="run the engine (the default; accepted so callers can say so)")
    parser.add_argument("--host", default=os.environ.get("EZSLICER_ENGINE_HOST", "127.0.0.1"), help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("EZSLICER_ENGINE_PORT") or 0), help="port (default: a free one)")
    parser.add_argument("--parent-pid", type=int, default=int(os.environ.get("JET_ENGINE_PARENT_PID") or 0), help="exit when this process exits (default: the process that started the engine)")
    parser.add_argument("--version", action="store_true", help="print the version JSON and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps(version_json()))
        return 0
    token = os.environ.get("JET_ENGINE_TOKEN", "").strip()
    if not token:
        sys.stderr.write("engine: set JET_ENGINE_TOKEN to the secret callers will send (Authorization: Bearer ...)\n")
        return 2
    serve(token, host=args.host, port=args.port, parent_pid=args.parent_pid or os.getppid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
