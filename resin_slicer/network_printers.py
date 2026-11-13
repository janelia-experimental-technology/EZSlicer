"""Network printer support for Wi-Fi/LAN resin printers.

Two protocols cover the Elegoo/ChiTu printers this slicer targets:

* ``chitu`` - the older ChiTu "CBD" LAN protocol (e.g. Elegoo Jupiter, Saturn,
  Mars 2/3). G-code style text commands over UDP port 3000. Files are written
  with M28 <name>, then binary chunks framed as
  ``payload | offset (uint32 LE) | XOR of payload+offset (1 byte) | 0x83``,
  each acknowledged with ``ok``, then M29 (``Done saving file!``).
* ``sdcp`` - Elegoo's Smart Device Control Protocol v3 (e.g. Mars 5 Ultra,
  Saturn 4 Ultra). JSON over a WebSocket on port 3030, files uploaded with
  chunked multipart POSTs to ``/uploadFile/upload``.

Both answer the same UDP discovery probe (``M99999`` to port 3000): ChiTu
boards reply with a text line, SDCP boards with JSON.

Only the standard library is used so the packaged app needs no new wheels.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import SlicerError

DISCOVERY_PORT = 3000
CHITU_PORT = 3000
SDCP_PORT = 3030
CHITU_CHUNK = 1280  # protocol doc recommends <= 256 * 5 bytes per packet
SDCP_CHUNK = 1024 * 1024
# ChiTu firmware truncates long network-written names; ChituBox keeps 25 chars of stem.
CHITU_MAX_STEM = 25

Progress = Callable[[str, float], None]


class PrinterError(SlicerError):
    """A network printer refused a request or could not be reached."""


@dataclass
class PrinterInfo:
    ip: str
    protocol: str  # "chitu" | "sdcp"
    name: str = ""
    model: str = ""
    firmware: str = ""
    mainboard_id: str = ""
    mac: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterStatus:
    state: str  # idle | printing | paused | busy | unknown
    detail: str = ""
    progress: float | None = None  # 0..1 when printing
    current_layer: int | None = None
    total_layers: int | None = None
    filename: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def parse_discovery_reply(data: bytes, source_ip: str) -> PrinterInfo | None:
    text = data.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        info = payload.get("Data") or {}
        if not isinstance(info, dict) or not info.get("MainboardID"):
            return None
        return PrinterInfo(
            ip=str(info.get("MainboardIP") or source_ip),
            protocol="sdcp",
            name=str(info.get("Name") or info.get("MachineName") or ""),
            model=str(info.get("MachineName") or ""),
            firmware=str(info.get("FirmwareVersion") or ""),
            mainboard_id=str(info.get("MainboardID")),
        )
    if text.startswith("ok") and "MAC:" in text:
        fields = dict(re.findall(r"(MAC|IP|VER|ID|NAME):(\S+)", text))
        return PrinterInfo(
            ip=fields.get("IP", source_ip),
            protocol="chitu",
            name=fields.get("NAME", ""),
            firmware=fields.get("VER", ""),
            mainboard_id=fields.get("ID", ""),
            mac=fields.get("MAC", ""),
        )
    return None


def discover(targets: list[str] | None = None, timeout: float = 2.0, broadcast: bool = True) -> list[PrinterInfo]:
    """Probe for printers. ``targets`` are unicast IPs to ask directly
    (broadcasts are often filtered between subnets)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    found: dict[str, PrinterInfo] = {}
    try:
        destinations = list(targets or [])
        if broadcast:
            destinations.append("255.255.255.255")
        for ip in destinations:
            try:
                sock.sendto(b"M99999", (ip, DISCOVERY_PORT))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (source_ip, _port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            info = parse_discovery_reply(data, source_ip)
            if info:
                found[info.ip] = info
    finally:
        sock.close()
    return sorted(found.values(), key=lambda item: tuple(int(part) for part in item.ip.split(".") if part.isdigit()))


def probe(ip: str, timeout: float = 2.0) -> PrinterInfo:
    printers = discover([ip], timeout=timeout, broadcast=False)
    for printer in printers:
        if printer.ip == ip:
            return printer
    if printers:
        return printers[0]
    raise PrinterError(f"No printer answered at {ip}. Check the IP and that the printer's network is on.")


def remote_filename(local_name: str, protocol: str) -> str:
    """Printer-safe file name: ASCII, no path characters, ChiTu stem <= 25."""
    base = os.path.basename(local_name.replace("\\", "/"))
    # splitext(".ctb") treats the whole name as a stem; keep it as the extension.
    stem, ext = ("", base) if base.startswith(".") and base.count(".") == 1 else os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9 _.\-()]+", "_", stem).strip(" ._") or "print"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext).lower()
    if protocol == "chitu":
        stem = stem[:CHITU_MAX_STEM].rstrip(" ._") or "print"
    else:
        stem = stem[:80]
    return stem + ext


