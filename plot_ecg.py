"""USB dual-board viewer, lossless CSV recording, and deterministic replay."""

import argparse
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

from ecg.acquisition import Acquisition
from ecg.recording import (
    Playback,
    Recorder,
    recorded_lead_i_board,
    recording_health,
    recording_options,
    replay,
)


def choose_input_mode():
    print("0 = Internal test signal (both boards)")
    print("1 = Electrodes / external inputs (both boards)")
    while True:
        choice = input("Input mode [0]: ").strip() or "0"
        if choice in ("0", "1"):
            return choice
        print("Enter 0 or 1.")


class LiveAcquisition:
    """One serial owner; acquisition continues independently of GUI redraws."""

    def __init__(
        self,
        engine,
        port_name,
        choice,
        recorder=None,
        serial_factory=None,
        echo_serial=False,
    ):
        if serial_factory is None:
            import serial

            serial_factory = serial.Serial
        self.engine, self.port_name, self.choice = engine, port_name, choice
        self.serial_factory, self.recorder = serial_factory, recorder
        self.echo_serial = echo_serial
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = threading.Event()
        self.error = None
        self.last_receive = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True, name="ECG serial")

    def _run(self):
        try:
            with self.serial_factory(
                self.port_name, 115200, timeout=0.1, write_timeout=1
            ) as port:
                try:
                    time.sleep(0.05)
                    port.reset_input_buffer()
                    self._read_stream(port)
                finally:
                    # Lower both lines in this order to clear Nano USB CDC state.
                    try:
                        port.dtr = False
                        port.rts = False
                    except OSError:
                        pass  # Physical unplug may already have removed the port.
        except Exception as exc:
            self.error = exc
            with self.lock:
                self.engine.invalidate(str(exc))
            self.started.set()

    def _read_stream(self, port):
        port.write((self.choice + "\n").encode("ascii"))
        deadline = time.monotonic() + 10
        while not self.stop.is_set():
            chunk = port.read(min(max(port.in_waiting, 1), 4096))
            if chunk:
                if self.recorder:
                    self.recorder.write(chunk)
                if self.echo_serial:
                    sys.stdout.write(chunk.decode("ascii", errors="replace"))
                    sys.stdout.flush()
                self.last_receive = time.monotonic()
                with self.lock:
                    records = self.engine.feed(chunk)
                    for record in records:
                        if record.kind == "E" and not self.echo_serial:
                            print("Board %d: %s" % (record.board, record.fields[0]))
                    if self.engine.ready and self.engine.raw_valid:
                        if (
                            self.engine.mode != int(self.choice)
                            and not self.started.is_set()
                        ):
                            raise RuntimeError(
                                "Board mode confirmation does not match selection"
                            )
                        self.started.set()
            if not self.started.is_set() and time.monotonic() > deadline:
                raise TimeoutError(
                    "No matching E2 confirmation from BOTH boards. Check wiring, close other serial apps, and upload the two-board firmware."
                )
            if self.started.is_set() and time.monotonic() - self.last_receive > (
                5 if not self.engine.valid else 2
            ):
                raise TimeoutError(
                    "Serial stream stopped. Reconnect the Nano and reopen Live Plot."
                )
        if self.recorder:
            # Finish only the current wire line; never leave a normal stop truncated.
            deadline = time.monotonic() + 0.25
            while self.engine.parser.pending and time.monotonic() < deadline:
                chunk = port.read(1)
                if chunk:
                    self.recorder.write(chunk)
                    with self.lock:
                        self.engine.feed(chunk)
            if self.engine.parser.pending:
                raise TimeoutError(
                    "Recording stopped inside an incomplete serial message"
                )

    def __enter__(self):
        self.thread.start()
        try:
            if not self.started.wait(12):
                raise TimeoutError("Serial startup timed out")
            if self.error:
                raise self.error
        except BaseException:
            self.close()
            raise
        print(
            "Both boards confirmed; input="
            + ("internal test" if self.choice == "0" else "external")
        )
        return self

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise TimeoutError(
                    "Serial worker did not stop; close this task before reopening the port"
                )

    def __exit__(self, error_type, *_):
        self.close()
        if error_type is None and self.error:
            raise self.error


def monitor_serial(live):
    print("Serial Monitor running. Press Ctrl+C to close; reopen to change modes.")
    while not live.stop.wait(0.1):
        if live.error:
            raise live.error


