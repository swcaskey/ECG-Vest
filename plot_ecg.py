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
from ecg.recording import Recorder, replay


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
                    if self.engine.ready:
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

    figure, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 8))
    figure.canvas.manager.set_window_title("ECG Vest")
    titles = (
        "Lead I - measured, Board 1",
        "Lead II - estimated from I + III",
        "Lead III - measured, Board 2",
    )
    lines = []
    for axis, title in zip(axes, titles):
        (line,) = axis.plot([], [], linewidth=1)
        lines.append(line)
        axis.set(
            title=title, ylabel="ADC counts", ylim=(-amplitude, amplitude), xlim=(-5, 0)
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
        if replay_chunks is not None:
            # Bounded replay work per redraw; pauses remain in the MCU timeline.
            for _ in range(8):
                chunk = next(replay_chunks, None)
                if chunk is None:
                    break
                engine.feed(chunk)
        with live.lock if live else nullcontext():
            measured, derived, status = engine.snapshot()
            diagnostics = engine.diagnostics()
        for board, line in ((1, lines[0]), (2, lines[2])):
            line.set_data(*measured.get(board, ([], [])))
        lines[1].set_data(*derived)
        derived_note.set_text(status)
        clipped = any(
            abs(y) > amplitude
            for _, ys in [*measured.values(), derived]
            for y in ys
            if math.isfinite(y)
        )
        timing = diagnostics["timing"]
        parts = [
            "Internal test" if engine.mode == 0 else "External inputs",
            "time association UNVALIDATED",
        ]
        for board, values in timing.items():
            if values["ready"]:
                parts.append(
                    "B%s candidate %.4f sps; residual %.1f us"
                    % (board, values["candidate_fs"], values["residual_us"])
                )
            else:
                parts.append("B%s: collecting anchors / check SAMP ratio" % board)
        if "relative_ppm" in diagnostics:
            parts.append("III vs I: %+.1f ppm" % diagnostics["relative_ppm"])
        parts.append("parser errors %d" % diagnostics["parser_errors"])
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
        "--headless",
        action="store_true",
        help="Validate a recording without GUI or serial hardware",
    )
    parser.add_argument("--range", type=float, default=8000, dest="amplitude")
    parser.add_argument(
        "--allow-unvalidated-timing",
        action="store_true",
        help="Show explicitly UNVALIDATED candidate sum; not a synchronization claim",
    )
    parser.add_argument(
        "--samp-ratio",
        type=int,
        choices=(1, 2, 4),
        help="Override the SAMP/FIFO ratio advertised by the firmware",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.amplitude) or args.amplitude <= 0:
        parser.error("--range must be positive and finite")
    if args.record and args.replay:
        parser.error("Choose recording or replay")
    if args.monitor and args.replay:
        parser.error("Choose serial monitoring or replay")
    if args.headless and not args.replay:
        parser.error("--headless requires --replay")
    engine = Acquisition(args.allow_unvalidated_timing, args.samp_ratio)
    if args.replay:
        chunks = replay(args.replay)
        if args.headless:
            for chunk in chunks:
                engine.feed(chunk)
            print(json.dumps(engine.diagnostics(), sort_keys=True))
            if (
                not engine.ready
                or engine.errors
                or engine.parser.pending
                or engine.parser.discarding
            ):
                raise RuntimeError(
                    "Replay contains incomplete configuration or malformed records"
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
    return engine


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        raise SystemExit("ERROR: " + str(error)) from error
