import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from resin_slicer import material_service, network_printers
from resin_slicer.network_printers import PrinterStatus


class FakeSdcp:
    """Stands in for SdcpPrinter: a scripted list of statuses, records pauses."""

    script: list[PrinterStatus] = []
    pauses = 0
    refuse_pauses = 0

    def __init__(self, ip: str, mainboard_id: str = "") -> None:
        self.last_status = {}

    def status(self) -> PrinterStatus:
        return FakeSdcp.script.pop(0) if len(FakeSdcp.script) > 1 else FakeSdcp.script[0]

    def pause(self) -> None:
        if FakeSdcp.refuse_pauses:
            FakeSdcp.refuse_pauses -= 1
            raise network_printers.PrinterError("busy")
        FakeSdcp.pauses += 1

    def close(self) -> None:
        pass


def printing(name: str) -> PrinterStatus:
    return PrinterStatus("printing", detail="Exposing", filename=f"/local/{name}")


IDLE = PrinterStatus("idle", detail="Idle")


class WatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = material_service.MaterialService(Path(self.tmp.name))
        self.store = self.service.store
        self.store.upsert("MB1", {"name": "Mars", "ip": "10.0.0.5", "protocol": "sdcp", "material": "Siraya Fast Grey",
                                  "policy": "pause"}, "test")
        FakeSdcp.pauses = 0
        FakeSdcp.refuse_pauses = 0
        patcher = mock.patch.object(network_printers, "SdcpPrinter", FakeSdcp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.watch = material_service.PrinterWatch(self.service, "MB1", 0.01, grace_s=0)

    def run_polls(self, *statuses: PrinterStatus) -> None:
        for status in statuses:
            FakeSdcp.script = [status]
            self.watch.poll()

    def test_mismatch_pauses_once(self) -> None:
        self.store.record_job("MB1", "part.goo", "Elegoo ABS-like Clear", "test")
        self.run_polls(IDLE, printing("part.goo"), printing("part.goo"), printing("part.goo"))
        events = self.store.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "mismatch")
        self.assertEqual(events[0]["action"], "paused")
        self.assertEqual(FakeSdcp.pauses, 1)

    def test_match_is_quiet(self) -> None:
        self.store.record_job("MB1", "part.goo", "siraya fast grey", "test")
        self.run_polls(IDLE, printing("part.goo"))
        self.assertEqual(self.store.events(), [])
        self.assertTrue(self.store.live["MB1"]["check"]["match"])

    def test_already_running_is_reported_not_paused(self) -> None:
        self.store.record_job("MB1", "part.goo", "Elegoo ABS-like Clear", "test")
        self.run_polls(printing("part.goo"))
        self.assertEqual(self.store.events()[0]["action"], "none")
        self.assertEqual(FakeSdcp.pauses, 0)

    def test_unknown_file_warns_but_only_strict_pauses(self) -> None:
        self.run_polls(IDLE, printing("usb.ctb"))
        self.assertEqual(self.store.events()[-1]["kind"], "unknown-material")
        self.assertEqual(FakeSdcp.pauses, 0)
        self.store.upsert("MB1", {"policy": "strict"}, "test")
        self.run_polls(IDLE, printing("usb.ctb"))
        self.assertEqual(self.store.events()[-1]["action"], "paused")

    def test_pause_retries_while_printer_is_busy(self) -> None:
        FakeSdcp.refuse_pauses = 2
        self.store.record_job("MB1", "part.goo", "Elegoo ABS-like Clear", "test")
        with mock.patch.object(self.watch.stop_event, "wait", return_value=False):
            self.run_polls(IDLE, printing("part.goo"))
        self.assertEqual(self.store.events()[0]["action"], "paused")

    def test_ledger_written_just_after_start_is_found(self) -> None:
        self.watch.grace_s = 60
        self.run_polls(IDLE, printing("part.goo"))
        self.assertEqual(self.store.events(), [])  # still waiting for the ledger
        self.store.record_job("MB1", "part.goo", "Elegoo ABS-like Clear", "test")
        self.run_polls(printing("part.goo"))
        self.assertEqual(self.store.events()[0]["kind"], "mismatch")
        self.assertEqual(FakeSdcp.pauses, 1)

    def test_reprint_of_same_file_is_checked_again(self) -> None:
        self.store.record_job("MB1", "part.goo", "Elegoo ABS-like Clear", "test")
        self.run_polls(IDLE, printing("part.goo"), IDLE, printing("part.goo"))
        self.assertEqual(len(self.store.events()), 2)


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = material_service.MaterialService(Path(self.tmp.name), token="s3cret")
        self.server = material_service.make_server(self.service, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = "s3cret") -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "X-Jet-User": "talbot"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_set_and_read_material(self) -> None:
        status, record = self.call("PUT", "/api/v1/printers/MB1", {"name": "Mars", "ip": "10.0.0.5", "material": "Grey"})
        self.assertEqual(status, 200)
        self.assertEqual(record["materialSetBy"], "talbot")
        status, listing = self.call("GET", "/api/v1/printers", token=None)
        self.assertEqual(listing["printers"][0]["material"], "Grey")
        self.assertEqual(listing["printers"][0]["key"], "MB1")
        # survives a restart
        again = material_service.Store(Path(self.tmp.name) / "printer-materials.json")
        self.assertEqual(again.printer("MB1")["material"], "Grey")

    def test_writes_need_token(self) -> None:
        status, _ = self.call("PUT", "/api/v1/printers/MB1", {"material": "Grey"}, token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/health", token=None)
        self.assertEqual(status, 200)

    def test_bad_policy_and_jobs(self) -> None:
        status, _ = self.call("PUT", "/api/v1/printers/MB1", {"policy": "explode"})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/v1/jobs", {"printerKey": "MB1"})
        self.assertEqual(status, 400)
        status, job = self.call("POST", "/api/v1/jobs", {"printerKey": "MB1", "remoteName": "/local/a.goo", "material": "Grey"})
        self.assertEqual((status, job["remoteName"]), (200, "a.goo"))
        status, events = self.call("GET", "/api/v1/events?since=0", token=None)
        self.assertEqual(events, {"events": [], "last": 0})


if __name__ == "__main__":
    unittest.main()