def show_plot(engine, amplitude, live=None, replay_chunks=None):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    playback = Playback(engine, replay_chunks) if replay_chunks is not None else None
    reported = [False]
    display_limit = [8000 if amplitude is None else amplitude]
    figure, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 8))
    figure.canvas.manager.set_window_title("ECG Vest")
    measured_boards = (engine.lead_i_board or 1, engine.lead_iii_board or 2)
    titles = (
        (
            "Lead I - measured, Board %d" % measured_boards[0],
            "Lead II - estimated from I + III",
            "Lead III - measured, Board %d" % measured_boards[1],
        )
        if engine.lead_i_board is not None
        else (
            "Board 1 - measured; lead mapping not recorded",
            "Board 1 + Board 2 sum - lead mapping not recorded",
            "Board 2 - measured; lead mapping not recorded",
        )
    )
    lines = []
    for axis, title in zip(axes, titles):
        (line,) = axis.plot([], [], linewidth=1)
        lines.append(line)
        axis.set(
            title=title,
            ylabel="ADC counts",
            ylim=(-display_limit[0], display_limit[0]),
            xlim=(-5, 0),
        )
        axis.grid(True)
    axes[-1].set_xlabel("Estimated common time relative to newest sample (s)")
    derived_note = axes[1].text(
        0.5,
        0.85,
        "Collecting timing",
        ha="center",
        transform=axes[1].transAxes,
        fontsize=9,
    )
    quality = figure.text(0.02, 0.015, "", fontsize=8)
    figure.tight_layout(rect=(0, 0.09, 1, 0.97))
    last_print = [0.0]

    def update(_):
        if playback:
            playback.advance()
            if playback.done and not reported[0]:
                print(playback.result())
                print(json.dumps(engine.diagnostics()))
                reported[0] = True
        with live.lock if live else nullcontext():
            measured, derived, status = engine.snapshot()
            diagnostics = engine.diagnostics()
            health = engine.health()
        for board, line in zip(measured_boards, (lines[0], lines[2])):
            line.set_data(*measured.get(board, ([], [])))
        lines[1].set_data(*derived)
        derived_note.set_text(status)
        visible_peak = max(
            (
                abs(y)
                for xs, ys in [*measured.values(), derived]
                for x, y in zip(xs, ys)
                if -5 <= x <= 0 and math.isfinite(y)
            ),
            default=0,
        )
        if amplitude is None:
            target = max(8000, visible_peak * 1.15)
            step = 10 ** max(3, math.floor(math.log10(target)) - 1)
            limit = math.ceil(target / step) * step
            # Round limits and shrink only after a substantial change.
            if limit > display_limit[0] or limit < display_limit[0] * 0.75:
                display_limit[0] = limit
                for axis in axes:
                    axis.set_ylim(-limit, limit)
        clipped = amplitude is not None and visible_peak > amplitude
        timing = diagnostics["timing"]
        parts = [
            (
                "Waiting for input mode"
                if engine.mode is None
                else ("Internal test" if engine.mode == 0 else "External inputs")
            ),
            (
                "FIFO time association established"
                if health["timing_validated"]
                else "FIFO time association UNVALIDATED"
            ),
        ]
        for board, values in timing.items():
            if values["ready"]:
                parts.append(
                    "B%s candidate %.4f sps; residual %.1f us"
                    % (board, values["candidate_fs"], values["residual_us"])
                )
            else:
                parts.append(
                    (
                        "B%s timing unavailable"
                        if not engine.valid
                        else "B%s: collecting timing anchors"
                    )
                    % board
                )
        if "relative_ppm" in diagnostics:
            parts.append("III vs I: %+.1f ppm" % diagnostics["relative_ppm"])
        elif "board2_vs_board1_ppm" in diagnostics:
            parts.append("B2 vs B1: %+.1f ppm" % diagnostics["board2_vs_board1_ppm"])
        parts.append("parser errors %d" % diagnostics["parser_errors"])
        parts.append("acquisition faults %d" % health["fault_count"])
        if playback and playback.done:
            passed = not playback.error and recording_health(engine)["healthy"]
            parts.append(
                "Replay complete: "
                + ("acquisition checks passed" if passed else "FAILED; see terminal")
            )
        if clipped:
            parts.append("CLIPPING: increase --range")
        if live and live.error:
            parts.append("STOPPED: " + str(live.error))
            lines[1].set_data([], [])
        quality.set_text(" | ".join(parts[:4]) + "\n" + " | ".join(parts[4:]))
        if live and time.monotonic() - last_print[0] >= 5:
            print(json.dumps(diagnostics))
            last_print[0] = time.monotonic()
        return (*lines, derived_note, quality)

    animation = FuncAnimation(figure, update, interval=100, cache_frame_data=False)
    plt.show()
    if playback:
        if not playback.done:
            print("Replay closed before the end; validation incomplete.")
        elif playback.error or not recording_health(engine)["healthy"]:
            raise RuntimeError(playback.result())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=os.environ.get("ECG_PORT"))
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Show serial data in the terminal instead of a graph",
    )
    parser.add_argument("--record", metavar="CSV")
    parser.add_argument("--replay", metavar="CSV")
    parser.add_argument(
        "--lead-i-board",
        type=int,
        choices=(1, 2),
        help="Physical board carrying Lead I; live default is 2, replay uses saved mapping",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Check the complete recording and acquisition health without GUI or hardware",
    )
    parser.add_argument(
        "--range",
        type=float,
        default=None,
        dest="amplitude",
        help="Fixed vertical limit in ADC counts; omitted uses automatic scaling",
    )
    parser.add_argument(
        "--allow-unvalidated-timing",
        action="store_true",
        help="Show explicitly UNVALIDATED candidate sum; not a synchronization claim",
    )
    parser.add_argument(
        "--samp-ratio",
        type=int,
        choices=(1, 2, 4),
        help="Override the anchor/FIFO position ratio advertised by the firmware",
    )
    args = parser.parse_args(argv)
    if args.amplitude is not None and (
        not math.isfinite(args.amplitude) or args.amplitude <= 0
    ):
        parser.error("--range must be positive and finite")
    if args.record and args.replay:
        parser.error("Choose recording or replay")
    if args.monitor and args.replay:
        parser.error("Choose serial monitoring or replay")
    if args.headless and not args.replay:
        parser.error("--headless requires --replay")
    if args.replay:
        saved_options = recording_options(args.replay)
        saved_lead = recorded_lead_i_board(saved_options)
        selected_lead = (
            args.lead_i_board if args.lead_i_board is not None else saved_lead
        )
        bias_board = saved_options.get("bias_board")
        if not args.headless:
            if args.lead_i_board is not None and args.lead_i_board != saved_lead:
                print(
                    "Using explicit lead mapping override; recorded wire data is unchanged."
                )
            elif selected_lead is None:
                print("Lead mapping not recorded; displaying physical board labels.")
    else:
        selected_lead = args.lead_i_board if args.lead_i_board is not None else 2
        bias_board = 2
        args.lead_i_board = selected_lead
        args.bias_board = bias_board
    engine = Acquisition(
        args.allow_unvalidated_timing,
        args.samp_ratio,
        lead_i_board=selected_lead,
        bias_board=bias_board,
    )
    if args.replay:
        chunks = replay(args.replay)
        if args.headless:
            for chunk in chunks:
                engine.feed(chunk)
            health = recording_health(engine)
            print(
                json.dumps({**engine.diagnostics(), "health": health}, sort_keys=True)
            )
            if not health["healthy"]:
                raise RuntimeError(
                    "Acquisition checks failed: " + "; ".join(health["issues"])
                )
        else:
            try:
                show_plot(engine, args.amplitude, replay_chunks=chunks)
            finally:
                chunks.close()
        return engine
    if not args.port:
        parser.error(
            "Use the Live Plot task for automatic detection, or --port / ECG_PORT."
        )
    choice = choose_input_mode()
    record_path = None
    if args.record:
        base = Path(__file__).resolve().parent / "recordings"
        record_path = Path(args.record)
        if not record_path.is_absolute():
            record_path = base / record_path
        if not record_path.resolve().is_relative_to(base):
            parser.error(
                "Recordings must be saved under the project's ignored recordings/ directory"
            )
    with (
        Recorder(record_path, vars(args)) if record_path else nullcontext()
    ) as recorder:
        with LiveAcquisition(
            engine, args.port, choice, recorder, echo_serial=args.monitor
        ) as live:
            if args.monitor:
                try:
                    monitor_serial(live)
                except KeyboardInterrupt:
                    print("\nSerial Monitor closed.")
            else:
                show_plot(engine, args.amplitude, live=live)
    if recorder:
        health = recording_health(engine)
        print("Recording saved: " + str(record_path))
        print(json.dumps({**engine.diagnostics(), "health": health}))
        if not health["healthy"]:
            raise RuntimeError(
                "Recording saved; acquisition checks failed: "
                + "; ".join(health["issues"])
            )
    return engine


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        raise SystemExit("ERROR: " + str(error)) from error
