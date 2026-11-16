"""Contract tests for EZSlicer's engine API: every endpoint jET Hangar and EZSlicer's own window
depend on. They talk HTTP only (no resin_slicer imports), so they can check any running engine.

    python -m pytest tests/contract                     start a local engine and test it
    EZSLICER_ENGINE_URL=http://127.0.0.1:51234 JET_ENGINE_TOKEN=<secret> python -m pytest tests/contract
                                                        test an engine that's already running

Run them before tagging a release (npm run release does). They never touch a real printer:
printer calls go to 127.0.0.1, where nothing answers.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]

# What Hangar and the window rely on. Adding to the engine is fine; removing any of these is a
# new major API version.
REQUIRED_FEATURES = {
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
}
REQUIRED_PATHS = {
    "/api/health": "get",
    "/api/version": "get",
    "/api/capabilities": "get",
    "/api/openapi.json": "get",
    "/api/v1/info": "get",
    "/api/v1/profiles": "get",
    "/api/v1/preview": "post",
    "/api/v1/slice": "post",
    "/api/v1/printers/discover": "post",
    "/api/v1/printers/status": "post",
    "/api/v1/printers/status-all": "post",
    "/api/v1/printers/control": "post",
    "/api/v1/printers/send": "post",
    "/api/v1/files/material": "post",
}
NOBODY = {"id": "contract-test", "ip": "127.0.0.1", "protocol": "chitu"}  # nothing answers here


def _cube_stl(path: Path, size: float = 8.0) -> None:
    """A binary STL cube, written without resin_slicer so the test stays black-box."""
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    data = bytearray(b"contract cube".ljust(80, b" ")) + struct.pack("<I", len(faces))
    for face in faces:
        data += struct.pack("<3f", 0, 0, 0)
        for index in face:
            data += struct.pack("<3f", *v[index])
        data += b"\0\0"
    path.write_bytes(bytes(data))


class Engine:
    """Either the engine named by EZSLICER_ENGINE_URL, or one this test starts."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        url = os.environ.get("EZSLICER_ENGINE_URL", "").strip()
        if url:
            self.token = os.environ.get("JET_ENGINE_TOKEN", "")
            parts = urlsplit(url)
            self.host, self.port = parts.hostname or "127.0.0.1", parts.port or 80
            return
        self.token = secrets.token_hex(16)
        env = {**os.environ, "JET_ENGINE_TOKEN": self.token, "PYTHONUTF8": "1"}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "resin_slicer.engine", "--engine", "--parent-pid", str(os.getpid())],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = b""
        while not line.strip():
            line = self.process.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise RuntimeError("engine exited: " + self.process.stderr.read().decode(errors="replace"))  # type: ignore[union-attr]
        self.startup = json.loads(line)
        self.host, self.port = "127.0.0.1", int(self.startup["port"])

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(5)

    def request(self, method: str, path: str, body=None, *, token: str | None = "default", accept: str | None = None, raw: bytes | None = None, timeout: float = 120):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://{self.host}:{self.port}{path}", method=method, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token == "default":
            token = self.token
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if accept:
            req.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status, res.headers.get("Content-Type", ""), res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")

    def json(self, method: str, path: str, body=None, **kwargs):
        status, _, text = self.request(method, path, body, **kwargs)
        return status, json.loads(text)

    def stream(self, path: str, body) -> list[dict]:
        status, content_type, text = self.request("POST", path, body, accept="application/x-ndjson")
        assert status == 200, (status, text)
        assert content_type.startswith("application/x-ndjson"), content_type
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class EngineContract(unittest.TestCase):
    engine: Engine

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = Engine()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.model = cls.dir / "cube.stl"
        _cube_stl(cls.model)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.tmp.cleanup()

    def assertErrorShape(self, payload: dict, code: str | None = None) -> None:
        self.assertIn("error", payload)
        self.assertIsInstance(payload["error"].get("code"), str)
        self.assertIsInstance(payload["error"].get("message"), str)
        self.assertTrue(payload["error"]["message"])
        if code:
            self.assertEqual(payload["error"]["code"], code)

    def plate(self, **extra) -> dict:
        return {"inputPath": str(self.model), "profile": "small-test", "support": {"enabled": False}, **extra}

    # discovery endpoints (no token)

    def test_startup_line(self) -> None:
        if not self.engine.process:
            self.skipTest("engine started elsewhere")
        self.assertEqual(self.engine.startup["engine"], "ezslicer")
        for key in ("version", "api", "port"):
            self.assertIn(key, self.engine.startup)

    def test_health(self) -> None:
        status, body = self.engine.json("GET", "/api/health", token=None)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)

    def test_version(self) -> None:
        status, body = self.engine.json("GET", "/api/version", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "ezslicer")
        self.assertRegex(body["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(body["api"], r"^\d+\.\d+$")
        self.assertIsInstance(body["commit"], str)

    def test_capabilities(self) -> None:
        status, body = self.engine.json("GET", "/api/capabilities", token=None)
        self.assertEqual(status, 200)
        _, version = self.engine.json("GET", "/api/version", token=None)
        self.assertEqual(body["api"], version["api"])
        self.assertEqual(body["api"].split(".")[0], "1", "Hangar speaks API 1.x")
        self.assertTrue(REQUIRED_FEATURES <= set(body["features"]), REQUIRED_FEATURES - set(body["features"]))

    def test_openapi_describes_every_route(self) -> None:
        status, doc = self.engine.json("GET", "/api/openapi.json", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(str(doc["openapi"]).startswith("3."))
        for path, method in REQUIRED_PATHS.items():
            self.assertIn(method, doc["paths"].get(path, {}), f"{method.upper()} {path} isn't in the OpenAPI document")
        _, version = self.engine.json("GET", "/api/version", token=None)
        self.assertEqual(doc["info"]["version"], version["api"])

    # sign-in and errors

    def test_token_required(self) -> None:
        for token in (None, "wrong"):
            status, body = self.engine.json("GET", "/api/v1/profiles", token=token)
            self.assertEqual(status, 401)
            self.assertErrorShape(body, "unauthorized")
        status, body = self.engine.json("POST", "/api/v1/printers/status-all", {"printers": []}, token=None)
        self.assertEqual(status, 401)

    def test_unknown_route(self) -> None:
        status, body = self.engine.json("GET", "/api/v1/no-such-thing")
        self.assertEqual(status, 404)
        self.assertErrorShape(body, "not_found")

    def test_wrong_method(self) -> None:
        status, body = self.engine.json("GET", "/api/v1/slice")
        self.assertEqual(status, 405)
        self.assertErrorShape(body)

    def test_invalid_json(self) -> None:
        status, _, text = self.engine.request("POST", "/api/v1/printers/discover", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertErrorShape(json.loads(text), "invalid_json")

    # the work

    def test_info(self) -> None:
        status, body = self.engine.json("GET", "/api/v1/info")
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "info")
        self.assertEqual(body["app"], "ezslicer")

    def test_profiles(self) -> None:
        status, body = self.engine.json("GET", "/api/v1/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "profiles")
        self.assertIn("generic-2k", body["profiles"])
        self.assertIn("resolution_x", body["profiles"]["generic-2k"])

    def test_preview_streams_then_answers(self) -> None:
        messages = self.engine.stream("/api/v1/preview", self.plate(support={"enabled": True}))
        final = messages[-1]
        self.assertEqual(final["type"], "preview", final)
        self.assertGreater(final["layers"], 0)
        self.assertIn("bounds", final)
        self.assertIsInstance(final["supports"], list)
        for message in messages[:-1]:
            self.assertIn(message["type"], ("progress", "support"))

    def test_preview_without_stream_answers_result_only(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/preview", self.plate())
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "preview")

    def test_slice_ctb_with_layer_images(self) -> None:
        out = self.dir / "plate.ctb"
        layers = self.dir / "layers"
        messages = self.engine.stream("/api/v1/slice", self.plate(outputPath=str(out), format="ctb", previewDir=str(layers), previewScale=4))
        final = messages[-1]
        self.assertEqual(final["type"], "done", final)
        self.assertTrue(Path(final["outputPath"]).is_file())
        self.assertGreater(final["layers"], 0)
        self.assertEqual(Path(final["previewDir"]), layers)
        images = sorted(layers.glob("layer_*.png"))
        self.assertEqual(len(images), final["layers"])
        self.assertTrue(any(m["type"] == "progress" and re.search(r"writing layer \d+/\d+", m.get("message", "")) for m in messages))

    def test_slice_goo(self) -> None:
        out = self.dir / "plate.goo"
        status, body = self.engine.json("POST", "/api/v1/slice", self.plate(outputPath=str(out), format="goo"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["type"], "done")
        self.assertTrue(out.is_file())

    def test_slice_refuses_bad_requests(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/slice", self.plate(outputPath=str(self.dir / "x.stl"), format="stl"))
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")
        status, body = self.engine.json("POST", "/api/v1/slice", self.plate())
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")

    def test_job_failure_is_an_error(self) -> None:
        missing = {"inputPath": str(self.dir / "missing.stl"), "profile": "small-test"}
        status, body = self.engine.json("POST", "/api/v1/preview", missing)
        self.assertEqual(status, 422)
        self.assertErrorShape(body)
        messages = self.engine.stream("/api/v1/preview", missing)
        self.assertEqual(messages[-1]["type"], "error")
        self.assertErrorShape(messages[-1])

    def test_hanging_up_stops_the_job(self) -> None:
        connection = http.client.HTTPConnection(self.engine.host, self.engine.port, timeout=30)
        body = json.dumps(self.plate(outputPath=str(self.dir / "cancel.ctb"), format="ctb", support={"enabled": True}))
        connection.request("POST", "/api/v1/slice", body, {"Authorization": f"Bearer {self.engine.token}", "Accept": "application/x-ndjson", "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.fp.readline()  # the job has started
        connection.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, health = self.engine.json("GET", "/api/health", token=None)
            if health.get("jobs", 0) == 0:
                break
            time.sleep(0.25)
        self.assertEqual(health.get("jobs", 0), 0)

    # printers (nothing answers on 127.0.0.1)

    def test_discover(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/printers/discover", {"targets": ["127.0.0.1"], "timeout": 0.3, "broadcast": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "printers")
        self.assertIsInstance(body["printers"], list)

    def test_status_all_reports_unreachable_per_printer(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/printers/status-all", {"printers": [NOBODY]})
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "printer-statuses")
        self.assertEqual(len(body["statuses"]), 1)
        self.assertEqual(body["statuses"][0]["id"], "contract-test")
        self.assertIs(body["statuses"][0]["ok"], False)
        self.assertTrue(body["statuses"][0]["error"])
        status, body = self.engine.json("POST", "/api/v1/printers/status-all", {"printers": []})
        self.assertEqual((status, body["statuses"]), (200, []))

    def test_status_of_unreachable_printer_is_an_error(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/printers/status", {"printer": NOBODY})
        self.assertEqual(status, 502)
        self.assertErrorShape(body)

    def test_printer_requests_are_checked(self) -> None:
        status, body = self.engine.json("POST", "/api/v1/printers/status", {})
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")
        status, body = self.engine.json("POST", "/api/v1/printers/control", {"printer": NOBODY, "action": "explode"})
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")
        status, body = self.engine.json("POST", "/api/v1/printers/send", {"printer": NOBODY, "path": str(self.dir / "missing.ctb")})
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")

    def _sliced_for(self, resin: str) -> Path:
        from resin_slicer.config import SupportConfig, profile
        from resin_slicer.mesh import cube_mesh
        from resin_slicer.pipeline import SliceJob, slice_to_file

        path = self.dir / f"{resin.replace(' ', '_')}.goo"
        config = profile("small-test").with_overrides(layer_height_mm=2.0, resin_name=resin)
        slice_to_file(SliceJob(cube_mesh(6), config, SupportConfig(enabled=False)), path, "goo")
        return path

    def test_file_material(self) -> None:
        path = self._sliced_for("Siraya Fast Grey")
        status, body = self.engine.json("POST", "/api/v1/files/material", {"path": str(path)})
        self.assertEqual((status, body["type"], body["material"]), (200, "file-material", "Siraya Fast Grey"))
        status, body = self.engine.json("POST", "/api/v1/files/material", {"path": str(self.dir / "missing.goo")})
        self.assertEqual(status, 400)
        self.assertErrorShape(body, "invalid_request")

    def test_send_refuses_other_resin_before_touching_the_printer(self) -> None:
        path = self._sliced_for("Siraya Fast Grey")
        request = {"printer": NOBODY, "path": str(path), "loadedMaterial": "Elegoo ABS-like Clear"}
        status, body = self.engine.json("POST", "/api/v1/printers/send", request)
        self.assertEqual(status, 409)
        self.assertErrorShape(body, "material_mismatch")
        self.assertEqual(body["error"]["details"], {"fileMaterial": "Siraya Fast Grey", "loadedMaterial": "Elegoo ABS-like Clear"})
        # a matching resin (or an override) gets past the check to the (unreachable) printer
        for extra in ({"loadedMaterial": "siraya fast grey"}, {"allowMaterialMismatch": True}):
            status, body = self.engine.json("POST", "/api/v1/printers/send", {**request, **extra})
            self.assertNotEqual(status, 409, body)


if __name__ == "__main__":
    unittest.main()
