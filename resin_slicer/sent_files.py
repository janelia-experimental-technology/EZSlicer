"""The files this PC sent to ChiTu printers, so a print's progress can name the file and its layer.

ChiTu boards (the Elegoo Jupiter) say only that they're printing, and how far into the file they've
read (M4000 ``D:<byte>/<file size>/<paused>``). They don't say which file. EZSlicer keeps a short
list of the files it sent to each of them. When the size being printed is the size of one of those,
that's the file. Its layer table (where each layer's data starts, from formats.ctb.read_layer_starts)
then turns the byte the printer has reached into the layer it's on.

Kept in ``<data folder>/sent-files.json``. The data folder is ``%LOCALAPPDATA%\\EZSlicer`` on
Windows, else ``~/.local/share/ezslicer``, and ``EZSLICER_DATA_DIR`` overrides it. The engine and
EZSlicer's window on one PC share it. Another PC's sends aren't known here.
"""
from __future__ import annotations

import json
import os
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any

FILE_NAME = "sent-files.json"
KEEP_PER_PRINTER = 20


def data_dir() -> Path:
    configured = os.environ.get("EZSLICER_DATA_DIR")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "EZSlicer"
    return Path.home() / ".local" / "share" / "ezslicer"


def _path() -> Path:
    return data_dir() / FILE_NAME


def _load() -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(ip: str) -> str:
    return f"chitu:{ip}"


def record(ip: str, name: str, size: int, layer_starts: list[int] | None) -> None:
    """Remember a file sent to the ChiTu printer at ip (newest first; the oldest are dropped)."""
    data = _load()
    entries = [e for e in data.get(_key(ip), []) if e.get("name") != name and e.get("size") != size]
    entries.insert(0, {
        "name": name,
        "size": int(size),
        "layers": len(layer_starts or []),
        "starts": list(layer_starts or []),
        "sentAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    data[_key(ip)] = entries[:KEEP_PER_PRINTER]
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{FILE_NAME}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def lookup(ip: str, size: int) -> dict[str, Any] | None:
    """The file sent to that printer whose size is the one it's printing, or None."""
    if not size:
        return None
    for entry in _load().get(_key(ip), []):
        if entry.get("size") == size:
            return entry
    return None


def layer_at(entry: dict[str, Any], position: int) -> int | None:
    """The layer (1-based) the printer is on at that byte of the file, or None without a layer table."""
    starts = entry.get("starts") or []
    if not starts:
        return None
    return max(1, min(len(starts), bisect_right(starts, position)))
