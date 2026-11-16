from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ecg.acquisition import Acquisition
from ecg.protocol import MAX_LINE
from ecg.recording import (
    Playback,
    Recorder,
    recording_health,
    recording_options,
    replay,
)
from plot_ecg import LiveAcquisition, choose_input_mode, main, show_plot
from test_protocol import config


def healthy_wire(mode=1, epoch=1, board_values=None, count=256):
    wire = config(mode=mode, epoch=epoch)
    for first in range(0, count, 8):
        for board in (1, 2):
            pulse = first * 4 + 1
            stamp = 1_000_000 + round(pulse * 1e6 / 512)
            wire += f"E2,A,123,{epoch},{board},{pulse},{stamp}\n".encode()
            values = ";".join(
                f"{board_values[board] if board_values else n-128}:0"
                for n in range(first, first + 8)
            )
            wire += f"E2,D,123,{epoch},{board},{first},{values}\n".encode()
    return wire


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
    def test_auto_display_range_includes_visible_sum_and_preserves_fixed_override(self):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        engine = Acquisition(True, lead_i_board=2)
        wire = healthy_wire(board_values={1: 50000, 2: 50000}, count=1024)
        wire = wire.replace(b",0,50000:0;", b",0,131071:0;")
        wire = wire.replace(b",640,50000:0;", b",640,50000:1;")
        engine.feed(wire)
        for amplitude in (None, 8000):

            def inspect():
                animation.call_args.args[1](None)
                figure = plt.gcf()
                limits = [axis.get_ylim() for axis in figure.axes]
                self.assertTrue(all(pair == limits[0] for pair in limits))
                if amplitude is None:
                    self.assertGreaterEqual(limits[0][1], 110000)
                    self.assertLess(limits[0][1], 150000)
                    self.assertNotIn("CLIPPING", figure.texts[0].get_text())
                else:
                    self.assertEqual(limits[0], (-8000, 8000))
                    self.assertIn("CLIPPING", figure.texts[0].get_text())
                raw = figure.axes[0].lines[0].get_ydata()
                self.assertTrue(any(math.isnan(y) for y in raw))
                self.assertIn(
                    131071, raw
                )  # Out-of-view data is not rescaled or discarded.
                self.assertIn(50000, raw)
                self.assertIn(100000, figure.axes[1].lines[0].get_ydata())
                plt.close(figure)

            with patch("matplotlib.animation.FuncAnimation") as animation, patch(
                "matplotlib.pyplot.show", side_effect=inspect
            ):
                show_plot(engine, amplitude)

    def test_new_recording_saves_current_physical_lead_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            port = FakePort(healthy_wire(mode=1))
            with patch("plot_ecg.__file__", str(Path(folder) / "plot_ecg.py")), patch(
                "builtins.input", return_value="1"
            ), patch("serial.Serial", return_value=port), patch(
                "plot_ecg.show_plot"
            ), redirect_stdout(
                io.StringIO()
            ):
                engine = main(["--port", "FAKE", "--record", "mapped.csv"])
            path = Path(folder) / "recordings" / "mapped.csv"
            options = recording_options(path)
            self.assertEqual(options["lead_i_board"], 2)
            self.assertEqual(options["bias_board"], 2)
            self.assertEqual(engine.lead_i_board, 2)
            self.assertEqual(engine.lead_iii_board, 1)
            with redirect_stdout(io.StringIO()):
                replayed = main(["--headless", "--replay", str(path)])
            self.assertEqual(replayed.lead_i_board, 2)
            self.assertEqual(replayed.bias_board, 2)

    def test_replay_mapping_provenance_and_unknown_legacy_recordings(self):
        for options, expected, bias in (
            ({}, None, None),
            ({"lead_i_board": 2, "bias_board": 2}, 2, 2),
            ({"lead_i_board": 1, "bias_board": 1}, 1, 1),
            ({"diagnostic_plug_swap": True}, 2, None),
            ({"diagnostic_plug_swap": False}, 1, None),
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "mapping.csv"
                wire = healthy_wire()
                with Recorder(path, options) as recorder:
                    recorder.write(wire)
                original = path.read_bytes()
                with redirect_stdout(io.StringIO()):
                    engine = main(["--headless", "--replay", str(path)])
                self.assertEqual(engine.lead_i_board, expected)
                self.assertEqual(engine.bias_board, bias)
                self.assertEqual(b"".join(replay(path)), wire)
                self.assertEqual(path.read_bytes(), original)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "override.csv"
            with Recorder(path, {"lead_i_board": 1}) as recorder:
                recorder.write(healthy_wire())
            output = io.StringIO()
            with patch("plot_ecg.show_plot"), redirect_stdout(output):
                engine = main(["--replay", str(path), "--lead-i-board", "2"])
            self.assertEqual(engine.lead_i_board, 2)
            self.assertIn("explicit lead mapping override", output.getvalue())

    def test_invalid_recorded_lead_mapping_is_rejected(self):
        for options in (
            {"lead_i_board": True},
            {"diagnostic_plug_swap": 2},
            {"bias_board": "2"},
        ):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad-mapping.csv"
                with Recorder(path, options) as recorder:
                    recorder.write(healthy_wire())
                with self.assertRaises(ValueError):
                    main(["--headless", "--replay", str(path)])

    def test_distinct_signals_use_mapped_or_neutral_pane_labels(self):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        for lead_i_board in (1, 2, None):
            engine = Acquisition(True, lead_i_board=lead_i_board)
            engine.feed(healthy_wire(board_values={1: 111, 2: 222}))

            def inspect():
                animation.call_args.args[1](None)
                figure = plt.gcf()
                first_board = lead_i_board or 1
                self.assertTrue(
                    all(
                        y == first_board * 111
                        for y in figure.axes[0].lines[0].get_ydata()
                    )
                )
                self.assertTrue(
                    all(
                        y == (3 - first_board) * 111
                        for y in figure.axes[2].lines[0].get_ydata()
                    )
                )
                self.assertTrue(
                    all(
                        abs(y - 333) < 1e-8 for y in figure.axes[1].lines[0].get_ydata()
                    )
                )
                if lead_i_board:
                    self.assertIn(
                        "Lead I - measured, Board %d" % first_board,
                        figure.axes[0].get_title(),
                    )
                else:
                    self.assertNotIn("Lead", figure.axes[0].get_title())
                    self.assertIn("mapping not recorded", figure.axes[0].get_title())
                plt.close(figure)

            with patch("matplotlib.animation.FuncAnimation") as animation, patch(
                "matplotlib.pyplot.show", side_effect=inspect
            ):
                show_plot(engine, 8000)

    def test_timing_fault_in_startup_chunk_still_opens_measured_plot(self):
        for event in ("TIMING_GLITCH_D181_P7813_R7812", "IRQ_QUEUE_OVERFLOW"):
            wire = (
                config(mode=0)
                + f"E2,E,123,1,1,{event},1100000\n".encode()
                + b"E2,S,123,1,1,000000,20,1,0\n"
                + b"E2,D,123,1,1,0,100:0;101:2\nE2,D,123,1,2,0,-100:0;-101:2\n"
            )
            port = FakePort(wire)
            with patch("builtins.input", return_value="0"), patch(
                "serial.Serial", return_value=port
            ), patch("plot_ecg.show_plot") as graph, redirect_stdout(io.StringIO()):
                engine = main(["--port", "FAKE"])
            graph.assert_called_once()
            self.assertTrue(port.closed)
            self.assertTrue(engine.raw_valid)
            self.assertFalse(engine.valid)
            self.assertEqual(engine.sample_count, [2, 2])
            self.assertEqual(engine.snapshot()[1], ([], []))
            self.assertFalse(engine.health()["healthy"])

    def test_startup_does_not_confirm_an_already_faulted_epoch(self):
        port = FakePort(b"")
        live = LiveAcquisition(
            Acquisition(), "FAKE", "0", serial_factory=lambda *a, **k: port
        )
        chunks = iter(
            (
                config(mode=0) + b"E2,E,123,1,1,FIFO_OVERFLOW,1100000\n",
                config(mode=0, epoch=2),
            )
        )

        def read(_):
            chunk = next(chunks, None)
            if chunk is None:
                live.stop.set()
                return b""
            self.assertFalse(live.started.is_set())
            return chunk

        port.read = read
        with redirect_stdout(io.StringIO()):
            live._run()
        self.assertIsNone(live.error)
        self.assertTrue(live.started.is_set())
        self.assertEqual(live.engine.key, (123, 2))

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
        def finish_plot(engine, *args, **kwargs):
            deadline = time.monotonic() + 2
            while engine.sample_count != [256, 256] and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(engine.sample_count, [256, 256])

        for choice in ("0", "1"):
            with tempfile.TemporaryDirectory() as folder:
                port = FakePort(healthy_wire(mode=int(choice)))
                with patch(
                    "plot_ecg.__file__", str(Path(folder) / "plot_ecg.py")
                ), patch("builtins.input", return_value=choice) as prompt, patch(
                    "serial.Serial", return_value=port
                ), patch(
                    "plot_ecg.show_plot", side_effect=finish_plot
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
        wire = healthy_wire()
        chunks = [wire[:9], wire[9:40], wire[40:]]
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
                self.assertEqual(result.sample_count, [256, 256])
                self.assertTrue(result.health()["healthy"])
            self.assertEqual(outputs[0], outputs[1])
            with self.assertRaises(FileExistsError):
                Recorder(path)

    def test_headless_rejects_faults_even_after_recovery(self):
        for suffix in (
            b"E2,E,123,1,1,FIFO_OVERFLOW,3100000\n",
            b"E2,D,123,1,1,264,1:2\n",
            b"E2,S,123,1,1,000000,1024,1,0\n",
        ):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "fault.csv"
                with Recorder(path) as recorder:
                    recorder.write(healthy_wire() + suffix + healthy_wire(epoch=2))
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                    RuntimeError, "Acquisition checks failed"
                ):
                    main(["--headless", "--replay", str(path)])

    def test_headless_rejects_empty_and_one_channel_recordings(self):
        for wire in (b"", config(), config() + b"E2,D,123,1,1,0,1:2\n"):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "empty.csv"
                with Recorder(path) as recorder:
                    recorder.write(wire)
                with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    main(["--headless", "--replay", str(path)])

    def test_playback_pacing_is_independent_of_wire_fragmentation(self):
        wire = healthy_wire(mode=0)
        snapshots = []
        for size in (1, 7, 113, len(wire)):
            clock = [0.0]
            engine = Acquisition()
            player = Playback(
                engine,
                (wire[i : i + size] for i in range(0, len(wire), size)),
                clock=lambda: clock[0],
            )
            progress = []
            for now in (0.0, 0.25, 0.75, 1.25, 2.1):
                clock[0] = now
                player.advance()
                progress.append(tuple(engine.sample_count))
            self.assertTrue(player.done)
            self.assertIn("checks passed", player.result())
            self.assertIn("UNVALIDATED", player.result())
            snapshots.append(progress)
        self.assertTrue(all(item == snapshots[0] for item in snapshots))
        self.assertLess(snapshots[0][1][0], snapshots[0][-1][0])

    def test_playback_reports_truncated_tail_and_invalid_csv(self):
        engine = Acquisition()
        player = Playback(engine, [healthy_wire() + b"E"], clock=lambda: 10.0)
        player.advance()
        player.started = 0.0
        player.advance()
        self.assertTrue(player.done)
        self.assertIn("Incomplete final serial message", player.result())
        self.assertFalse(recording_health(engine)["healthy"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.csv"
            path.write_text("index,wire_hex,text\n0,,not-json\n", encoding="utf8")
            with self.assertRaises(ValueError):
                list(replay(path))

    def test_invalid_frame_timestamps_cannot_stall_replay(self):
        anchor = b"E2,A,123,1,1,1,999999999999999"
        for bad in (
            anchor,
            anchor + b" " * (MAX_LINE + 1 - len(anchor)) + b"\n",
            b"x" * (MAX_LINE + 1) + anchor + b"\n",
        ):
            with self.subTest(bad=bad[:40]):
                engine = Acquisition()
                player = Playback(engine, [config() + bad], clock=lambda: 0)
                player.advance()
                self.assertTrue(player.done)
                self.assertIn("FAILED", player.result())

    def test_three_pane_display_modes_and_timing_labels(self):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        for mode, associated, exploratory in (
            (0, False, False),
            (1, False, True),
            (1, True, False),
        ):
            engine = Acquisition(allow_unvalidated=exploratory)
            wire = healthy_wire(mode)
            if associated:
                wire = wire.replace(b",4906,0\n", b",4906,1\n")
            engine.feed(wire)

            def inspect():
                update = animation.call_args.args[1]
                update(None)
                figure = plt.gcf()
                figure.canvas.draw()
                self.assertEqual(len(figure.axes), 3)
                self.assertGreater(len(figure.axes[0].lines[0].get_ydata()), 0)
                self.assertGreater(len(figure.axes[2].lines[0].get_ydata()), 0)
                has_sum = len(figure.axes[1].lines[0].get_ydata()) > 0
                self.assertEqual(has_sum, associated or exploratory)
                self.assertEqual(
                    "UNVALIDATED" in figure.texts[0].get_text(), not associated
                )
                plt.close(figure)

            with patch("matplotlib.animation.FuncAnimation") as animation, patch(
                "matplotlib.pyplot.show", side_effect=inspect
            ):
                show_plot(engine, 8000)

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
