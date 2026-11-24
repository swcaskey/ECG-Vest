"""Save exact PCB serial bytes and replay using the capture timeline."""

import csv
import json
from pathlib import Path
import time

from .acquisition import Capture
from .protocol import LEADS, MAX_LINE, POLARITY, parse_record
from .timing import RATE, SAMPLE_US

FORMAT = "ecg-pcb-e2-csv-1"
COLUMNS = ["index", "wire_hex", "text"]


class Recorder:
    def __init__(self, path, transport):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "format": FORMAT,
            "protocol": "E2",
            "transport": transport,
            "sample_rate_hz": RATE,
            "lead_order": LEADS,
            "polarity": POLARITY,
            "units": "ADC counts",
        }
        self.file = path.open("x", newline="", encoding="utf8")
        try:
            self.writer = csv.writer(self.file)
            self.writer.writerow(COLUMNS)
            self.writer.writerow((0, "", json.dumps(metadata)))
        except BaseException:
            self.file.close()
            raise
        self.index = 0

    def write(self, chunk):
        if chunk:
            self.index += 1
            self.writer.writerow(
                (self.index, chunk.hex(), chunk.decode("ascii", errors="replace"))
            )
            self.file.flush()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.close()


def _rows(path):
    with open(path, newline="", encoding="utf8") as stream:
        if stream.readline().strip().split(",") != COLUMNS:
            raise ValueError("Not an ECG Vest recording")
        rows = csv.reader(stream, strict=True)
        try:
            row = next(rows, [])
            if len(row) != len(COLUMNS) or row[:2] != ["0", ""]:
                raise ValueError("Missing PCB recording metadata")
            details = json.loads(row[2])
            if not isinstance(details, dict) or details.get("format") != FORMAT:
                raise ValueError(
                    "Unsupported recording format. Open it with the version that recorded it."
                )
            if (
                details.get("protocol") != "E2"
                or details.get("transport") not in ("usb", "ble")
                or details.get("sample_rate_hz") != RATE
                or details.get("lead_order") != list(LEADS)
                or details.get("polarity") != list(POLARITY)
            ):
                raise ValueError("Unsupported PCB capture configuration")
            yield details
            for index, row in enumerate(rows, 1):
                if len(row) != len(COLUMNS) or row[0] != str(index) or not row[1]:
                    raise ValueError(f"Missing or reordered recording row {index}")
                chunk = bytes.fromhex(row[1])
                if not chunk:
                    raise ValueError(f"Empty recording chunk at row {index}")
                yield chunk
        except csv.Error as error:
            raise ValueError(f"Invalid recording CSV: {error}") from error


def _frames(chunks):
    pending = bytearray()
    for chunk in chunks:
        for byte in chunk:
            pending.append(byte)
            if byte == 10 or len(pending) > MAX_LINE:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


class Playback:
    """Follow MCU time and sample positions, independent of USB packet grouping."""

    def __init__(self, path, clock=time.monotonic):
        self.rows = _rows(path)
        details = next(self.rows)
        self.capture = Capture(details["transport"])
        self.frames = _frames(self.rows)
        self.clock = clock
        self.started = None
        self.pending = None
        self.session = None
        self.origin_us = 0
        self.segment_us = 0
        self.position_us = 0
        self.done = False

    def _position(self, frame):
        if self.capture.discarding_line or not frame.endswith(b"\n"):
            return self.position_us
        try:
            kind, key, afe, data = parse_record(frame.decode("ascii").strip())
        except ValueError:
            return (
                self.position_us
            )  # The acquisition parser handles notices and errors.
        if kind == "C":
            stamp = data.launch_us
            if key[0] != self.session:
                self.session = key[0]
                self.origin_us = stamp
                self.segment_us = self.position_us
        elif key[0] != self.session:
            return self.position_us
        elif kind == "A":
            stamp = data[1]
        elif kind == "D" and key == self.capture.key:
            channel = self.capture.channels.get(afe)
            if channel is None:
                return self.position_us
            first, samples = data
            stamp = channel.configuration.launch_us + (first + len(samples)) * SAMPLE_US
        else:
            return self.position_us
        return max(self.position_us, self.segment_us + stamp - self.origin_us)

    def advance(self, headless=False):
        if self.done:
            return
        now = self.clock()
        if self.started is None:
            self.started = now
        target = float("inf") if headless else (now - self.started) * 1e6
        # Limit per-frame work during GUI playback and allow headless batch validation.
        for _ in range(512):
            if self.pending is None:
                frame = next(self.frames, None)
                if frame is None:
                    self.done = True
                    return
                self.pending = frame, self._position(frame)
            frame, position = self.pending
            if position > target:
                return
            self.capture.feed(frame, position / 1e6)
            self.position_us = position
            self.pending = None

    def snapshot(self):
        return self.capture.snapshot(self.position_us / 1e6)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.rows.close()
