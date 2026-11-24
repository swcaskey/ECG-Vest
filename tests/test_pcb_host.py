import csv
from math import isnan
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ecg.acquisition import Capture
from ecg.protocol import parse_record
from ecg.recording import Playback, Recorder
from ecg.timing import SAMPLE_US
from plot_ecg import recording_path


def simulated_epoch(session, epoch, start_us=1_000_000):
    key = f"{session},{epoch}"
    for afe in range(1, 4):
        yield f"E2,C,{key},{afe},1,513742,{start_us},1,4906,1\n".encode()
    yield f"E2,R,{key},1\n".encode()
    for first in range(0, 800, 8):
        tick = start_us + round((first + 8 + 18) * SAMPLE_US)
        for afe in range(1, 4):
            value = afe * (-100 if afe % 2 else 100)
            samples = ";".join(
                f"{value}:{1 if index == 300 else 0}"
                for index in range(first, first + 8)
            )
            yield f"E2,D,{key},{afe},{first},{samples}\n".encode()
            yield f"E2,A,{key},{afe},{first + 8},{tick + afe * 30}\n".encode()


def fragmented(wire, size=39):
    for start in range(0, len(wire), size):
        yield wire[start : start + size]


class AcquisitionTests(unittest.TestCase):
    def test_fragmented_three_channel_capture_preserves_signs_and_settling_gaps(self):
        capture = Capture()
        for chunk in fragmented(b"".join(simulated_epoch(100, 1))):
            capture.feed(chunk, 1.0)
        x, traces, status = capture.snapshot(1.0)
        self.assertEqual(len(x), 641)
        self.assertEqual(status, "")
        self.assertEqual([trace[-1] for trace in traces], [-100, 200, -300])
        self.assertTrue(all(any(isnan(value) for value in trace) for trace in traces))
        self.assertEqual(capture.health()["samples"], {"1": 800, "2": 800, "3": 800})
        self.assertTrue(capture.health()["healthy"])
        self.assertEqual(capture.snapshot(2.1)[:2], ((), ()))

    def test_missing_batch_stops_display_until_new_epoch(self):
        capture = Capture()
        capture.feed(b"".join(simulated_epoch(100, 1)), 1.0)
        capture.feed(b"E2,D,100,1,2,808,200:0\n", 1.1)
        self.assertEqual(capture.snapshot(1.1), ((), (), "AFE 2: sample sequence gap"))
        capture.feed(b"".join(simulated_epoch(100, 2)), 2.0)
        self.assertTrue(capture.snapshot(2.0)[0])
        self.assertFalse(capture.health()["healthy"])
        self.assertIn("AFE 2: sample sequence gap (1)", capture.health()["issues"])
        self.assertEqual(capture.health()["epochs"], 2)

    def test_ble_loss_clears_plot_and_fresh_session_can_resume(self):
        capture = Capture("ble")
        capture.feed(b"".join(simulated_epoch(100, 1)), 1.0)
        capture.feed(b"INFO: BLE_LOST sequence gap\n", 2.0)
        self.assertIn("Reconnecting", capture.snapshot(2.0)[2])
        self.assertFalse(capture.ready)
        capture.feed(b"INFO: BLE_READY\n", 3.0)
        self.assertIn("BLE connected", capture.snapshot(3.0)[2])
        capture.feed(b"".join(simulated_epoch(200, 1)), 4.0)
        self.assertTrue(capture.snapshot(4.0)[0])
        self.assertEqual(capture.key, (200, 1))
        self.assertIn("BLE link lost (sequence gap) (1)", capture.health()["issues"])

    def test_sample_range_and_tags(self):
        parsed = parse_record("E2,D,100,1,3,0,-131072:0;131071:2;0:3;0:1")
        self.assertEqual(parsed[3][1], [(-131072, 0), (131071, 2), (0, 3), (0, 1)])
        for item in ("131072:0", "-131073:0", "0:4"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_record(f"E2,D,100,1,3,0,{item}")


class RecordingTests(unittest.TestCase):
    def test_wire_bytes_round_trip_and_replay_are_independent_of_chunks(self):
        wire = b"".join(simulated_epoch(100, 1))
        with TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.csv"
            for size in (39, 1103):
                path = first_path if size == 39 else Path(directory) / "second.csv"
                with Recorder(path, "ble") as recorder:
                    for chunk in fragmented(wire, size):
                        recorder.write(chunk)
                with path.open(newline="", encoding="utf8") as stream:
                    reader = csv.reader(stream)
                    next(reader)
                    next(reader)
                    self.assertEqual(
                        b"".join(bytes.fromhex(row[1]) for row in reader), wire
                    )
                with Playback(path) as playback:
                    while not playback.done:
                        playback.advance(headless=True)
                    self.assertTrue(playback.capture.health()["healthy"])
                    self.assertEqual(playback.capture.transport, "ble")
                    timeline = playback.snapshot()[0]
                    if size == 39:
                        first_timeline = timeline
                    else:
                        self.assertEqual(timeline, first_timeline)
            with self.assertRaises(FileExistsError):
                Recorder(first_path, "ble")

    def test_legacy_and_truncated_recordings_are_not_healthy(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.csv"
            with path.open("w", newline="", encoding="utf8") as stream:
                writer = csv.writer(stream)
                writer.writerow(("index", "wire_hex", "text"))
                writer.writerow((0, "", '{"format":"ecg-e2-csv-1"}'))
            with self.assertRaisesRegex(ValueError, "Unsupported recording format"):
                Playback(path)
            path = Path(directory) / "truncated.csv"
            with Recorder(path, "usb") as recorder:
                recorder.write(b"".join(simulated_epoch(100, 1)) + b"E2,D,100,1")
            with Playback(path) as playback:
                while not playback.done:
                    playback.advance(headless=True)
                health = playback.capture.health()
                self.assertFalse(health["healthy"])
                self.assertIn("Incomplete final serial message", health["issues"])

    def test_recording_names_stay_in_ignored_directory(self):
        self.assertEqual(recording_path("bench.csv").parent.name, "recordings")
        with self.assertRaises(ValueError):
            recording_path("../bench.csv")


if __name__ == "__main__":
    unittest.main()
