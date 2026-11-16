"""What this EZSlicer engine is: its id, version, API version and the commit it was built from.

The API version is major.minor. A minor version only adds (new endpoints, new optional fields);
anything that removes or changes something is a new major version, served under /api/v<major>/
next to the old one.
"""
from __future__ import annotations

import json
import platform
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__

ENGINE_ID = "ezslicer"
API_VERSION = "1.0"

PACKAGE_DIR = Path(__file__).resolve().parent
# Written into the package by tools/package_engine.py, so a released engine knows its commit
# without git.
BUILD_FILE = PACKAGE_DIR / "BUILD.json"

# Feature names Hangar keys off. Add the name when you ship the feature; never remove one
# without a new major API version.
CAPABILITIES = (
    "engine.info",
    "profiles.list",
    "supports.preview",
    "slice.run",
    "slice.layer-images",
    "formats.ctb",
    "formats.goo",
    "printers.discover",
    "printers.status",
    "printers.status-all",
    "printers.send",
    "printers.control",
    "printers.material-check",
    "files.material",
)


@lru_cache(maxsize=1)
def build_info() -> dict[str, str]:
    """{"commit", "date"} of this engine: from BUILD.json in a released engine, else git."""
    try:
        data = json.loads(BUILD_FILE.read_text(encoding="utf-8"))
        return {"commit": str(data.get("commit", "")), "date": str(data.get("date", ""))}
    except (OSError, ValueError):
        pass
    repo = PACKAGE_DIR.parent
    if (repo / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "log", "-1", "--format=%H|%cI"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            commit, _, date = out.partition("|")
            return {"commit": commit, "date": date}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"commit": "", "date": ""}


def version_json() -> dict[str, Any]:
    """The answer to GET /api/version (and the bridge's `info` command)."""
    return {
        "app": ENGINE_ID,
        "name": "EZSlicer",
        "version": __version__,
        "api": API_VERSION,
        **build_info(),
        "python": platform.python_version(),
    }
