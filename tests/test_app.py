from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ecg.acquisition import Acquisition
from ecg.recording import Recorder, replay
from plot_ecg import LiveAcquisition, choose_input_mode, main
from test_protocol import config


class FakePort:
    def __init__(self, wire):
        self.wire = bytearray(wire)
        self.writes = []
        self.closed = False
        self.reset = False
        self.lines = []

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value
        self.lines.append(("dtr", value))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value
        self.lines.append(("rts", value))

    @property
    def in_waiting(self):
        return len(self.wire)

    def read(self, count):
        chunk = bytes(self.wire[:count])
        del self.wire[:count]
        if not chunk:
            time.sleep(0.001)
        return chunk

    def write(self, data):
        self.writes.append(data)

    def reset_input_buffer(self):
        self.reset = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class AppTests(unittest.TestCase):
    def test_monitor_waits_for_firmware_recovery(self):
        clock = [0.0]
        engine = Acquisition()
        port = FakePort(b"")
        with patch("plot_ecg.time.monotonic", side_effect=lambda: clock[0]):
            live = LiveAcquisition(
                engine, "FAKE", "0", serial_factory=lambda *a, **k: port
            )
            chunks = iter(
                (
                    (0.0, config(mode=0)),
                    (0.5, b"E2,E,123,1,2,TIMING_GLITCH,1500000\n"),
                    (3.0, b""),
                    (3.1, config(epoch=2, mode=0)),
                )
            )

            def read(_):
                item = next(chunks, None)
                if item is None:
                    live.stop.set()
                    return b""
                clock[0], chunk = item
                return chunk

            port.read = read
            with redirect_stdout(io.StringIO()):
                live._run()
        self.assertIsNone(live.error)
        self.assertEqual(engine.key, (123, 2))
        self.assertTrue(engine.valid)
        self.assertTrue(port.closed)

    def test_monitor_prompts_and_sends_both_mode_choices(self):
        for choice in ("0", "1"):
            port = FakePort(config(mode=int(choice)))
            output = io.StringIO()
            with patch("builtins.input", return_value=choice) as prompt, patch(
                "serial.Serial", return_value=port
            ), patch("plot_ecg.monitor_serial") as monitor, patch(
                "plot_ecg.show_plot"
            ) as graph, redirect_stdout(
                output
            ):
                engine = main(["--port", "FAKE", "--monitor"])
            prompt.assert_called_once_with("Input mode [0]: ")
            monitor.assert_called_once()
            graph.assert_not_called()
            self.assertTrue(engine.ready)
            self.assertEqual(engine.mode, int(choice))
            self.assertEqual(port.writes, [(choice + "\n").encode()])
            self.assertTrue(port.closed)
            self.assertIn("E2,R,123,1," + choice, output.getvalue())

    def test_recording_prompts_and_saves_both_mode_choices(self):
        for choice in ("0", "1"):
            with tempfile.TemporaryDirectory() as folder:
                port = FakePort(config(mode=int(choice)))
                with patch(
                    "plot_ecg.__file__", str(Path(folder) / "plot_ecg.py")
                ), patch("builtins.input", return_value=choice) as prompt, patch(
                    "serial.Serial", return_value=port
                ), patch(
                    "plot_ecg.show_plot"
                ) as graph, redirect_stdout(
                    io.StringIO()
                ):
                    engine = main(["--port", "FAKE", "--record", "chosen.csv"])
                prompt.assert_called_once_with("Input mode [0]: ")
                graph.assert_called_once()
                self.assertEqual(engine.mode, int(choice))
                self.assertEqual(port.writes, [(choice + "\n").encode()])
                self.assertTrue(port.closed)
                saved = b"".join(replay(Path(folder) / "recordings" / "chosen.csv"))
                self.assertIn(("E2,R,123,1," + choice).encode(), saved)

    def test_monitor_interrupt_and_failure_release_port(self):
        for error in (KeyboardInterrupt(), RuntimeError("monitor failed")):
            port = FakePort(config(mode=0))
            with patch("builtins.input", return_value="0"), patch(
                "serial.Serial", return_value=port
            ), patch("plot_ecg.monitor_serial", side_effect=error), redirect_stdout(
                io.StringIO()
            ):
                if isinstance(error, KeyboardInterrupt):
                    main(["--port", "FAKE", "--monitor"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "monitor failed"):
                        main(["--port", "FAKE", "--monitor"])
            self.assertTrue(port.closed)

    def test_interrupt_during_startup_stops_worker(self):
        live = LiveAcquisition(
            Acquisition(), "FAKE", "0", serial_factory=lambda *a, **k: FakePort(b"")
        )
        with patch.object(live.thread, "start"), patch.object(
            live.started, "wait", side_effect=KeyboardInterrupt
        ), patch.object(live, "close") as close:
            with self.assertRaises(KeyboardInterrupt):
                live.__enter__()
            close.assert_called_once()

    def test_prompt(self):
        with redirect_stdout(io.StringIO()):
            for answers, expected in (
                ([""], "0"),
                (["1"], "1"),
                (["x", "01", "0"], "0"),
            ):
                with patch("builtins.input", side_effect=answers):
                    self.assertEqual(choose_input_mode(), expected)

    def test_startup_and_cleanup(self):
        port = FakePort(config(mode=0))
        with redirect_stdout(io.StringIO()):
            with LiveAcquisition(
                Acquisition(), "FAKE", "0", serial_factory=lambda *a, **k: port
            ) as live:
                self.assertTrue(live.engine.ready)
                self.assertTrue(port.reset)
                self.assertEqual(port.writes, [b"0\n"])
        self.assertTrue(port.closed)
        self.assertFalse(live.thread.is_alive())
        self.assertEqual(
            port.lines,
            [("dtr", False), ("rts", False)],
        )

    def test_mode_mismatch_and_protocol_failure_close_port(self):
        for wire in (config(mode=1), b"ECG:1\n"):
            port = FakePort(wire)
            with self.assertRaises((ValueError, RuntimeError)):
                with LiveAcquisition(
                    Acquisition(), "FAKE", "0", serial_factory=lambda *a, **k: port
                ):
                    self.fail("Should not start with wrong mode/version")
            self.assertTrue(port.closed)

    def test_record_replay_lossless_deterministic(self):
        chunks = [
            config()[:9],
            config()[9:],
            b"E2,D,123,1,1,0,-131072:0;0:1;131071:2\n",
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.csv"
            with Recorder(path, {"input": "external"}) as recorder:
                for chunk in chunks:
                    recorder.write(chunk)
            self.assertEqual(list(replay(path)), chunks)
            outputs = []
            for _ in range(2):
                capture = io.StringIO()
                with redirect_stdout(capture):
                    result = main(["--replay", str(path), "--headless"])
                outputs.append(json.loads(capture.getvalue()))
                self.assertEqual(result.sample_count, [3, 0])
            self.assertEqual(outputs[0], outputs[1])
            with self.assertRaises(FileExistsError):
                Recorder(path)

    def test_recording_close_finishes_current_message_only(self):
        prefix = config(mode=0) + b"E2,D,123,1,1,0,-123:0;"
        tail = b"456:2\n"
        following = b"E2,D,123,1,1,2,789:2\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "close.csv"
            port = FakePort(tail + following)
            with Recorder(path) as recorder:
                live = LiveAcquisition(
                    Acquisition(),
                    "FAKE",
                    "0",
                    recorder,
                    serial_factory=lambda *a, **k: port,
                )
                read_tail = port.read

                def read(count):
                    if not live.stop.is_set():
                        live.stop.set()
                        return prefix
                    return read_tail(count)

                port.read = read
                with redirect_stdout(io.StringIO()):
                    live._run()
            self.assertIsNone(live.error)
            self.assertEqual(b"".join(replay(path)), prefix + tail)
            self.assertEqual(bytes(port.wire), following)
            self.assertEqual(live.engine.sample_count, [2, 0])
            self.assertFalse(live.engine.parser.pending)

    def test_recording_close_reports_incomplete_message(self):
        port = FakePort(b"")
        with tempfile.TemporaryDirectory() as folder:
            with Recorder(Path(folder) / "incomplete.csv") as recorder:
                live = LiveAcquisition(
                    Acquisition(),
                    "FAKE",
                    "0",
                    recorder,
                    serial_factory=lambda *a, **k: port,
                )
                empty_read = port.read

                def read(count):
                    if not live.stop.is_set():
                        live.stop.set()
                        return config(mode=0) + b"E"
                    return empty_read(count)

                port.read = read
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                    TimeoutError, "incomplete serial message"
                ):
                    with live:
                        pass
            self.assertTrue(port.closed)
            self.assertFalse(live.thread.is_alive())

    def test_replay_malformed_and_truncated(self):
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (b"broken\n", b"E2,D,123,1,1,0,1:"):
                path = Path(folder) / (str(len(suffix)) + ".csv")
                with Recorder(path) as recorder:
                    recorder.write(config() + suffix)
                with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    main(["--headless", "--replay", str(path)])

    def test_no_serial_required_for_replay_and_gui_cleanup(self):
        with patch("plot_ecg.show_plot", side_effect=RuntimeError("GUI closed")), patch(
            "plot_ecg.choose_input_mode", return_value="0"
        ):
            port = FakePort(config(mode=0))
            with patch("serial.Serial", return_value=port), redirect_stdout(
                io.StringIO()
            ):
                with self.assertRaisesRegex(RuntimeError, "GUI closed"):
                    main(["--port", "FAKE"])
            self.assertTrue(port.closed)
