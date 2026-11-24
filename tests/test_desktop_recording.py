import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ecg.desktop_recording import DesktopPlayback, export_samples, list_recordings
from ecg.recording import Recorder
from ecg.timing import SAMPLE_US


class DesktopRecordingTests(unittest.TestCase):
    def test_browse_and_seek_capture_without_replaying_from_start(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            path = directory / "capture.csv"
            (directory / "unrelated.csv").write_text("x,y,z\n")
            with Recorder(path, "ble") as recorder:
                for afe in range(1, 4):
                    recorder.write(
                        f"E2,C,7,1,{afe},1,513742,1000000,1,4906,1\n".encode()
                    )
                recorder.write(b"E2,R,7,1,1\n")
                for first in range(0, 2560, 8):
                    stamp = 1_000_000 + round((first + 26) * SAMPLE_US)
                    for afe in range(1, 4):
                        readings = ";".join([f"{afe * 100}:0"] * 8)
                        recorder.write(
                            (
                                f"E2,D,7,1,{afe},{first},{readings}\n"
                                f"E2,A,7,1,{afe},{first + 8},{stamp}\n"
                            ).encode()
                        )
            infos = list_recordings(directory)
            self.assertEqual([entry.name for entry in infos], ["capture.csv"])
            self.assertEqual(infos[0].transport, "ble")
            self.assertAlmostEqual(infos[0].duration, 20.14, places=2)

            now = [0.0]
            with DesktopPlayback(path, clock=lambda: now[0]) as replay:
                replay.seek(17)
                for _ in range(8):
                    replay.advance()
                self.assertFalse(replay.seeking)
                self.assertAlmostEqual(replay.position, 17)
                self.assertLess(replay.capture.total_samples[1], 17 * 128)
                traces = replay.snapshot()[1]
                self.assertEqual([lead[-1] for lead in traces], [100, 200, 300])
                self.assertIn("E2,D,7,1,1,", replay.raw_text)
                replay.speed = 2
                replay.play()
                now[0] += 1
                replay.advance()
                self.assertAlmostEqual(replay.position, 19)
                replay.pause()
                now[0] += 1
                replay.advance()
                self.assertAlmostEqual(replay.position, 19)
                replay.seek(3)
                replay.advance()
                self.assertAlmostEqual(replay.position, 3)

    def test_export_preserves_epochs_settling_and_missing_channels(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "capture.csv"
            destination = Path(directory) / "samples.csv"
            with Recorder(source, "usb") as recorder:
                recorder.write(b"E2,D,42,1,1,0,-15:0;19:1\nE2,D,42,1,")
                recorder.write(b"2,1,20:2\nE2,D,42,2,3,0,-130:0\n")
            self.assertEqual(export_samples(source, destination), 3)
            with destination.open(newline="", encoding="utf8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(
                rows[0][3:],
                [
                    "Lead I (ADC counts)",
                    "Lead II (ADC counts)",
                    "Lead III (ADC counts)",
                ],
            )
            self.assertEqual(
                rows[1:],
                [
                    ["42", "1", "0", "-15", "", ""],
                    ["42", "1", "1", "", "20", ""],
                    ["42", "2", "0", "", "", "-130"],
                ],
            )
            with self.assertRaisesRegex(ValueError, "different file"):
                export_samples(source, source)
