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


class Recorder:
    def __init__(self) -> None:
        self.reports: list[dict] = []

    def report(self, body: dict) -> None:
        self.reports.append(body)


def ended(code: int, error: int = 0) -> PrinterStatus:
    return PrinterStatus("idle", detail="Idle", raw={"status": {"PrintInfo": {"Status": code, "ErrorNumber": error}}})


class StatsTests(unittest.TestCase):
    """Each resin job reported to jET's stats: sent, printing, then how it ended."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recorder = Recorder()
        self.service = material_service.MaterialService(Path(self.tmp.name), stats=self.recorder)
        self.store = self.service.store
        self.store.upsert("MB1", {"name": "Mars 5 Ultra", "ip": "10.0.0.5", "protocol": "sdcp", "material": "Siraya Fast Grey"}, "test")
        patcher = mock.patch.object(network_printers, "SdcpPrinter", FakeSdcp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.watch = material_service.PrinterWatch(self.service, "MB1", 0.01, grace_s=0)

    def run_polls(self, *statuses: PrinterStatus) -> None:
        for status in statuses:
            FakeSdcp.script = [status]
            self.watch.poll()

    def send(self, name: str = "part.goo") -> dict:
        entry = self.store.record_job("MB1", name, "Siraya Fast Grey", "test", {"name": "part", "person": "HHMI\\lima", "parts": 3, "volumeMl": 12.5, "seconds": 3600})
        self.service.report("MB1", entry, "sent")
        return entry

    def test_sent_printing_done_share_one_job(self) -> None:
        entry = self.send()
        self.run_polls(IDLE, printing("part.goo"), printing("part.goo"), ended(9))
        self.assertEqual([r["status"] for r in self.recorder.reports], ["sent", "printing", "done"])
        self.assertEqual({r["job_id"] for r in self.recorder.reports}, {entry["jobId"]})
        sent = self.recorder.reports[0]
        self.assertEqual((sent["app"], sent["kind"], sent["printer"], sent["material"]), ("ezslicer", "resin", "Mars 5 Ultra", "Siraya Fast Grey"))
        self.assertEqual((sent["parts"], sent["volume_cm3"], sent["seconds"]), (3, 12.5, 3600))
        done = self.recorder.reports[-1]
        self.assertLess(done["seconds"], 60)  # the real time, not the estimate
        self.assertEqual(self.store.data["running"], {})

    def test_how_prints_end(self) -> None:
        cases = [(ended(8), "cancelled"), (ended(0, error=3), "failed"), (ended(9), "done")]
        for status, outcome in cases:
            self.run_polls(IDLE, printing("usb.ctb"), status)
            self.assertEqual(self.recorder.reports[-1]["status"], outcome)
        # no PrintInfo: judged by the last layer seen
        self.run_polls(IDLE, PrinterStatus("printing", filename="usb.ctb", current_layer=40, total_layers=100), IDLE)
        self.assertEqual(self.recorder.reports[-1]["status"], "cancelled")

    def test_reprint_is_a_new_job_with_the_same_numbers(self) -> None:
        entry = self.send()
        self.run_polls(IDLE, printing("part.goo"), ended(9), printing("part.goo"), ended(9))
        printing_reports = [r for r in self.recorder.reports if r["status"] == "printing"]
        self.assertEqual(printing_reports[0]["job_id"], entry["jobId"])
        self.assertNotEqual(printing_reports[1]["job_id"], entry["jobId"])
        self.assertEqual(printing_reports[1]["volume_cm3"], 12.5)
        # the send is the sender's; a reprint is started at the printer, by nobody known
        self.assertEqual(printing_reports[0]["person"], "HHMI\\lima")
        self.assertNotIn("person", printing_reports[1])

    def test_person_rides_on_every_report_of_a_sent_job(self) -> None:
        self.send()
        self.run_polls(IDLE, printing("part.goo"), ended(9), printing("usb.ctb"), ended(9))
        sent_job = [r for r in self.recorder.reports if r["name"] == "part"]
        self.assertEqual([r["person"] for r in sent_job], ["HHMI\\lima"] * 3)
        self.assertTrue(all("person" not in r for r in self.recorder.reports if r["name"] == "usb.ctb"))

    def test_file_transfer_is_not_the_end(self) -> None:
        self.run_polls(IDLE, printing("usb.ctb"), PrinterStatus("busy"), printing("usb.ctb"))
        self.assertEqual([r["status"] for r in self.recorder.reports], ["printing"])

    def test_restart_mid_print_keeps_the_job(self) -> None:
        self.send()
        self.run_polls(IDLE, printing("part.goo"))
        first = self.recorder.reports[-1]["job_id"]
        again = material_service.MaterialService(Path(self.tmp.name), stats=self.recorder)
        watch = material_service.PrinterWatch(again, "MB1", 0.01, grace_s=0)
        for status in (printing("part.goo"), ended(9)):
            FakeSdcp.script = [status]
            watch.poll()
        self.assertEqual(self.recorder.reports[-1]["job_id"], first)
        self.assertEqual(self.recorder.reports[-1]["status"], "done")
        self.assertLess(self.recorder.reports[-1]["seconds"], 60)  # timed from the start it saw before the restart

    def test_print_already_running_at_first_look_has_no_time(self) -> None:
        self.run_polls(printing("usb.ctb"), ended(9))
        self.assertEqual(self.recorder.reports[-1]["status"], "done")
        self.assertNotIn("seconds", self.recorder.reports[-1])

    def test_bad_numbers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            material_service.job_stats({"parts": "lots"})
        with self.assertRaises(ValueError):
            material_service.job_stats({"volumeMl": -1})
        self.assertEqual(material_service.job_stats({"parts": 2, "name": " a ", "seconds": None}), {"parts": 2, "name": "a"})


class StatsReporterTests(unittest.TestCase):
    def test_posts_with_the_token_and_ignores_a_404(self) -> None:
        seen = []

        class Console(material_service.BaseHTTPRequestHandler):
            code = 202

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.headers.get("Authorization"), body))
                self.send_response(Console.code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        server = material_service.ThreadingHTTPServer(("127.0.0.1", 0), Console)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "console_token.txt"
            token.write_text("tok\n", encoding="utf-8")
            reporter = material_service.StatsReporter.__new__(material_service.StatsReporter)
            reporter.url, reporter.token_file, reporter.warned = f"http://127.0.0.1:{server.server_address[1]}/api/v1/stats/jobs", token, set()
            self.assertTrue(reporter.post({"job_id": "j1"}))
            Console.code = 404
            self.assertFalse(reporter.post({"job_id": "j2"}))
        self.assertEqual(seen[0], ("Bearer tok", {"job_id": "j1"}))
        reporter.url = "http://127.0.0.1:9/nothing"
        self.assertFalse(reporter.post({"job_id": "j3"}))  # no answer: dropped, no exception
        self.assertEqual(reporter.warned, set())

    def test_off_without_a_url(self) -> None:
        reporter = material_service.StatsReporter()
        reporter.report({"job_id": "x"})
        self.assertTrue(reporter.queue.empty())


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
        status, _ = self.call("POST", "/api/v1/jobs", {"printerKey": "MB1", "remoteName": "b.goo", "parts": "many"})
        self.assertEqual(status, 400)
        status, job = self.call("POST", "/api/v1/jobs", {"printerKey": "MB1", "remoteName": "c.goo", "parts": 2, "volumeMl": 4.5})
        self.assertEqual((job["parts"], job["volumeMl"]), (2, 4.5))
        self.assertEqual(job["person"], "talbot")  # X-Jet-User, when the body doesn't say
        status, job = self.call("POST", "/api/v1/jobs", {"printerKey": "MB1", "remoteName": "d.goo", "person": "HHMI\\lima"})
        self.assertEqual(job["person"], "HHMI\\lima")
        status, events = self.call("GET", "/api/v1/events?since=0", token=None)
        self.assertEqual(events, {"events": [], "last": 0})


if __name__ == "__main__":
    unittest.main()
