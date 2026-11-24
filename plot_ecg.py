"""View, monitor, record, and replay three ECG leads from the PCB or BLE receiver."""

import argparse
from contextlib import nullcontext
import json
from math import isfinite
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time

from ecg.acquisition import Capture
from ecg.protocol import LEADS
from ecg.recording import Playback, Recorder
from ecg.timing import WINDOW


class LiveCapture:
    """Own the USB port on a worker while the graph redraws."""

    def __init__(self, port_name, transport, command, recorder=None, echo=False):
        self.port_name = port_name
        self.capture = Capture(transport)
        self.command = command
        self.recorder = recorder
        self.echo = echo
        self.lock = Lock()
        self.opened = Event()
        self.stop = Event()
        self.error = None
        self.start_time = time.monotonic()
        self.thread = Thread(target=self._run, daemon=True, name="ECG serial")

    def _read(self, port, size):
        chunk = port.read(size)
        if not chunk:
            return
        if self.recorder:
            self.recorder.write(chunk)
        if self.echo:
            sys.stdout.write(chunk.decode("ascii", errors="replace"))
            sys.stdout.flush()
        with self.lock:
            self.capture.feed(chunk, time.monotonic())

    def _run(self):
        try:
            import serial

            with serial.Serial(
                self.port_name, 115200, timeout=0.1, write_timeout=1
            ) as port:
                try:
                    time.sleep(0.1)
                    port.reset_input_buffer()
                    port.write((self.command + "\n").encode("ascii"))
                    self.opened.set()
                    while not self.stop.is_set():
                        self._read(port, min(max(1, port.in_waiting), 16384))
                    if self.recorder:
                        # Close normally on a line boundary, even when USB splits a line.
                        deadline = time.monotonic() + 0.3
                        while (
                            self.capture.pending or self.capture.discarding_line
                        ) and time.monotonic() < deadline:
                            self._read(port, 1)
                finally:
                    try:
                        port.dtr = False
                        port.rts = False
                    except OSError:
                        pass  # A physically removed USB port cannot be reset.
        except (OSError, RuntimeError, ImportError) as error:
            self.error = error
            with self.lock:
                self.capture.invalidate(f"Serial session stopped: {error}")
        finally:
            self.opened.set()
            self.stop.set()

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            x, y, status = self.capture.snapshot(now)
            if self.capture.key is None and not self.capture.problem:
                if self.capture.transport == "usb" and now - self.start_time > 8:
                    status = (
                        "No capture reply. Check the PCB Nano USB port and firmware."
                    )
                elif status.startswith("Looking for") and now - self.start_time > 15:
                    status = (
                        "Looking for the BLE transmitter. Check the PCB Nano power."
                    )
            return x, y, status

    def __enter__(self):
        self.thread.start()
        try:
            if not self.opened.wait(4):
                raise TimeoutError("Timed out opening the ECG USB port")
            if self.error:
                raise self.error
        except BaseException:
            self.stop.set()
            self.thread.join(timeout=2)
            raise
        return self

    def __exit__(self, error_type, *_):
        self.stop.set()
        self.thread.join(timeout=2)
        if error_type is None:
            if self.thread.is_alive():
                raise TimeoutError("ECG serial worker did not close")
            if self.error:
                raise self.error


def show_plot(source, amplitude):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 6))
    lines = []
    for axis, lead in zip(axes, LEADS):
        (line,) = axis.plot([], [], linewidth=1)
        lines.append(line)
        axis.set(title=f"Lead {lead}", ylabel="ADC counts", xlim=(-WINDOW, 0))
        if amplitude is not None:
            axis.set_ylim(-amplitude, amplitude)
        axis.grid(True)
    axes[-1].set_xlabel("Estimated time (s)")
    fig.canvas.manager.set_window_title("ECG Vest")
    status = fig.suptitle(source.capture.status)
    fig.tight_layout()
    replay = source if isinstance(source, Playback) else None
    playback_error = None

    def update(_):
        nonlocal playback_error
        if replay and not playback_error:
            try:
                replay.advance()
            except (OSError, ValueError) as error:
                playback_error = str(error)
        x, y, message = source.snapshot()
        if playback_error:
            message = f"Replay failed: {playback_error}"
        elif replay and replay.done:
            passed = replay.capture.health()["healthy"]
            outcome = "checks passed" if passed else "checks failed"
            message = f"Replay complete ({outcome})" + (
                f"\n{message}" if message else ""
            )
        status.set_text(message)
        for index, (axis, line) in enumerate(zip(axes, lines)):
            line.set_data(x, y[index] if y else [])
            if y and amplitude is None:
                axis.relim()
                axis.autoscale_view(scalex=False)
        return (*lines, status)

    animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
    plt.show()
    if playback_error:
        raise ValueError(playback_error)
    return animation


def report_health(capture):
    health = capture.health()
    print(json.dumps(health, sort_keys=True))
    if not health["healthy"]:
        raise RuntimeError(
            "ECG acquisition checks failed: " + "; ".join(health["issues"])
        )


def choose_command():
    while True:
        command = input("Type go to start: ").strip().lower()
        if command in ("go", "yes", "y"):
            return command


def recording_path(name):
    base = (Path(__file__).resolve().parent / "recordings").resolve()
    path = Path(name)
    path = (path if path.is_absolute() else base / path).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() != ".csv":
        raise ValueError("Recordings require a .csv filename under recordings/")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serial_port", nargs="?", help="PCB or BLE receiver USB port")
    parser.add_argument(
        "--port", help="PCB or BLE receiver USB port (for example COM7)"
    )
    parser.add_argument("--transport", choices=("usb", "ble"), default="usb")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--monitor", action="store_true", help="Print the serial stream"
    )
    source.add_argument("--replay", metavar="CSV", help="Replay a saved PCB recording")
    source.add_argument(
        "--record", metavar="CSV", help="Record while viewing the live plot"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Validate a replay without a GUI"
    )
    parser.add_argument(
        "--range", type=float, dest="amplitude", help="Fixed ±ADC count plot limits"
    )
    args = parser.parse_args(argv)
    if args.headless and not args.replay:
        parser.error("--headless requires --replay")
    if args.amplitude is not None and (
        not isfinite(args.amplitude) or args.amplitude <= 0
    ):
        parser.error("--range must be positive and finite")
    if args.replay:
        with Playback(args.replay) as replay:
            if args.headless:
                while not replay.done:
                    replay.advance(headless=True)
            else:
                show_plot(replay, args.amplitude)
            if replay.done:
                report_health(replay.capture)
        return
    if args.port and args.serial_port:
        parser.error("Select the port once, using a positional value or --port")
    port = args.port or args.serial_port or os.environ.get("ECG_PORT")
    if not port:
        parser.error("Provide the PCB or BLE receiver port with --port")
    path = recording_path(args.record) if args.record else None
    command = choose_command()
    with Recorder(path, args.transport) if path else nullcontext() as recorder:
        with LiveCapture(port, args.transport, command, recorder, args.monitor) as live:
            if args.monitor:
                print("Serial Monitor running. Press Ctrl+C to close.")
                try:
                    live.stop.wait()
                except KeyboardInterrupt:
                    print("\nSerial Monitor closed.")
            else:
                show_plot(live, args.amplitude)
    if path:
        print(f"Recording saved: {path}")
        report_health(live.capture)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, ImportError, EOFError) as error:
        raise SystemExit(f"ERROR: {error}") from error
