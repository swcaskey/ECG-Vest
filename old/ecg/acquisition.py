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
    valid_samples: int = 0
    stale_notice: bool = False
    latest_data_us: int | None = None
    data_stale_notice: bool = False

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
    def __init__(
        self, allow_unvalidated=False, samp_ratio=None, lead_i_board=1, bias_board=None
    ):
        for board in (lead_i_board, bias_board):
            if board is not None and (type(board) is not int or board not in (1, 2)):
                raise ValueError("Lead/bias board must be 1, 2, or unspecified")
        self.parser = Parser()
        self.allow_unvalidated = allow_unvalidated
        self.samp_ratio = samp_ratio
        self.lead_i_board = lead_i_board
        self.bias_board = bias_board
        self.key = None
        self.retired = deque(maxlen=64)
        self.channels = {}
        self.ready = False
        self.valid = False
        self.raw_valid = False
        self.mode = None
        self.now_us = 0
        self.events = deque(maxlen=12)
        self.records = 0
        self.errors = 0
        self.epochs = 0
        self.sample_count = [0, 0]
        self.fault_count = 0
        self.fault_reasons = {}
        self.timing_context = deque(maxlen=18)

    @property
    def lead_iii_board(self):
        return 3 - self.lead_i_board if self.lead_i_board is not None else None

    def _remember_fault(self, reason):
        # Keep capture history after recovery without an unbounded event log.
        reason = str(reason)[:160]
        if reason not in self.fault_reasons and len(self.fault_reasons) >= 15:
            reason = "Other acquisition faults"
        self.fault_count += 1
        self.fault_reasons[reason] = self.fault_reasons.get(reason, 0) + 1

    def invalidate(self, reason, *, record_fault=True, timing_only=False):
        if record_fault:
            self._remember_fault(reason)
        self.valid = False
        if not timing_only:
            self.raw_valid = False
        for channel in self.channels.values():
            if channel.clock.ready:
                channel.last_mapping = (
                    channel.clock.time_us(0),
                    channel.clock.period_us,
                )
            channel.clock.invalidate()
        if not self.events or self.events[-1] != reason:
            self.events.append(reason)

    def feed(self, chunk):
        records = self.parser.feed(chunk)
        if self.parser.errors:
            self.errors += len(self.parser.errors)
            for _ in self.parser.errors:
                self._remember_fault("Malformed serial record")
            self.invalidate(self.parser.errors[-1], record_fault=False)
        for record in records:
            self.accept(record)
        # A malformed line in the same chunk must not be hidden by a ready record.
        if self.parser.errors:
            self.invalidate(self.parser.errors[-1], record_fault=False)
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
                    self._remember_fault("Acquisition restarted or changed mode")
                    self.retired.append(self.key)
                self.key = key
                self.channels.clear()
                self.ready = False
                self.valid = True
                self.raw_valid = True
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
            effective_ratio = self.samp_ratio or ratio
            self.channels[record.board] = Channel(
                launch,
                mode,
                bool(associated) and effective_ratio == ratio,
                ClockModel(effective_ratio, latency),
            )
            self.now_us = max(self.now_us, launch)
            return
        if record.kind == "E" and record.fields[0] == "INVALID_MODE":
            return
        if record.kind == "E" and self.key is None:
            reason = "Board %d: %s" % (record.board, record.fields[0])
            if record.fields[0] == "MODE_CHANGE":
                self.events.append(reason)
            else:
                self.invalidate(reason)
            return
        if key != self.key:
            return
        if record.kind == "T":
            pulse, stamp, interval, fifo_count, irq_lost = record.fields
            self.timing_context.append(
                {
                    "session": record.session,
                    "epoch": record.epoch,
                    "board": record.board,
                    "pulse_count": pulse,
                    "esp_us": stamp,
                    "interval_us": interval,
                    "fifo_read_count": fifo_count,
                    "irq_lost": irq_lost,
                }
            )
            return
        if record.kind == "R":
            mode = record.fields[0]
            settings = {
                (c.mode, c.launch_us, c.clock.ratio, c.clock.latency_ticks)
                for c in self.channels.values()
            }
            if (
                set(self.channels) == {1, 2}
                and len(settings) == 1
                and all(c.mode == mode for c in self.channels.values())
            ):
                self.ready = True
                self.mode = mode
            else:
                self.invalidate("Ready received without both matching configurations")
            return
        if record.kind == "E":
            code, stamp = record.fields
            self.now_us = max(self.now_us, stamp)
            self.invalidate(
                "Board %d: %s" % (record.board, code),
                timing_only=code.startswith("TIMING_") or code == "IRQ_QUEUE_OVERFLOW",
            )
            return
        channel = self.channels.get(record.board)
        if channel is None or not self.ready:
            return
        if record.kind == "A":
            pulse, stamp = record.fields
            self.now_us = max(self.now_us, stamp)
            if self.valid and not channel.clock.add(pulse, stamp):
                self.events.append("Board %d: rejected timing anchor" % record.board)
            self._check_timing_age()
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
                if tag in (0, 2):
                    channel.valid_samples += 1
            channel.next_seq = first + len(samples)
            # Sequence progress ages anchors even if both A streams stop.
            # This freshness estimate does not validate the sample-to-pulse phase.
            channel.latest_data_us = channel.launch_us + round(
                channel.next_seq * channel.clock.period_us
            )
            self.now_us = max(self.now_us, channel.latest_data_us)
            self._check_timing_age()
        elif record.kind == "S":
            status, _, irq_lost, tx_lost = record.fields
            counts = (irq_lost, tx_lost)
            channel.observed_pulses = record.fields[1]
            previous = channel.loss_counts or (0, 0)
            if any(new < old for new, old in zip(counts, previous)):
                self.invalidate("Loss counter decreased within an epoch")
            if irq_lost > previous[0]:
                self.invalidate("Timing queue loss", timing_only=True)
            if tx_lost > previous[1]:
                self.invalidate("Transport queue loss")
            channel.loss_counts = counts
            if status & ((1 << 22) | (1 << 8)):
                self.invalidate("FIFO overflow or PLL unlock")

    def _check_timing_age(self):
        for board, channel in self.channels.items():
            stale = channel.clock.ready and channel.clock.stale(self.now_us)
            if stale and not channel.stale_notice:
                reason = "Board %d timing anchors became stale" % board
                self._remember_fault(reason)
                self.events.append(reason)
            channel.stale_notice = stale
            data_stale = channel.clock.ready and self._data_stale(channel)
            if data_stale and not channel.data_stale_notice:
                reason = "Board %d sample data became stale" % board
                self._remember_fault(reason)
                self.events.append(reason)
            channel.data_stale_notice = data_stale

    def _data_stale(self, channel):
        return (
            channel.valid_samples > 0
            and channel.latest_data_us is not None
            and self.now_us - channel.latest_data_us > 1_000_000
        )

    def health(self):
        """Report current readiness and persistent faults for the whole capture."""
        issues = [
            "%s (%d)" % (reason, count) for reason, count in self.fault_reasons.items()
        ]
        if not self.ready or set(self.channels) != {1, 2}:
            issues.append("Both matching board configurations are required")
        if not self.raw_valid:
            issues.append("Current acquisition is invalid")
        elif not self.valid:
            issues.append("Timing alignment is unavailable; measured ECG can continue")
        for board in (1, 2):
            channel = self.channels.get(board)
            if channel is None:
                continue
            if not channel.valid_samples:
                issues.append("Board %d has no valid-voltage samples" % board)
            elif self._data_stale(channel):
                issues.append("Board %d sample data are stale" % board)
            if not channel.clock.ready:
                issues.append("Board %d has no usable clock fit" % board)
            elif channel.clock.stale(self.now_us):
                issues.append("Board %d timing anchors are stale" % board)
        return {
            "healthy": not issues,
            "issues": issues,
            "fault_count": self.fault_count,
            "timing_validated": (
                self.ready
                and self.valid
                and set(self.channels) == {1, 2}
                and all(c.association_valid for c in self.channels.values())
            ),
        }

    def status(self):
        if not self.ready:
            return "Waiting for both boards"
        if not self.valid:
            prefix = (
                "Timing unavailable; measured ECG continues: "
                if self.raw_valid
                and all(
                    c.valid_samples and not self._data_stale(c)
                    for c in self.channels.values()
                )
                else "Alignment unavailable: "
            )
            return prefix + (self.events[-1] if self.events else "epoch invalid")
        if any(not c.clock.ready for c in self.channels.values()):
            return "Collecting FIFO timing anchors"
        if any(c.clock.stale(self.now_us) for c in self.channels.values()):
            return "Alignment unavailable: stale timing anchors"
        if any(self._data_stale(c) for c in self.channels.values()):
            return "Alignment unavailable: stale sample data"
        if (
            not all(c.association_valid for c in self.channels.values())
            and not self.allow_unvalidated
        ):
            label = "Lead II" if self.lead_i_board else "Board sum"
            return label + " unavailable: FIFO time association unvalidated"
        label = (
            "Internal-test sum"
            if self.mode == 0
            else ("Lead II estimate" if self.lead_i_board else "Board 1 + Board 2 sum")
        )
        return (
            label
            if all(c.association_valid for c in self.channels.values())
            else label + ": UNVALIDATED timing"
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
                c.clock.ready
                and not c.clock.stale(self.now_us)
                and not self._data_stale(c)
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
            "raw_valid": self.raw_valid,
            "lead_i_board": self.lead_i_board,
            "lead_iii_board": self.lead_iii_board,
            "bias_board": self.bias_board,
            "events": list(self.events),
            "health": self.health(),
            "timing_context": list(self.timing_context),
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
                "anchor_sample_rate_hz": (
                    raw_hz / clock.ratio if raw_hz is not None else None
                ),
                "pulse_count": channel.observed_pulses,
            }
        data["timing"] = rates
        if len(rates) == 2 and all(r["candidate_fs"] for r in rates.values()):
            data["board2_vs_board1_ppm"] = 1e6 * (
                rates["2"]["candidate_fs"] / rates["1"]["candidate_fs"] - 1
            )
            if self.lead_i_board is not None:
                data["relative_ppm"] = 1e6 * (
                    rates[str(self.lead_iii_board)]["candidate_fs"]
                    / rates[str(self.lead_i_board)]["candidate_fs"]
                    - 1
                )
        return data
