"""Which resin a sliced file was made for, and whether it matches the resin in a printer.

Both formats EZSlicer writes already carry the resin profile's name:

* GOO keeps it in a fixed 32-byte ASCII field of the header (so long names are cut to 32).
* CTB v4/v5 keeps it in the resin-parameters block, which sits right after the CBD
  disclaimer text in the unencrypted part of the file (ChituBox lays it out the same way).

Names are compared loosely (case, spacing and punctuation are ignored), and a name cut to
GOO's 32 characters matches a longer name that starts the same.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

GOO_RESIN_OFFSET = 4 + 8 + 32 + 24 + 24 + 32 + 32  # version, magic, software, version, date, machine, profile
GOO_FIELD = 32
CTB_MAGICS = (0x12FD0086, 0x12FD0106, 0x12FD0107)  # CTB v2/v3, v4, v5 (encrypted)
CTB_DISCLAIMER_HEAD = b"Layout and record format for the ctb and cbddlp file types"
CTB_SCAN_BYTES = 256 * 1024  # the disclaimer sits before the previews and layers


def read_file_material(path: str | Path) -> str | None:
    """The resin name stored in a sliced CTB/GOO file, or None when it can't be told."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            head = handle.read(CTB_SCAN_BYTES)
    except OSError:
        return None
    suffix = path.suffix.lower()
    if suffix == ".goo" or head[4:12] == b"\x07\x00\x00\x00DLP\x00":
        return _goo_material(head)
    if suffix == ".ctb" or (len(head) >= 4 and struct.unpack_from("<I", head)[0] in CTB_MAGICS):
        return _ctb_material(head, path)
    return None


def _goo_material(head: bytes) -> str | None:
    field = head[GOO_RESIN_OFFSET:GOO_RESIN_OFFSET + GOO_FIELD]
    if len(field) < GOO_FIELD:
        return None
    name = field.split(b"\0", 1)[0].decode("ascii", "replace").strip()
    return name or None


def _ctb_material(head: bytes, path: Path) -> str | None:
    start = head.find(CTB_DISCLAIMER_HEAD)
    if start < 0:
        return None
    end = head.find(b".", head.find(b"sublicense", start))
    if end < 0:
        return None
    block = end + 1  # the resin block starts right after the disclaimer's final "."
    if block + 32 > len(head):
        return None
    size, offset = struct.unpack_from("<II", head, block + 20)
    if not 0 < size <= 256:
        return None
    data = head[offset:offset + size] if offset + size <= len(head) else _read_at(path, offset, size)
    name = data.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return name or None


def _read_at(path: Path, offset: int, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(size)
    except OSError:
        return b""


def normalize_material(name: str | None) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", str(name or "").lower()).strip()


def materials_match(loaded: str | None, job: str | None) -> bool | None:
    """True/False when both are known; None when either is missing (can't tell)."""
    a, b = normalize_material(loaded), normalize_material(job)
    if not a or not b:
        return None
    if a == b:
        return True
    # GOO keeps only 32 characters: a cut-off name matches the full one it starts.
    short, full = sorted((str(loaded).strip(), str(job).strip()), key=len)
    if len(short) == GOO_FIELD and normalize_material(full[:GOO_FIELD]) == normalize_material(short):
        return True
    return False
