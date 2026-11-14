import json
import threading
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from resin_slicer import electron_bridge as eb
from resin_slicer import engine
from resin_slicer import network_printers as npr
from resin_slicer.config import profile
from resin_slicer.engine_info import CAPABILITIES, version_json
from resin_slicer.engine_openapi import openapi_document
from resin_slicer.mesh import cube_mesh
from test_network_printers import FakeChituBoard


class BridgeCommandTests(unittest.TestCase):
    """The JSON-lines commands jET Hangar's hangar_bridge.py used to provide."""

    def run_bridge(self, command, request=None):
        lines = []
        with mock.patch.object(eb, "_write_json", lines.append), mock.patch.object(eb, "_read_json", return_value=request or {}):
            code = eb.main([command])
        return code, lines

    def test_every_hangar_command_is_accepted(self):
        for command in ("info", "preview", "slice", "printer-status-all", "printer-discover", "printer-status", "printer-send", "printer-control"):
            self.assertIn(command, eb.COMMANDS)

    def test_info(self):
        code, lines = self.run_bridge("info")
        self.assertEqual(code, 0)
        info = lines[-1]
        self.assertEqual(info["type"], "info")
        for key in ("engine", "version", "api", "commit", "date", "python"):
            self.assertIn(key, info)

    def test_status_all_reports_each_printer(self):
        class Client:
            def __init__(self, printer):
                self.printer = printer

            def __enter__(self):
                if self.printer["ip"] == "bad":
                    raise npr.PrinterError("no answer")
                return self

            def __exit__(self, *exc):
                return False

            def status(self):
                return npr.PrinterStatus(state="idle")

        with mock.patch.object(eb.network_printers, "connect", Client):
            code, lines = self.run_bridge("printer-status-all", {"printers": [{"id": "a", "ip": "good"}, {"id": "b", "ip": "bad"}, "junk"]})
        self.assertEqual(code, 0)
        statuses = lines[-1]["statuses"]
        self.assertEqual(lines[-1]["type"], "printer-statuses")
        self.assertEqual([(s["id"], s["ok"]) for s in statuses], [("a", True), ("b", False)])
        self.assertEqual(statuses[0]["state"], "idle")
        self.assertEqual(statuses[1]["error"], "no answer")

    def test_slice_takes_layer_image_folder_and_scale(self):
        result = SimpleNamespace(output_path=Path("out.ctb"), layer_count=1, support_count=0, material_ml=0.1, support_report=None)
        with TemporaryDirectory() as tmp, (
            mock.patch.object(eb, "_mesh_from_request", return_value=cube_mesh(1))
        ), mock.patch.object(eb, "_config_from_request", return_value=profile("generic-2k")), mock.patch.object(eb, "_write_json") as write, mock.patch.object(eb, "slice_to_file", return_value=result) as slice_to_file:
            layers = str(Path(tmp) / "layers")
            eb._slice({"outputPath": "out.ctb", "format": "ctb", "previewDir": layers, "previewScale": 4})
            self.assertTrue(Path(layers).is_dir())
        self.assertEqual(slice_to_file.call_args.kwargs["preview_dir"], layers)
        self.assertEqual(slice_to_file.call_args.kwargs["preview_scale"], 4)
        self.assertEqual(write.call_args.args[0]["previewDir"], layers)


class EngineDescriptionTests(unittest.TestCase):
    def test_openapi_lists_every_route(self):
        handler = engine.EngineHandler.__new__(engine.EngineHandler)
        paths = openapi_document()["paths"]
        for method, path in handler._routes():
            self.assertIn(method.lower(), paths.get(path, {}), f"{method} {path} is missing from engine_openapi.py")

    def test_version_and_capabilities_agree(self):
        info = version_json()
        self.assertEqual(info["app"], "ezslicer")
        self.assertEqual(openapi_document()["info"]["version"], info["api"])
        self.assertEqual(len(set(CAPABILITIES)), len(CAPABILITIES))


class EngineSendTests(unittest.TestCase):
    """printers/send through the HTTP engine, to a fake ChiTu board on 127.0.0.1."""

    def setUp(self):
        self.board = FakeChituBoard()
        self.board.start()
        patcher = mock.patch.object(npr, "CHITU_PORT", self.board.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.board.stop)
        handler = type("TestHandler", (engine.EngineHandler,), {"token": "secret"})
        self.server = engine.EngineServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body, stream=False):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer secret", **({"Accept": "application/x-ndjson"} if stream else {})},
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read().decode()

    def test_send_streams_progress_then_result(self):
        payload = bytes(range(256)) * 20
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plate.ctb"
            path.write_bytes(payload)
            text = self.post("/api/v1/printers/send", {"printer": {"ip": "127.0.0.1", "protocol": "chitu"}, "path": str(path), "start": True}, stream=True)
        messages = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(messages[-1]["type"], "printer-sent")
        self.assertEqual(messages[-1]["remoteName"], "plate.ctb")
        self.assertTrue(messages[-1]["started"])
        self.assertTrue(any(m["type"] == "progress" and "fraction" in m for m in messages[:-1]))
        self.assertEqual(self.board.files["plate.ctb"], payload)

    def test_status_through_engine(self):
        body = json.loads(self.post("/api/v1/printers/status", {"printer": {"ip": "127.0.0.1", "protocol": "chitu"}}))
        self.assertEqual(body["type"], "printer-status")
        self.assertEqual(body["status"]["state"], "idle")

    def test_send_refuses_wrong_format(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plate.goo"
            path.write_bytes(b"x")
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/printers/send",
                data=json.dumps({"printer": {"ip": "127.0.0.1", "protocol": "chitu"}, "path": str(path)}).encode(),
                method="POST",
                headers={"Authorization": "Bearer secret"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=20)
        self.assertEqual(caught.exception.code, 502)
        error = json.loads(caught.exception.read())["error"]
        self.assertEqual(error["code"], "printer_error")
        self.assertRegex(error["message"], "GOO.*CTB")


class NoCurrentFolderTests(unittest.TestCase):
    """An embeddable Python (._pth file) never imports from the current folder; python -I is the
    same. The engine's children and the root launcher must work anyway."""

    def run_isolated(self, args):
        import subprocess
        import sys

        with TemporaryDirectory() as elsewhere:
            return subprocess.run([sys.executable, "-I", *args], cwd=elsewhere, input=b"{}", capture_output=True, timeout=60)

    def test_child_boot_finds_the_package(self):
        done = self.run_isolated(["-c", engine._CHILD_BOOT, str(engine.PACKAGE_DIR.parent), "info"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.splitlines()[-1])["type"], "info")

    def test_root_launcher(self):
        launcher = str(engine.PACKAGE_DIR.parent / "ezslicer_engine.py")
        done = self.run_isolated([launcher, "bridge", "info"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.splitlines()[-1])["app"], "ezslicer")
        done = self.run_isolated([launcher, "--version"])
        self.assertEqual(json.loads(done.stdout)["app"], "ezslicer")


class ParentWatchTests(unittest.TestCase):
    def test_this_process_is_alive_and_a_dead_pid_is_not(self):
        import os
        import subprocess
        import sys

        self.assertTrue(engine._parent_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(engine._parent_alive(child.pid))


if __name__ == "__main__":
    unittest.main()
