"""Session handling, gap preservation and common-grid estimated lead addition."""

from collections import deque
from dataclasses import dataclass, field
import math

from .protocol import Parser
from .timing import ClockModel, interpolate

NOMINAL_PERIOD_US = 1_000_000 / 128
HISTORY = 1280


@dataclass
class Channel:
    launch_us: int
    mode: int
    association_valid: bool
    clock: ClockModel
    samples: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    next_seq: int = 0
    loss_counts: tuple | None = None
    last_mapping: tuple | None = None
    observed_pulses: int = 0

    def points(self, now_us):
        # Retain historical timing when stale; disable summation separately.
        if self.clock.ready:
            self.last_mapping = (self.clock.time_us(0), self.clock.period_us)
        return [
            (
                seq,
                (
                    self.last_mapping[0] + seq * self.last_mapping[1]
                    if self.last_mapping
                    else self.launch_us + (seq + 1) * NOMINAL_PERIOD_US - 149718.75
                ),
                value,
            )
            for seq, value in self.samples
        ]


class Acquisition:
    def __init__(self, allow_unvalidated=False, samp_ratio=None):
        self.parser = Parser()
        self.allow_unvalidated = allow_unvalidated
        self.samp_ratio = samp_ratio
        self.key = None
        self.retired = deque(maxlen=64)
        self.channels = {}
        self.ready = False
        self.valid = False
        self.mode = None
        self.now_us = 0
        self.events = deque(maxlen=12)
        self.records = 0
        self.errors = 0
        self.epochs = 0
        self.sample_count = [0, 0]

    def invalidate(self, reason):
        self.valid = False
        for channel in self.channels.values():
            channel.clock.invalidate()
        if not self.events or self.events[-1] != reason:
            self.events.append(reason)

    def feed(self, chunk):
        records = self.parser.feed(chunk)
        if self.parser.errors:
            self.errors += len(self.parser.errors)
            self.invalidate(self.parser.errors[-1])
        for record in records:
            self.accept(record)
        # A malformed line in the same chunk must not be hidden by a ready record.
        if self.parser.errors:
            self.invalidate(self.parser.errors[-1])
        return records

    def accept(self, record):
        self.records += 1
        key = (record.session, record.epoch)
        if record.kind == "C":
            if key in self.retired:
                return
            if self.key and key[0] == self.key[0] and key[1] < self.key[1]:
                return
            if key != self.key:
                if self.key:
                    self.retired.append(self.key)
                self.key = key
                self.channels.clear()
                self.ready = False
                self.valid = True
                self.now_us = 0
                self.epochs += 1
                self.mode = None
            mode, chip, launch, ratio, latency, associated = record.fields
            if (chip & 0xF03000) != 0x503000:
                self.invalidate("Board identification mismatch")
                return
            if record.board in self.channels:
                self.invalidate("Duplicate board configuration")
                return
            self.channels[record.board] = Channel(
                launch,
                mode,
                bool(associated),
                ClockModel(self.samp_ratio or ratio, latency),
            )
            self.now_us = max(self.now_us, launch)
            return
        if record.kind == "E" and record.fields[0] == "INVALID_MODE":
            return
        if record.kind == "E" and self.key is None:
            self.events.append("Board %d: %s" % (record.board, record.fields[0]))
            return
        if key != self.key:
            return
        if record.kind == "R":
            mode = record.fields[0]
            if set(self.channels) == {1, 2} and all(
                c.mode == mode for c in self.channels.values()
            ):
                self.ready = True
                self.mode = mode
            else:
                self.invalidate("Ready received without both matching configurations")
            return
        if record.kind == "E":
            code, stamp = record.fields
            self.now_us = max(self.now_us, stamp)
            self.invalidate("Board %d: %s" % (record.board, code))
            return
        channel = self.channels.get(record.board)
        if channel is None or not self.ready:
            return
        if record.kind == "A":
            pulse, stamp = record.fields
            self.now_us = max(self.now_us, stamp)
            if self.valid and not channel.clock.add(pulse, stamp):
                self.events.append("Board %d: rejected timing anchor" % record.board)
        elif record.kind == "D":
            first, samples = record.fields
            if first < channel.next_seq:
                self.invalidate("Duplicate/out-of-order sample batch")
                return
            if first != channel.next_seq:
                self.invalidate("Missing sample positions")
            for offset, (value, tag) in enumerate(samples):
                seq = first + offset
                if tag not in (0, 1, 2, 3):
                    self.invalidate("FIFO empty/overflow/reserved tag in sample batch")
                    return
                channel.samples.append((seq, value if tag in (0, 2) else None))
                self.sample_count[record.board - 1] += 1
            channel.next_seq = first + len(samples)
        elif record.kind == "S":
            status, _, irq_lost, tx_lost = record.fields
            counts = (irq_lost, tx_lost)
            channel.observed_pulses = record.fields[1]
            if any(counts) or (
                channel.loss_counts is not None and counts != channel.loss_counts
            ):
                self.invalidate("Timing/transport queue loss")
            channel.loss_counts = counts
            if status & ((1 << 22) | (1 << 8)):
                self.invalidate("FIFO overflow or PLL unlock")

    def status(self):
        if not self.ready:
            return "Waiting for both boards"
        if not self.valid:
            return "Alignment unavailable: " + (
                self.events[-1] if self.events else "epoch invalid"
            )
        if any(not c.clock.ready for c in self.channels.values()):
            return "Collecting timing (SAMP/FIFO association unvalidated)"
        if any(c.clock.stale(self.now_us) for c in self.channels.values()):
            return "Alignment unavailable: stale timing anchors"
        if (
            not all(c.association_valid for c in self.channels.values())
            and not self.allow_unvalidated
        ):
            return "Lead II unavailable: SAMP/FIFO association unvalidated"
        return (
            "Internal-test sum: UNVALIDATED timing"
            if self.mode == 0
            else (
                "Lead II estimate: UNVALIDATED timing"
                if not all(c.association_valid for c in self.channels.values())
                else "Lead II estimate"
            )
        )

    def snapshot(self):
        points = {board: c.points(self.now_us) for board, c in self.channels.items()}
        latest = max(
            (p[-1][1] for p in points.values() if p), default=float(self.now_us)
        )
        measured = {}
        for board, channel_points in points.items():
            xs, ys = [], []
            previous = None
            for seq, stamp, value in channel_points:
                if previous is not None and seq != previous + 1:
                    xs.append((stamp - latest) / 1e6)
                    ys.append(math.nan)
                xs.append((stamp - latest) / 1e6)
                ys.append(math.nan if value is None else value)
                previous = seq
            measured[board] = (xs, ys)
        can_sum = (
            self.ready
            and self.valid
            and len(points) == 2
            and all(
                c.clock.ready and not c.clock.stale(self.now_us)
                for c in self.channels.values()
            )
            and (
                self.allow_unvalidated
                or all(c.association_valid for c in self.channels.values())
            )
        )
        derived = ([], [])
        if can_sum and all(points.values()):
            end = min(p[-1][1] for p in points.values())
            begin = max(latest - 5e6, *(p[0][1] for p in points.values()))
            first_grid = math.ceil(begin / NOMINAL_PERIOD_US)
            xs, ys = [], []
            for grid in range(first_grid, math.floor(end / NOMINAL_PERIOD_US) + 1):
                stamp = grid * NOMINAL_PERIOD_US
                left = interpolate(points[1], stamp)
                right = interpolate(points[2], stamp)
                xs.append((stamp - latest) / 1e6)
                ys.append(math.nan if left is None or right is None else left + right)
            derived = xs, ys
        return measured, derived, self.status()

    def diagnostics(self):
        data = {
            "session_epoch": self.key,
            "records": self.records,
            "parser_errors": self.errors,
            "epochs": self.epochs,
            "samples": self.sample_count,
            "status": self.status(),
            "events": list(self.events),
        }
        rates = {}
        for board, channel in self.channels.items():
            clock = channel.clock
            raw_hz = None
            if len(clock.anchors) >= 2:
                p0, t0 = clock.anchors[0]
                p1, t1 = clock.anchors[-1]
                raw_hz = 1e6 * (p1 - p0) / (t1 - t0)
            rates[str(board)] = {
                "ready": clock.ready,
                "candidate_fs": 1e6 / clock.period_us if clock.ready else None,
                "residual_us": clock.residual_us if clock.ready else None,
                "samp_hz": raw_hz,
                "pulse_count": channel.observed_pulses,
            }
        data["timing"] = rates
        if len(rates) == 2 and all(r["candidate_fs"] for r in rates.values()):
            data["relative_ppm"] = 1e6 * (
                rates["2"]["candidate_fs"] / rates["1"]["candidate_fs"] - 1
            )
        return data
