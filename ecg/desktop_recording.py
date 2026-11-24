"""Browse, seek, and export PCB captures in the desktop app."""

from bisect import bisect_right
from collections import deque
from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from math import isfinite
from pathlib import Path
import sqlite3
from struct import Struct
from tempfile import NamedTemporaryFile, TemporaryFile
import time

from .acquisition import Capture, STATUS_FAULTS
from .protocol import LEADS, MAX_LINE, POLARITY, parse_record
from .recording import _frames, _rows
from .timing import SAMPLE_US

_FRAME = Struct("<dI")
_SEEK_HISTORY_US = 12_000_000


def _record(frame):
    if frame.endswith(b"\n") and len(frame) <= MAX_LINE + 1:
        try:
            return parse_record(frame.decode("ascii").strip())
        except ValueError:
            pass  # Preserve unrecognized records in the raw view.
    return None


@dataclass(frozen=True)
class RecordingInfo:
    path: Path
    modified: datetime
    transport: str
    duration: float

    @property
    def name(self):
        return self.path.name


@dataclass(frozen=True)
class _Checkpoint:
    position: float
    offset: int
    headers: tuple
    ready: bytes | None
    indexes: tuple
    problem: str | None


class _Timeline:
    """Track the capture clock and the state needed before a replay seek."""

    def __init__(self):
        self.position = self.origin = self.base = 0.0
        self.session = self.key = None
        self.headers = {}
        self.launches = {}
        self.indexes = {}
        self.ready = self.problem = None

    def checkpoint(self, offset):
        return _Checkpoint(
            self.position,
            offset,
            tuple(self.headers.values()),
            self.ready,
            tuple(self.indexes.items()),
            self.problem,
        )

    def accept(self, frame, record):
        stamp = None
        if frame.startswith(b"INFO: BLE_LOST "):
            self.key = None
            self.headers.clear()
            self.launches.clear()
            self.indexes.clear()
            self.ready = self.problem = None
        if record is None:
            if frame.strip() and not frame.startswith(b"INFO:"):
                self.problem = "Invalid E2 record"
            return self.position
        kind, key, afe, data = record
        if self.key and key[0] == self.key[0] and key[1] < self.key[1]:
            return self.position
        if kind == "C":
            stamp = data.launch_us
            if key[0] != self.session:
                self.session = key[0]
                self.origin = stamp
                self.base = self.position
            if key != self.key:
                self.key = key
                self.headers.clear()
                self.launches.clear()
                self.indexes.clear()
                self.ready = self.problem = None
            if afe in self.headers:
                self.problem = "Duplicate AFE configuration"
            else:
                self.headers[afe] = frame
                self.launches[afe] = stamp
        elif kind == "E":
            if data == "MODE_CHANGE":
                self.ready = None
            else:
                self.problem = f"AFE {afe}: {data}" if afe else data
        elif key == self.key:
            if kind == "R":
                self.ready = frame
            elif kind == "A":
                stamp = data[1]
            elif kind == "S" and (data[0] & STATUS_FAULTS or any(data[1:])):
                self.problem = f"AFE {afe}: FIFO, clock, or transport fault"
            elif kind == "D" and afe in self.launches:
                first, samples = data
                end = first + len(samples)
                stamp = self.launches[afe] + end * SAMPLE_US
                if self.ready and not self.problem:
                    if first == self.indexes.get(afe, 0):
                        self.indexes[afe] = end
                    else:
                        self.problem = f"AFE {afe}: sample sequence gap"
        if stamp is not None:
            self.position = max(self.position, self.base + stamp - self.origin)
        return self.position


@lru_cache(maxsize=64)
def _read_info(path, modified_ns, size):
    with closing(_rows(path)) as rows:
        details = next(rows)
        timeline = _Timeline()
        for frame in _frames(rows):
            timeline.accept(frame, _record(frame))
    return RecordingInfo(
        path,
        datetime.fromtimestamp(modified_ns / 1e9),
        details["transport"],
        timeline.position / 1e6,
    )


def list_recordings(directory):
    captures = []
    for path in Path(directory).glob("*.csv"):
        try:
            stat = path.stat()
            captures.append(_read_info(path, stat.st_mtime_ns, stat.st_size))
        except (OSError, ValueError):
            continue  # Other CSV files do not belong in the capture library.
    return sorted(captures, key=lambda info: info.modified, reverse=True)


