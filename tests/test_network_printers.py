import socket
import struct
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from resin_slicer import network_printers as npr


class FakeChituBoard(threading.Thread):
    """UDP stand-in for a ChiTu board: answers commands and reassembles M28 uploads."""

    def __init__(self, files=None, printing=False, resend_once_at=None):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.files = dict(files or {})
        self.printing = printing
        self.resend_once_at = resend_once_at
        self.writing = None
        self.buffer = bytearray()
        self.bad_frames = 0
        self.commands = []
        self.running = True

    def stop(self):
        self.running = False
        self.join(1)
        self.sock.close()

    def reply(self, addr, *lines):
        for line in lines:
            self.sock.sendto(line.encode(), addr)

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if self.writing is not None and data != b"M29" and data[-1:] == b"\x83":
                body, check = data[:-2], data[-2]
                expected = 0
                for byte in body:
                    expected ^= byte
                offset = struct.unpack("<I", body[-4:])[0]
                if check != expected:
                    self.bad_frames += 1
                    self.reply(addr, f"resend {offset}")
                    continue
                if self.resend_once_at is not None and offset == self.resend_once_at:
                    self.resend_once_at = None
                    self.reply(addr, f"resend {offset}")
                    continue
                chunk = body[:-4]
                self.buffer[offset:offset + len(chunk)] = chunk
                self.reply(addr, "ok")
                continue
            text = data.decode()
            self.commands.append(text)
            if text == "M4000":
                total = 1000 if self.printing else 0
                self.reply(addr, f"ok B:0/0 X:0.000 Y:0.000 Z:0.000 F:256/256 D:{250 if self.printing else 0}/{total}/0")
            elif text == "M27":
                self.reply(addr, "SD printing byte 250/1000", "ok N:0") if self.printing else self.reply(addr, "Error:It's not printing now!", "ok N:0")
            elif text == "M20":
                lines = ["Begin file list"] + [f"{name} 536976908" for name in self.files] + ["End file list", f"ok L:{len(self.files)}"]
                self.reply(addr, *lines)
            elif text.startswith("M28 "):
                self.writing = text[4:]
                self.buffer = bytearray()
                self.reply(addr, "ok N:0")
            elif text == "M29":
                if self.writing is not None:
                    self.files[self.writing] = bytes(self.buffer)
                    self.reply(addr, f"Done saving file!\r\n// {self.writing}", "ok N:0")
                    self.writing = None
                else:
                    self.reply(addr, "ok N:0")
            elif text.startswith("M30 "):
                self.files.pop(text[4:], None)
                self.reply(addr, f"File deleted :{text[4:]}", "ok N:0")
            elif text.startswith("M6030 "):
                self.printing = True
                self.reply(addr, "ok N:0")
            else:
                self.reply(addr, "ok N:0")


class ChituPrinterTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeChituBoard(files={"existing.ctb": b""})
        self.board.start()
        patcher = mock.patch.object(npr, "CHITU_PORT", self.board.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.board.stop)

    def test_frame_layout(self):
        frame = npr.chitu_frame(b"\x01\x02", 0x01020304)
        self.assertEqual(frame[:2], b"\x01\x02")
        self.assertEqual(frame[2:6], b"\x04\x03\x02\x01")  # little-endian offset
        expected = 0
        for byte in frame[:6]:
            expected ^= byte
        self.assertEqual(frame[6], expected)
        self.assertEqual(frame[7], 0x83)

    def test_upload_reassembles_file_with_resend(self):
        payload = bytes((index * 31 + 7) % 256 for index in range(5000))
        self.board.resend_once_at = 1280
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.ctb"
            path.write_bytes(payload)
            seen = []
            with npr.ChituPrinter("127.0.0.1", timeout=1.0) as printer:
                printer.upload(path, "part.ctb", lambda message, fraction: seen.append(fraction))
        self.assertEqual(self.board.files["part.ctb"], payload)
        self.assertEqual(self.board.bad_frames, 0)
        self.assertEqual(seen[-1], 1.0)

    def test_status_and_files(self):
        with npr.ChituPrinter("127.0.0.1", timeout=1.0) as printer:
            self.assertEqual(printer.status().state, "idle")
            self.assertEqual(printer.list_files(), ["existing.ctb"])
        self.board.printing = True
        with npr.ChituPrinter("127.0.0.1", timeout=1.0) as printer:
            status = printer.status()
        self.assertEqual(status.state, "printing")
        self.assertAlmostEqual(status.progress, 0.25)

    def test_send_file_picks_unused_name_and_starts(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.ctb"
            path.write_bytes(b"x" * 300)
            result = npr.send_file({"ip": "127.0.0.1", "protocol": "chitu"}, path, start=True)
        self.assertEqual(result["remoteName"], "existing-2.ctb")
        self.assertTrue(result["started"])
        self.assertEqual(self.board.files["existing.ctb"], b"")  # original untouched
        self.assertIn("M6030 ':existing-2.ctb'", self.board.commands)

    def test_send_file_refuses_goo_on_chitu(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.goo"
            path.write_bytes(b"x")
            with self.assertRaisesRegex(npr.PrinterError, "GOO.*CTB"):
                npr.send_file({"ip": "127.0.0.1", "protocol": "chitu"}, path)
        self.assertFalse(any(command.startswith("M28") for command in self.board.commands))

    def test_send_file_refuses_while_printing(self):
        self.board.printing = True
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.ctb"
            path.write_bytes(b"x")
            with self.assertRaises(npr.PrinterError):
                npr.send_file({"ip": "127.0.0.1", "protocol": "chitu"}, path)
        self.assertFalse(any(command.startswith("M28") for command in self.board.commands))


class HelperTests(unittest.TestCase):
    def test_parse_chitu_discovery(self):
        info = npr.parse_discovery_reply(
            b"ok MAC:00:e0:4c:25:00:34 IP:10.0.0.5 VER:V1.4.1 ID:34,00,25 NAME:CBD\r\n", "10.0.0.5"
        )
        self.assertEqual((info.protocol, info.ip, info.firmware, info.name), ("chitu", "10.0.0.5", "V1.4.1", "CBD"))

    def test_parse_sdcp_discovery(self):
        info = npr.parse_discovery_reply(
            b'{"Id":"x","Data":{"Name":"Mars 5 Ultra","MachineName":"Mars 5 Ultra","MainboardIP":"10.0.0.9",'
            b'"MainboardID":"16ba","ProtocolVersion":"V3.0.0","FirmwareVersion":"V1.5.0"}}',
            "10.0.0.9",
        )
        self.assertEqual((info.protocol, info.model, info.mainboard_id), ("sdcp", "Mars 5 Ultra", "16ba"))
        self.assertIsNone(npr.parse_discovery_reply(b"garbage", "1.2.3.4"))

    def test_remote_filename(self):
        self.assertEqual(
            npr.remote_filename("C:/jobs/Imaging_Well_V2_-_Power_Cover_V2.ctb", "chitu"),
            "Imaging_Well_V2_-_Power_C.ctb",
        )
        self.assertEqual(npr.remote_filename("Agar 108\u03bcm.GOO", "sdcp"), "Agar 108_m.goo")
        self.assertEqual(npr.remote_filename("///.ctb", "chitu"), "print.ctb")

    def test_unique_name_keeps_chitu_limit(self):
        name = "a" * 25 + ".ctb"
        result = npr.unique_name(name, [name], "chitu")
        self.assertEqual(result, "a" * 23 + "-2.ctb")
        self.assertEqual(npr.unique_name("x.goo", ["X.GOO", "x-2.goo"], "sdcp"), "x-3.goo")

    def test_sdcp_status_mapping(self):
        idle = npr.sdcp_status_from_json({"CurrentStatus": [0], "PrintInfo": {"Status": 0}})
        self.assertEqual(idle.state, "idle")
        printing = npr.sdcp_status_from_json(
            {"CurrentStatus": [1], "PrintInfo": {"Status": 3, "CurrentLayer": 50, "TotalLayer": 200, "Filename": "a.goo"}}
        )
        self.assertEqual((printing.state, printing.progress, printing.filename), ("printing", 0.25, "a.goo"))
        paused = npr.sdcp_status_from_json({"CurrentStatus": [1], "PrintInfo": {"Status": 6, "CurrentLayer": 1, "TotalLayer": 2}})
        self.assertEqual(paused.state, "paused")
        self.assertEqual(npr.sdcp_status_from_json({}).state, "unknown")

    def test_sdcp_wait_detects_rejected_upload(self):
        printer = npr.SdcpPrinter.__new__(npr.SdcpPrinter)
        listings = iter([["u1_part.goo"], ["u1_part.goo"], []])
        printer.list_files = lambda: next(listings, [])
        with mock.patch.object(npr.time, "sleep"):
            with self.assertRaises(npr.PrinterError):
                printer._wait_for_saved_file("u1", "part.goo", None, timeout=5)

    def test_sdcp_wait_accepts_renamed_file(self):
        printer = npr.SdcpPrinter.__new__(npr.SdcpPrinter)
        listings = iter([["u1_part.goo"], ["part.goo"]])
        printer.list_files = lambda: next(listings, ["part.goo"])
        with mock.patch.object(npr.time, "sleep"):
            printer._wait_for_saved_file("u1", "part.goo", None, timeout=5)

    def test_websocket_frames_round_trip(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        ws = npr.WebSocket.__new__(npr.WebSocket)
        ws.sock, ws._buffer = left, b""
        ws.send_text("hello " * 30)  # >125 bytes uses the 16-bit length form
        raw = right.recv(65536)
        self.assertEqual(raw[0], 0x81)
        self.assertEqual(raw[1], 0x80 | 126)
        length = struct.unpack(">H", raw[2:4])[0]
        mask, body = raw[4:8], raw[8:8 + length]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(body)).decode(), "hello " * 30)
        right.sendall(b"\x81\x05howdy")  # unmasked server frame
        self.assertEqual(ws.recv_text(1.0), "howdy")

    def test_multipart_body(self):
        body, content_type = npr._multipart({"Offset": "0"}, "File", "a.goo", b"\x00\x01")
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(b'name="Offset"\r\n\r\n0\r\n', body)
        self.assertIn(b'filename="a.goo"', body)
        self.assertTrue(body.endswith(f"\r\n--{boundary}--\r\n".encode()))


if __name__ == "__main__":
    unittest.main()
