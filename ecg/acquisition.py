"""Track PCB samples, timing, and capture faults."""

from collections import Counter, deque
from dataclasses import dataclass, field
from math import ceil, floor, nan

from .protocol import Configuration, LEADS, MAX_LINE, POLARITY, parse_record
from .timing import (
    RATE,
    SAMPLE_US,
    WINDOW,
    Clock,
    add_anchor,
    clock_ready,
    interpolate,
    reset_clock,
    sample_time_us,
)

AFE_IDS = set(range(1, len(LEADS) + 1))
STATUS_FAULTS = (1 << 22) | (1 << 8)


@dataclass
class Channel:
    configuration: Configuration
    clock: Clock
    samples: deque = field(default_factory=lambda: deque(maxlen=RATE * 10))
    next_index: int = 0
    valid_samples: int = 0
    last_sample_rx: float | None = None
    last_anchor_rx: float | None = None


class Capture:
    def __init__(self, transport="usb"):
        self.key = None
        self.channels = {}
        self.pending = bytearray()
        self.discarding_line = False
        self.ready = False
        self.problem = None
        self.transport = transport
        self.status = (
            "Looking for the BLE transmitter…"
            if transport == "ble"
            else "Starting capture…"
        )
        self.faults = Counter()
        self.total_samples = Counter()
        self.epochs = 0
        self.last_rx = 0.0

    def invalidate(self, problem):
        self.problem = problem
        # Bound distinct messages even on malformed input.
        reason = (
            problem
            if problem in self.faults or len(self.faults) < 16
            else "Other faults"
        )
        self.faults[reason] += 1

    def feed(self, chunk, received_at):
        """Keep partial lines across reads. Receive time only checks freshness."""
        if not chunk:
            return
        self.last_rx = received_at
        for byte in chunk:
            if byte == 10:
                if not self.discarding_line:
                    try:
                        self._line(self.pending.decode("ascii").strip(), received_at)
                    except ValueError as error:
                        self.invalidate(f"Invalid E2 record: {error}")
                self.pending.clear()
                self.discarding_line = False
            elif not self.discarding_line:
                self.pending.append(byte)
                if len(self.pending) > MAX_LINE:
                    self.pending.clear()
                    self.discarding_line = True
                    self.invalidate("Oversized E2 record")

    def _line(self, line, received_at):
        if line.startswith("INFO: BLE_LOST "):
            reason = line.removeprefix("INFO: BLE_LOST ")
            self.invalidate(f"BLE link lost ({reason})")
            self.key = None
            self.channels.clear()
            self.ready = False
            self.problem = None
            self.status = f"BLE link lost ({reason}). Reconnecting…"
        elif line == "INFO: BLE_READY":
            if not self.ready:
                self.status = "BLE connected. Starting capture…"
        elif line and not line.startswith("INFO:"):
            self._accept(parse_record(line), received_at)

    def _accept(self, record, received_at):
        kind, key, afe, data = record
        if self.key and key[0] == self.key[0] and key[1] < self.key[1]:
            return  # Data or status from a retired epoch cannot replace it.
        if kind == "C":
            if key != self.key:
                self.key = key
                self.channels.clear()
                self.ready = False
                self.problem = None
                self.epochs += 1
            if self.ready or afe in self.channels:
                self.invalidate("Duplicate AFE configuration")
            else:
                clock = Clock(data.latency_ticks, data.timing_window_us)
                reset_clock(clock)
                self.channels[afe] = Channel(data, clock)
        elif kind == "E":
            if data == "MODE_CHANGE":
                self.ready = False
                self.status = "Starting capture…"
            else:
                self.invalidate(f"AFE {afe}: {data}" if afe else data)
        elif key == self.key:
            if kind == "R":
                if self.ready:
                    self.invalidate("Duplicate capture start")
                self.ready = self.channels.keys() == AFE_IDS
                if not self.ready:
                    self.invalidate("Capture started before all three AFEs reported")
            elif kind == "S":
                status, lost_irq, lost_tx = data
                if status & STATUS_FAULTS or lost_irq or lost_tx:
                    self.invalidate(f"AFE {afe}: FIFO, clock, or transport fault")
            elif kind in ("A", "D") and self.ready and not self.problem:
                channel = self.channels[afe]
                if kind == "A":
                    if data[0] > channel.next_index:
                        self.invalidate(
                            f"AFE {afe}: timing anchor refers to missing samples"
                        )
                    elif add_anchor(channel.clock, *data):
                        channel.last_anchor_rx = received_at
                    return
                first, samples = data
                if first != channel.next_index:
                    self.invalidate(f"AFE {afe}: sample sequence gap")
                    return
                for index, (value, tag) in enumerate(samples, first):
                    valid = tag in (0, 2)
                    channel.samples.append((index, value if valid else None))
                    channel.valid_samples += valid
                channel.next_index += len(samples)
                self.total_samples[afe] += len(samples)
                channel.last_sample_rx = received_at

    def snapshot(self, now):
        """Plot three measured leads with settling gaps preserved."""
        if self.problem:
            return (), (), self.problem
        if not self.ready:
            return (), (), self.status
        channels = [self.channels[afe] for afe in sorted(AFE_IDS)]
        if any(
            stamp is not None and now - stamp > 1
            for channel in channels
            for stamp in (channel.last_anchor_rx, channel.last_sample_rx)
        ):
            return (), (), "Capture stalled"
        if not all(clock_ready(ch.clock) and ch.samples for ch in channels):
            return (), (), "Collecting samples and timing…"

        points = [
            [
                (index, sample_time_us(ch.clock, index), value)
                for index, value in ch.samples
            ]
            for ch in channels
        ]
        last = floor(min(trace[-1][1] for trace in points) / SAMPLE_US)
        first = max(
            ceil(max(trace[0][1] for trace in points) / SAMPLE_US),
            last - RATE * WINDOW,
        )
        if last < first:
            return (), (), "Waiting for overlapping samples…"
        ticks = range(first, last + 1)
        x = [(index - last) / RATE for index in ticks]
        y = []
        for trace, sign in zip(points, POLARITY):
            values = (interpolate(trace, index * SAMPLE_US) for index in ticks)
            y.append([nan if value is None else sign * value for value in values])
        return x, y, ""

    def health(self, now=None):
        """Report current acquisition and faults retained from earlier epochs."""
        now = self.last_rx if now is None else now
        issues = [f"{reason} ({count})" for reason, count in self.faults.items()]
        if self.pending or self.discarding_line:
            issues.append("Incomplete final serial message")
        if not self.ready:
            issues.append(
                "Capture did not receive all three AFE configurations and READY"
            )
        timing = {}
        for afe, channel in sorted(self.channels.items()):
            clock = channel.clock
            fitted = clock_ready(clock)
            timing[str(afe)] = {
                "chip_id": f"0x{channel.configuration.chip_id:06X}",
                "estimated_sample_rate_hz": 1e6 / clock.period_us if fitted else None,
                "fit_residual_us": clock.residual_us if fitted else None,
                "rejected_anchors": clock.rejected,
            }
            if not channel.valid_samples:
                issues.append(f"AFE {afe}: no valid samples")
            if not fitted:
                issues.append(f"AFE {afe}: timing estimate unavailable")
            if any(
                stamp is not None and now - stamp > 1
                for stamp in (channel.last_anchor_rx, channel.last_sample_rx)
            ):
                issues.append(f"AFE {afe}: timing or samples stopped")
        return {
            "healthy": not issues,
            "issues": issues,
            "epochs": self.epochs,
            "samples": {str(afe): self.total_samples[afe] for afe in sorted(AFE_IDS)},
            "timing": timing,
        }
