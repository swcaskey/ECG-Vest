from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Condition
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ecg.live import LiveCapture
from ecg.recording import Playback, _rows
from test_pcb_host import simulated_epoch


class SimulatedPort:
    def __init__(self, streams):
        self.streams = iter(streams)
        self.buffer = bytearray()
        self.input_ready = Condition()
        self.commands = []

    @property
    def in_waiting(self):
        with self.input_ready:
            return len(self.buffer)

    def reset_input_buffer(self):
        with self.input_ready:
            self.buffer.clear()

    def send(self, data):
        with self.input_ready:
            self.buffer.extend(data)
            self.input_ready.notify()

    def write(self, data):
        self.commands.append(data)
        self.send(next(self.streams))
        return len(data)

    def read(self, size):
        with self.input_ready:
            self.input_ready.wait_for(lambda: self.buffer, timeout=0.02)
            size = min(size, 53)
            chunk = bytes(self.buffer[:size])
            del self.buffer[:size]
            return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class LiveCaptureTests(unittest.TestCase):
    def wait_until(self, condition):
        deadline = time.monotonic() + 4
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for the serial worker")
            time.sleep(0.01)

    def test_record_from_fresh_epoch_and_stop_at_line_boundary(self):
        first = b"".join(simulated_epoch(100, 1))
        second = b"".join(simulated_epoch(100, 2, start_us=10_000_000))
        pending_status = b"E2,S,100,2,1,0,"
        status_end = b"0,0,0\n"
        trailing = b"INFO: After recording\n"
        port = SimulatedPort(
            (first, b"E2,S,100,1,1,0,0,0,0\n" + second + pending_status)
        )
        serial = SimpleNamespace(Serial=lambda *args, **kwargs: port)
        with TemporaryDirectory() as directory, patch.dict(
            sys.modules, {"serial": serial}
        ):
            path = Path(directory) / "capture.csv"
            with LiveCapture("COM42", "usb") as live:
                self.wait_until(
                    lambda: live.raw_since()[1].endswith(first[-40:].decode())
                )
                cursor, _ = live.raw_since()
                live.start_recording(path)
                self.wait_until(
                    lambda: live.raw_since(cursor)[1].endswith(pending_status.decode())
                )
                self.assertEqual(live.recording_status()[0], "recording")
                live.stop_recording()
                port.send(status_end + trailing)
                self.wait_until(lambda: live.recording_status()[0] == "idle")
                self.assertIsNone(live.recording_status()[-1])
                self.wait_until(
                    lambda: live.raw_since(cursor)[1].endswith(trailing.decode())
                )
                self.assertEqual(port.commands, [b"go\n", b"go\n"])

            rows = _rows(path)
            next(rows)
            self.assertEqual(b"".join(rows), second + pending_status + status_end)
            with Playback(path) as playback:
                while not playback.done:
                    playback.advance(headless=True)
                self.assertTrue(playback.capture.health()["healthy"])