def unique_name(name: str, existing: list[str], protocol: str) -> str:
    """Never overwrite a file already on the printer: add -2, -3, ... instead."""
    taken = {item.lower() for item in existing}
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    for index in range(2, 1000):
        suffix = f"-{index}"
        base = stem[:CHITU_MAX_STEM - len(suffix)] if protocol == "chitu" else stem
        candidate = f"{base}{suffix}{ext}"
        if candidate.lower() not in taken:
            return candidate
    raise PrinterError(f"Too many files named like {name} on the printer")


# --------------------------------------------------------------------------
# ChiTu (UDP)
# --------------------------------------------------------------------------

def chitu_frame(chunk: bytes, offset: int) -> bytes:
    body = chunk + struct.pack("<I", offset)
    check = 0
    for byte in body:
        check ^= byte
    return body + bytes((check, 0x83))


class ChituPrinter:
    def __init__(self, ip: str, timeout: float = 2.0) -> None:
        self.ip = ip
        self.timeout = timeout
        # The board ties its session to our source port, so keep one socket.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "ChituPrinter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _recv_lines(self, until: Callable[[list[str]], bool], timeout: float) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            for line in data.decode("utf-8", "replace").replace("\r", "").split("\n"):
                if line.strip():
                    lines.append(line.strip())
            if until(lines):
                break
        return lines

    def command(self, text: str, timeout: float | None = None) -> list[str]:
        """Send a command; return reply lines up to and including the final ok/Error."""
        self._flush()
        self.sock.sendto(text.encode("utf-8"), (self.ip, CHITU_PORT))
        lines = self._recv_lines(lambda got: any(_is_final(line) for line in got), timeout or self.timeout)
        if not lines:
            raise PrinterError(f"{self.ip} did not answer {text.split()[0]}")
        return lines

    def _flush(self) -> None:
        self.sock.settimeout(0)
        try:
            while True:
                self.sock.recvfrom(65535)
        except (BlockingIOError, socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

    def status(self) -> PrinterStatus:
        lines = self.command("M4000")
        line = next((item for item in lines if item.startswith("ok") and "D:" in item), "")
        raw: dict[str, Any] = {"m4000": line}
        match = re.search(r"D:(\d+)/(\d+)/(\d+)", line)
        if not match:
            return PrinterStatus("unknown", detail=line or " ".join(lines), raw=raw)
        position, total, paused = (int(value) for value in match.groups())
        printing = self.command("M27")
        raw["m27"] = printing
        if any("not printing" in item.lower() for item in printing) or total == 0:
            return PrinterStatus("idle", detail="Idle", raw=raw)
        progress = position / total if total else None
        state = "paused" if paused == 1 else "printing"
        detail = f"{'Paused' if paused == 1 else 'Printing'} {progress * 100:.0f}%" if progress is not None else state
        return PrinterStatus(state, detail=detail, progress=progress, raw=raw)

    def list_files(self) -> list[str]:
        lines = self.command("M20", timeout=6)
        names = []
        for line in lines:
            if line in ("Begin file list", "End file list") or _is_final(line):
                continue
            # "<name> <size>" - the size field is not reliable on these boards.
            names.append(line.rsplit(" ", 1)[0] if re.search(r" \d+$", line) else line)
        return [name for name in names if name != "System Volume Information"]

    def upload(self, path: str | Path, remote_name: str, progress: Progress | None = None) -> None:
        data = Path(path).read_bytes()
        reply = self.command(f"M28 {remote_name}", timeout=4)
        if not any(line.startswith("ok") for line in reply):
            raise PrinterError(f"Printer refused the upload: {' '.join(reply)}")
        try:
            offset = 0
            retries = 0
            last_report = 0.0
            while offset < len(data):
                chunk = data[offset:offset + CHITU_CHUNK]
                self.sock.sendto(chitu_frame(chunk, offset), (self.ip, CHITU_PORT))
                lines = self._recv_lines(lambda got: any(line.startswith(("ok", "resend", "Error")) for line in got), 3.0)
                resend = next((line for line in lines if line.startswith("resend")), None)
                error = next((line for line in lines if line.startswith("Error")), None)
                if error:
                    raise PrinterError(f"Printer write failed at byte {offset}: {error}")
                if resend:
                    numbers = re.findall(r"\d+", resend)
                    offset = int(numbers[0]) if numbers else offset
                    retries += 1
                elif any(line.startswith("ok") for line in lines):
                    offset += len(chunk)
                    retries = 0
                else:
                    retries += 1
                if retries > 8:
                    raise PrinterError(f"Upload stalled at byte {offset} after repeated retries")
                if progress and (time.monotonic() - last_report > 0.25 or offset >= len(data)):
                    last_report = time.monotonic()
                    progress(f"Uploading {remote_name}", offset / len(data) if data else 1.0)
        except BaseException:
            # Close the half-written file so the board isn't left mid-write.
            try:
                self.command("M29", timeout=4)
            except PrinterError:
                pass
            raise
        done = self.command("M29", timeout=8)
        if not any("Done saving" in line for line in done):
            raise PrinterError(f"Printer did not confirm the saved file: {' '.join(done)}")

    def start_print(self, remote_name: str) -> None:
        _expect_ok(self.command(f"M6030 ':{remote_name}'", timeout=6), "start the print")

    def pause(self) -> None:
        _expect_ok(self.command("M25"), "pause")

    def resume(self) -> None:
        _expect_ok(self.command("M24"), "resume")

    def stop(self) -> None:
        _expect_ok(self.command("M33", timeout=6), "stop")

    def delete(self, remote_name: str) -> None:
        _expect_ok(self.command(f"M30 {remote_name}", timeout=4), "delete the file")


def _is_final(line: str) -> bool:
    return line.startswith("ok") or line.startswith("Error")


def _expect_ok(lines: list[str], action: str) -> None:
    if any(line.startswith("Error") for line in lines) or not any(line.startswith("ok") for line in lines):
        raise PrinterError(f"Printer could not {action}: {' '.join(lines)}")


# --------------------------------------------------------------------------
# SDCP v3 (WebSocket + HTTP)
# --------------------------------------------------------------------------

SDCP_MACHINE_STATUS = {0: "idle", 1: "printing", 2: "busy", 3: "busy", 4: "busy"}
SDCP_PRINT_STATUS = {
    0: "Idle", 1: "Homing", 2: "Dropping", 3: "Exposing", 4: "Lifting", 5: "Pausing",
    6: "Paused", 7: "Stopping", 8: "Stopped", 9: "Complete", 10: "Checking file",
}
SDCP_CMD_STATUS = 0
SDCP_CMD_ATTRIBUTES = 1
SDCP_CMD_START = 128
SDCP_CMD_PAUSE = 129
SDCP_CMD_STOP = 130
SDCP_CMD_RESUME = 131
SDCP_CMD_FILE_LIST = 258
SDCP_CMD_DELETE = 259


class WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise PrinterError("WebSocket handshake closed early")
            response += chunk
        head, self._buffer = response.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise PrinterError(f"WebSocket upgrade refused: {head.splitlines()[0].decode('latin-1')}")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise PrinterError("Printer closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def recv_text(self, timeout: float) -> str | None:
        self.sock.settimeout(timeout)
        message = b""
        try:
            while True:
                first, second = self._read_exact(2)
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read_exact(8))[0]
                mask = self._read_exact(4) if second & 0x80 else b""
                payload = self._read_exact(length)
                if mask:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0x8:
                    raise PrinterError("Printer closed the connection")
                if opcode in (0x1, 0x0, 0x2):
                    message += payload
                    if first & 0x80:
                        return message.decode("utf-8", "replace")
        except socket.timeout:
            return None


class SdcpPrinter:
    def __init__(self, ip: str, mainboard_id: str = "", timeout: float = 5.0) -> None:
        self.ip = ip
        self.timeout = timeout
        self.mainboard_id = mainboard_id or probe(ip).mainboard_id
        self.ws: WebSocket | None = None
        self.last_status: dict[str, Any] = {}

    def __enter__(self) -> "SdcpPrinter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    def _socket(self) -> WebSocket:
        if not self.ws:
            self.ws = WebSocket(self.ip, SDCP_PORT, "/websocket", timeout=self.timeout)
        return self.ws

    def request(self, cmd: int, data: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        ws = self._socket()
        request_id = uuid.uuid4().hex
        ws.send_text(json.dumps({
            "Id": uuid.uuid4().hex,
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": request_id,
                "MainboardID": self.mainboard_id,
                "TimeStamp": int(time.time()),
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }))
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            text = ws.recv_text(max(0.05, deadline - time.monotonic()))
            if text is None:
                break
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            topic = str(message.get("Topic", ""))
            if topic.startswith("sdcp/status/") and isinstance(message.get("Status"), dict):
                self.last_status = message["Status"]
            body = message.get("Data") or {}
            if topic.startswith("sdcp/response/") and body.get("RequestID") == request_id:
                result = body.get("Data") or {}
                ack = result.get("Ack", 0)
                if ack not in (0, None):
                    raise PrinterError(f"Printer rejected command {cmd} (Ack {ack})")
                return result
        raise PrinterError(f"{self.ip} did not answer command {cmd}")

    def status(self) -> PrinterStatus:
        self.request(SDCP_CMD_STATUS)
        # Status arrives as a pushed sdcp/status message; give it a moment.
        deadline = time.monotonic() + 2.0
        while not self.last_status and time.monotonic() < deadline:
            text = self._socket().recv_text(0.25)
            if text:
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(message.get("Status"), dict):
                    self.last_status = message["Status"]
        return sdcp_status_from_json(self.last_status)

    def list_files(self, folder: str = "/local/") -> list[str]:
        result = self.request(SDCP_CMD_FILE_LIST, {"Url": folder})
        files = result.get("FileList") or []
        return [str(item.get("name", "")).rsplit("/", 1)[-1] for item in files if item.get("type") != 0]

    def upload(self, path: str | Path, remote_name: str, progress: Progress | None = None) -> None:
        data = Path(path).read_bytes()
        digest = hashlib.md5(data).hexdigest()
        upload_id = uuid.uuid4().hex
        url = f"http://{self.ip}:{SDCP_PORT}/uploadFile/upload"
        offset = 0
        while offset < len(data) or (offset == 0 and not data):
            chunk = data[offset:offset + SDCP_CHUNK]
            fields = {
                "S-File-MD5": digest,
                "Check": "1",
                "Offset": str(offset),
                "Uuid": upload_id,
                "TotalSize": str(len(data)),
            }
            body, content_type = _multipart(fields, "File", remote_name, chunk)
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    reply = json.loads(response.read().decode("utf-8", "replace") or "{}")
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                raise PrinterError(f"Upload to {self.ip} failed at byte {offset}: {exc}") from exc
            if not reply.get("success", False):
                raise PrinterError(f"Printer rejected upload chunk at byte {offset}: {reply}")
            offset += len(chunk)
            if progress:
                progress(f"Uploading {remote_name}", offset / len(data) if data else 1.0)
            if not data:
                break
        self._wait_for_saved_file(upload_id, remote_name, progress)

    def _wait_for_saved_file(self, upload_id: str, remote_name: str, progress: Progress | None, timeout: float = 45.0) -> None:
        """The printer stores an upload as '<Uuid>_<name>', checks it, then renames it.
        Files that fail the check (wrong format/resolution) are silently dropped."""
        temp_name = f"{upload_id}_{remote_name}"
        deadline = time.monotonic() + timeout
        seen_temp = False
        while time.monotonic() < deadline:
            files = self.list_files()
            if remote_name in files and temp_name not in files:
                return
            if temp_name in files:
                seen_temp = True
                if progress:
                    progress(f"Printer is checking {remote_name}", 1.0)
            elif seen_temp:
                # Temp file vanished without the final name appearing: rejected.
                break
            time.sleep(1.0)
        raise PrinterError(
            f"The printer did not keep {remote_name}. It may have rejected the file "
            "(check the machine profile's resolution and format match this printer)."
        )

    def start_print(self, remote_name: str) -> None:
        self.request(SDCP_CMD_START, {"Filename": f"/local/{remote_name}", "StartLayer": 0}, timeout=10)

    def pause(self) -> None:
        self.request(SDCP_CMD_PAUSE)

    def resume(self) -> None:
        self.request(SDCP_CMD_RESUME)

    def stop(self) -> None:
        self.request(SDCP_CMD_STOP, timeout=10)

    def delete(self, remote_name: str) -> None:
        self.request(SDCP_CMD_DELETE, {"FileList": [f"/local/{remote_name}"], "FolderList": []})


def sdcp_status_from_json(status: dict[str, Any]) -> PrinterStatus:
    if not status:
        return PrinterStatus("unknown", detail="No status reported")
    current = status.get("CurrentStatus")
    code = current[0] if isinstance(current, list) and current else current
    info = status.get("PrintInfo") or {}
    print_code = info.get("Status")
    layer, total = info.get("CurrentLayer"), info.get("TotalLayer")
    progress = (layer / total) if isinstance(layer, (int, float)) and isinstance(total, (int, float)) and total else None
    state = SDCP_MACHINE_STATUS.get(code, "unknown")
    if state == "printing" and print_code in (5, 6):
        state = "paused"
    if state == "printing" or state == "paused":
        label = SDCP_PRINT_STATUS.get(print_code, "Printing")
        detail = f"{label} - layer {layer}/{total}" if total else label
    elif state == "idle":
        detail = "Idle"
    else:
        detail = "Busy (file transfer or self-test)"
    return PrinterStatus(
        state,
        detail=detail,
        progress=progress,
        current_layer=layer if isinstance(layer, int) else None,
        total_layers=total if isinstance(total, int) else None,
        filename=str(info.get("Filename") or ""),
        raw={"status": status},
    )


def _multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----EZSlicer" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------

def connect(printer: dict[str, Any]) -> ChituPrinter | SdcpPrinter:
    ip = str(printer.get("ip") or "").strip()
    if not ip:
        raise PrinterError("Printer has no IP address")
    protocol = str(printer.get("protocol") or "").lower()
    if protocol not in ("chitu", "sdcp"):
        info = probe(ip)
        protocol = info.protocol
        printer = {**printer, "mainboardId": info.mainboard_id}
    if protocol == "chitu":
        return ChituPrinter(ip)
    return SdcpPrinter(ip, str(printer.get("mainboardId") or printer.get("mainboard_id") or ""))


def send_file(
    printer: dict[str, Any],
    path: str | Path,
    *,
    start: bool = False,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Upload a sliced file (refusing if the printer is mid-print) and optionally start it."""
    path = Path(path)
    if not path.is_file():
        raise PrinterError(f"Sliced file not found: {path}")
    with connect(printer) as client:
        protocol = "chitu" if isinstance(client, ChituPrinter) else "sdcp"
        status = client.status()
        if status.state in ("printing", "paused", "busy"):
            raise PrinterError(f"Printer is {status.detail.lower()}; wait for it to finish before uploading.")
        name = unique_name(remote_filename(path.name, protocol), client.list_files(), protocol)
        if progress:
            progress(f"Uploading {name}", 0.0)
        client.upload(path, name, progress)
        started = False
        if start:
            if progress:
                progress(f"Starting {name}", 1.0)
            client.start_print(name)
            started = True
        return {"remoteName": name, "started": started, "bytes": path.stat().st_size}
