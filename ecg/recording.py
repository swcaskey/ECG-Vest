"""Lossless raw serial chunks in CSV; replay never uses packet arrival times."""

import csv
import json
from pathlib import Path
import time

from .protocol import MAX_LINE, ProtocolError, parse_line

FORMAT = "ecg-e2-csv-1"


class Recorder:
    def __init__(self, path, options=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("x", newline="", encoding="utf8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(("index", "wire_hex", "text"))
        self.writer.writerow(
            (
                0,
                "",
                json.dumps(
                    {
                        "format": FORMAT,
                        "protocol": "E2",
                        "units": "ADC counts; ESP monotonic microseconds",
                        "options": options or {},
                    }
                ),
            )
        )
        self.index = 0

    def write(self, chunk):
        self.index += 1
        self.writer.writerow(
            (self.index, chunk.hex(), chunk.decode("ascii", errors="replace"))
        )
        self.file.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _metadata(rows):
    try:
        if rows.fieldnames != ["index", "wire_hex", "text"]:
            raise ValueError("Not an ECG CSV recording")
        metadata = next(rows, None)
        details = json.loads(metadata["text"]) if metadata else None
    except (csv.Error, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid ECG recording metadata") from exc
    if (
        not isinstance(details, dict)
        or details.get("format") != FORMAT
        or details.get("protocol") != "E2"
        or metadata.get("index") != "0"
        or metadata.get("wire_hex") != ""
        or not isinstance(details.get("options", {}), dict)
    ):
        raise ValueError("Unsupported ECG recording format")
    return details


def recording_options(path):
    """Read setup provenance without changing or relabeling the recorded wire."""
    with open(path, newline="", encoding="utf8") as stream:
        return _metadata(csv.DictReader(stream)).get("options", {})


def recorded_lead_i_board(options):
    if options.get("lead_i_board") is not None:
        board = options["lead_i_board"]
        if type(board) is not int or board not in (1, 2):
            raise ValueError("Invalid recorded lead mapping")
        return board
    if "diagnostic_plug_swap" in options:
        if type(options["diagnostic_plug_swap"]) is not bool:
            raise ValueError("Invalid diagnostic plug mapping")
        return 2 if options["diagnostic_plug_swap"] else 1
    return None


def replay(path):
    with open(path, newline="", encoding="utf8") as stream:
        rows = csv.DictReader(stream)
        _metadata(rows)
        try:
            for index, row in enumerate(rows, 1):
                if None in row or int(row["index"]) != index:
                    raise ValueError("Missing/reordered CSV chunk")
                yield bytes.fromhex(row["wire_hex"])
        except (csv.Error, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid ECG recording row: " + str(exc)) from exc


def recording_health(engine):
    """File completeness and acquisition health, separate from timing validation."""
    result = engine.health()
    if engine.parser.pending or engine.parser.discarding:
        result["issues"].append("Incomplete final serial message")
        result["healthy"] = False
    return result


def _wire_frames(chunks):
    # Reassemble ordinary lines; cap malformed fragments without losing bytes.
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
    """Pace replay from MCU progress, independent of saved USB fragmentation."""

    def __init__(self, engine, chunks, clock=time.monotonic):
        self.engine, self.clock = engine, clock
        self.frames = iter(_wire_frames(chunks))
        self.pending = None
        self.started = None
        self.session = None
        self.origin_us = None
        self.segment_us = 0
        self.position_us = 0
        self.done = False
        self.error = None

    def _position(self, wire):
        if (
            self.engine.parser.discarding
            or not wire.endswith(b"\n")
            or len(wire) - 1 > MAX_LINE
        ):
            return self.position_us
        try:
            record = parse_line(wire.decode("ascii").strip())
        except (ProtocolError, UnicodeDecodeError):
            return self.position_us  # Feed the actual error to the acquisition parser.
        if record is None:
            return self.position_us
        key = (record.session, record.epoch)
        if key in self.engine.retired:
            return self.position_us
        if (
            self.engine.key
            and record.session == self.engine.key[0]
            and record.epoch < self.engine.key[1]
        ):
            return self.position_us
        stamp = None
        if record.kind == "C":
            stamp = record.fields[2]
        elif record.kind in ("A", "E"):
            stamp = record.fields[1]
        elif record.kind == "D" and key == self.engine.key:
            channel = self.engine.channels.get(record.board)
            if channel:
                last = record.fields[0] + len(record.fields[1]) - 1
                if channel.clock.ready:
                    stamp = (
                        channel.clock.time_us(last)
                        + channel.clock.latency_ticks * channel.clock.period_us / 256
                    )
                else:
                    stamp = channel.launch_us + (last + 1) * 1e6 / 128
        if stamp is None:
            return self.position_us
        if self.session is None or (
            record.kind == "C" and record.session != self.session
        ):
            self.session = record.session
            self.origin_us = stamp
            self.segment_us = self.position_us
        elif record.session != self.session:
            return self.position_us
        # New boot sessions have unrelated MCU origins; preserve gaps within a boot.
        return max(self.position_us, self.segment_us + stamp - self.origin_us)

    def advance(self, max_frames=512):
        if self.done:
            return
        now = self.clock()
        if self.started is None:
            self.started = now
        target_us = (now - self.started) * 1e6
        try:
            for _ in range(max_frames):
                if self.pending is None:
                    wire = next(self.frames, None)
                    if wire is None:
                        self.done = True
                        return
                    self.pending = (wire, self._position(wire))
                wire, position = self.pending
                if position > target_us:
                    return
                self.engine.feed(wire)
                self.position_us = position
                self.pending = None
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            self.engine.invalidate(self.error)
            self.done = True

    def result(self):
        if not self.done:
            return "Replay in progress"
        if self.error:
            return "Replay failed: " + self.error
        health = recording_health(self.engine)
        if not health["healthy"]:
            return "Replay complete; acquisition checks FAILED: " + "; ".join(
                health["issues"]
            )
        return "Replay complete; acquisition checks passed" + (
            "; timing UNVALIDATED" if not health["timing_validated"] else ""
        )
