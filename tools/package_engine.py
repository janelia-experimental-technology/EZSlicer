"""Package EZSlicer's engine on its own: dist/EZSlicer-Engine-<version>.zip plus its .sha256.

    python tools/package_engine.py [--out dist] [--allow-dirty]

The zip is plain Python (no packages to install) for any Python 3.10+, such as the embeddable
build jET Hangar ships:

    resin_slicer/            the engine (BUILD.json inside records version, commit, date)
    version.txt              first line = the bare version
    LICENSE
    ENGINE.md                how to run it

A release is built from its tag, never from uncommitted work: it refuses a dirty tree unless
--allow-dirty (for trying it out locally).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "resin_slicer"

ENGINE_MD = """# EZSlicer engine {version}

EZSlicer's slicer and resin-printer connections, without the window. Needs Python 3.10 or newer;
nothing to install. Run it from this folder (the one holding `resin_slicer`).

## HTTP engine (what jET Hangar uses)

    set JET_ENGINE_TOKEN=<a secret you make up>
    python -m resin_slicer.engine --engine

It listens on 127.0.0.1 on a free port and prints one line, for example
`{{"engine": "ezslicer", "version": "{version}", "api": "<major.minor>", "port": 51234}}`.
Send `Authorization: Bearer <secret>` with every request except /api/health, /api/version,
/api/capabilities and /api/openapi.json. The whole API is described at /api/openapi.json.
The engine exits when the process that started it exits.

## JSON-lines bridge

    python -m resin_slicer.electron_bridge <command>   (JSON request on stdin)

Commands: info, profiles, preview, slice, printer-discover, printer-status, printer-status-all,
printer-send, printer-control. Messages, one per line: progress, support, then the result
(info, profiles, preview, done, printers, printer-status, printer-statuses, printer-sent) or
error.
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def version() -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', (PACKAGE / "__init__.py").read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit("package_engine: no __version__ in resin_slicer/__init__.py")
    return match.group(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "dist"), help="output folder (default dist/)")
    parser.add_argument("--allow-dirty", action="store_true", help="package uncommitted work (not for releases)")
    args = parser.parse_args(argv)

    if not args.allow_dirty and git("status", "--porcelain", "--", "resin_slicer", "LICENSE"):
        sys.exit("package_engine: resin_slicer has uncommitted changes; commit them (or pass --allow-dirty)")
    ver = version()
    build = {
        "app": "ezslicer",
        "version": ver,
        "commit": git("rev-parse", "HEAD"),
        "date": git("log", "-1", "--format=%cI"),
        "tag": git("tag", "--points-at", "HEAD", "--list", "v[0-9]*") or "",
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"EZSlicer-Engine-{ver}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PACKAGE.rglob("*")):
            relative = path.relative_to(ROOT)
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo") or path.name == "BUILD.json":
                continue
            zf.write(path, relative.as_posix())
        zf.writestr("resin_slicer/BUILD.json", json.dumps(build, indent=2) + "\n")
        zf.writestr("version.txt", ver + "\n")
        zf.writestr("ENGINE.md", ENGINE_MD.format(version=ver))
        zf.write(ROOT / "LICENSE", "LICENSE")
    checksum = sha256(zip_path)
    sha_path = zip_path.with_name(zip_path.name + ".sha256")
    sha_path.write_text(f"{checksum}  {zip_path.name}\n", encoding="ascii", newline="\n")
    print(zip_path)
    print(sha_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