class DesktopPlayback:
    """Replay with a bounded seek cost and a rolling serial view."""

    def __init__(self, path, clock=time.monotonic):
        self._clock = clock
        self._stream = TemporaryFile()
        timeline = _Timeline()
        self._checkpoints = [timeline.checkpoint(0)]
        try:
            with closing(_rows(path)) as rows:
                self.transport = next(rows)["transport"]
                for frame in _frames(rows):
                    if timeline.position - self._checkpoints[-1].position >= 1_000_000:
                        self._checkpoints.append(
                            timeline.checkpoint(self._stream.tell())
                        )
                    stamp = timeline.accept(frame, _record(frame))
                    self._stream.write(_FRAME.pack(stamp, len(frame)))
                    self._stream.write(frame)
        except BaseException:
            self._stream.close()
            raise
        self._checkpoint_times = [entry.position for entry in self._checkpoints]
        self.duration = timeline.position / 1e6
        self._raw = deque(maxlen=400)
        self.paused = True
        self.speed = 1.0
        self.seek(0)

    @property
    def position(self):
        return self._position / 1e6

    @property
    def seeking(self):
        return self._seeking

    @property
    def raw_text(self):
        return b"".join(self._raw).decode("ascii", errors="replace")

    def play(self):
        if self.done:
            self.seek(0)
        self._last_time = self._clock()
        self.paused = False

    def pause(self):
        self.paused = True

    def seek(self, seconds):
        if not isfinite(seconds):
            raise ValueError("Replay position must be finite")
        target = max(0, min(self.duration, seconds)) * 1e6
        history = max(0, target - _SEEK_HISTORY_US)
        index = bisect_right(self._checkpoint_times, history) - 1
        entry = self._checkpoints[index]
        self.capture = Capture(self.transport)
        for frame in entry.headers:
            self.capture.feed(frame, entry.position / 1e6)
        if entry.ready is not None:
            self.capture.feed(entry.ready, entry.position / 1e6)
        for afe, next_index in entry.indexes:
            self.capture.channels[afe].next_index = next_index
        if entry.problem:
            self.capture.problem = entry.problem
        self._stream.seek(entry.offset)
        self._reading = None
        self._raw.clear()
        self._position = entry.position
        self._target = target
        self._seeking = True
        self._last_time = self._clock()
        self.done = False

    def advance(self):
        now = self._clock()
        elapsed, self._last_time = now - self._last_time, now
        if self.done or (self.paused and not self._seeking):
            return
        if not self.paused and not self._seeking:
            self._target = min(
                self.duration * 1e6, self._target + elapsed * self.speed * 1e6
            )
        for _ in range(512):
            if self._reading is None:
                header = self._stream.read(_FRAME.size)
                if not header:
                    self.done = self.paused = True
                    self._seeking = False
                    return
                stamp, size = _FRAME.unpack(header)
                self._reading = stamp, self._stream.read(size)
            stamp, frame = self._reading
            if stamp > self._target:
                self._position = self._target
                self._seeking = False
                return
            self._raw.append(frame)
            self.capture.feed(frame, stamp / 1e6)
            self._position = stamp
            self._reading = None

    def snapshot(self):
        return self.capture.snapshot(self.position)

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def export_samples(source_path, destination_path):
    """Export ADC counts by capture and sample index, with blanks for gaps."""
    destination = Path(destination_path)
    if Path(source_path).resolve() == destination.resolve():
        raise ValueError("Choose a different file from the raw recording")
    # SQLite uses a temporary file so long or incomplete captures stay bounded.
    with closing(sqlite3.connect("")) as samples, closing(_rows(source_path)) as rows:
        next(rows)  # Validated capture metadata.
        samples.execute(
            "CREATE TABLE data (run_order INTEGER, sample INTEGER, "
            "i INTEGER, ii INTEGER, iii INTEGER, PRIMARY KEY (run_order, sample))"
        )
        runs = {}
        identities = []
        for frame in _frames(rows):
            record = _record(frame)
            if record is None or record[0] != "D":
                continue
            _, key, afe, (first, readings) = record
            if first + len(readings) > 1 << 63:
                raise ValueError("Sample index exceeds the recording range")
            if key not in runs:
                runs[key] = len(identities)
                identities.append(key)
            column = ("i", "ii", "iii")[afe - 1]
            sign = POLARITY[afe - 1]
            samples.executemany(
                f"INSERT INTO data (run_order, sample, {column}) VALUES (?, ?, ?) "
                f"ON CONFLICT(run_order, sample) DO UPDATE "
                f"SET {column}=excluded.{column}",
                (
                    (runs[key], index, value * sign if tag in (0, 2) else None)
                    for index, (value, tag) in enumerate(readings, first)
                ),
            )
        if not identities:
            raise ValueError("The recording contains no ECG samples")
        temp = None
        count = 0
        try:
            with NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf8",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temp = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(
                    (
                        "session",
                        "epoch",
                        "sample_index",
                        *(f"Lead {lead} (ADC counts)" for lead in LEADS),
                    )
                )
                for run, index, i, ii, iii in samples.execute(
                    "SELECT run_order, sample, i, ii, iii "
                    "FROM data ORDER BY run_order, sample"
                ):
                    writer.writerow((*identities[run], index, i, ii, iii))
                    count += 1
            temp.replace(destination)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return count
