"""Plot three time-aligned ECG leads from the PCB Nano over USB."""

import argparse
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from math import ceil, floor, fsum, isfinite, nan, sqrt
from statistics import median
import time

RATE = 128
SAMPLE_US = 1_000_000 / RATE
SPI_TIMING_WINDOW_US = 2_000  # Firmware discards wider STATUS brackets.
WINDOW = 5
LEADS = ("I", "II", "III")
# Order is AFE1, AFE2, AFE3. After a bench polarity check, change a sign to -1.
POLARITY = (1, 1, 1)


@dataclass
class Clock:
    ratio: int
    latency_ticks: int
    timing_window_us: int = 0
    rejected: int = 0


def reset_clock(clock):
    clock.anchors = deque(maxlen=256)
    clock._fit_ready = False
    clock._established = False
    clock._quality = deque(maxlen=64)
    clock.pulse_period_us = 1e6 / (128 * clock.ratio)
    clock.period_us = 1e6 / 128
    clock.residual_us = float("inf")
    clock._center_pulse = 0.0
    clock._center_us = 0.0
    clock._startup_rejections = 0


def clock_ready(clock):
    # A low-residual fit to a cherry-picked minority is not a good clock.
    return clock._fit_ready and (
        len(clock._quality) < 8 or sum(clock._quality) >= 0.8 * len(clock._quality)
    )


def predict_pulse(clock, pulse):
    return clock._center_us + (pulse - clock._center_pulse) * clock.pulse_period_us


def least_squares(points):
    # Center both axes before multiplication; esp_timer is a large int64.
    origin_p, origin_t = points[0]
    xs = [p - origin_p for p, _ in points]
    ys = [t - origin_t for _, t in points]
    mx = fsum(xs) / len(xs)
    my = fsum(ys) / len(ys)
    variance = fsum((x - mx) ** 2 for x in xs)
    slope = fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / variance
    return origin_p + mx, origin_t + my, slope


def fit_clock(clock):
    points = list(clock.anchors)
    if len(points) < 3:
        return
    # A robust initial slope avoids letting a startup timestamp outlier
    # distort the OLS fit and then reject all subsequent good anchors.
    slope = median(
        (t1 - t0) / (p1 - p0) for (p0, t0), (p1, t1) in zip(points, points[1:])
    )
    origin_p, origin_t = points[0]
    offsets = [t - origin_t - slope * (p - origin_p) for p, t in points]
    offset = median(offsets)
    mad = median(abs(x - offset) for x in offsets)
    threshold = max(100.0, clock.timing_window_us, 8 * 1.4826 * mad)
    inliers = [
        point
        for point, residual in zip(points, offsets)
        if abs(residual - offset) <= threshold
    ]
    if len(inliers) < 3:
        clock._fit_ready = False
        return
    center_p, center_t, slope = least_squares(inliers)
    output_period = slope * clock.ratio
    plausible = abs(output_period / (1e6 / 128) - 1) <= 0.02
    if not plausible:
        clock._fit_ready = False
        return
    clock.rejected += len(points) - len(inliers)
    clock.anchors = deque(inliers, maxlen=256)
    clock._center_pulse, clock._center_us = center_p, center_t
    clock.pulse_period_us = slope
    clock.period_us = output_period
    clock.residual_us = sqrt(
        fsum((t - predict_pulse(clock, p)) ** 2 for p, t in inliers) / len(inliers)
    )
    # Clock-fit quality and sample-index association are separate checks.
    clock._fit_ready = (
        len(inliers) >= 8
        and inliers[-1][1] - inliers[0][1] >= 500_000
        and clock.residual_us <= max(250, clock.timing_window_us / 2)
    )


def add_anchor(clock, pulse, time_us):
    """Accept a valid timing anchor; return False when rejected.

    A good fit helps estimate when samples were taken on the Nano clock.
    """
    accepted = accept_anchor(clock, pulse, time_us)
    clock._quality.append(accepted)
    if clock_ready(clock):
        clock._established = True
    return accepted


def accept_anchor(clock, pulse, time_us):
    if (
        not isinstance(pulse, int)
        or pulse < 0
        or not isinstance(time_us, (int, float))
        or not isfinite(time_us)
        or time_us < 0
    ):
        clock.rejected += 1
        return False
    if clock.anchors:
        last_p, last_t = clock.anchors[-1]
        if pulse <= last_p or time_us <= last_t:
            clock.rejected += 1
            return False
        nominal_delta = (pulse - last_p) * 1e6 / (128 * clock.ratio)
        interval_tolerance = max(150.0, nominal_delta * 0.025) + clock.timing_window_us
        if abs((time_us - last_t) - nominal_delta) > interval_tolerance:
            clock.rejected += 1
            clock._startup_rejections += 1
            if clock._established or clock._startup_rejections < 3:
                return False
            # Before any usable fit exists, a single corrupt first
            # timestamp must not poison every subsequent interval check.
            # Keep explicit pulse numbers; do not invent/reset their phase.
            reset_clock(clock)
        if clock._established and abs(time_us - predict_pulse(clock, pulse)) > max(
            150.0, clock.timing_window_us, 8 * clock.residual_us
        ):
            clock.rejected += 1
            return False
    clock._startup_rejections = 0
    clock.anchors.append((pulse, time_us))
    fit_clock(clock)
    return bool(clock.anchors and clock.anchors[-1] == (pulse, time_us))


def sample_time_us(clock, sample_index):
    """Estimate input time for FIFO position n, including settling entries.

    FIFO positions are one-based. Subtract the digital filter delay.
    """
    if not clock_ready(clock):
        raise ValueError("Clock model is not ready")
    return (
        predict_pulse(clock, (sample_index + 1) * clock.ratio)
        - clock.latency_ticks * clock.period_us / 256
    )


def interpolate(points, time_us, max_gap_us=1.5e6 / 128):
    """Interpolate only consecutive valid positions that bracket time_us.

    Points are (sequence, common_time_us, raw_value_or_None), sorted by time.
    Epoch compatibility is the caller's responsibility. Linear interpolation
    has error <= max|signal''|*sample_period**2/8; this is appreciable near the
    chip's 28.35 Hz low-pass limit and is not clinical reconstruction.
    """
    if not points or not isfinite(time_us):
        return None
    index = bisect_left(points, time_us, key=lambda point: point[1])
    if index < len(points) and points[index][1] == time_us:
        value = points[index][2]
        return value if value is not None and isfinite(value) else None
    if index == 0 or index == len(points):
        return None
    seq0, t0, v0 = points[index - 1]
    seq1, t1, v1 = points[index]
    width = t1 - t0
    if (
        seq1 != seq0 + 1
        or v0 is None
        or v1 is None
        or not isfinite(v0)
        or not isfinite(v1)
        or width <= 0
        or width > max_gap_us
    ):
        return None
    return v0 + (v1 - v0) * ((time_us - t0) / width)


@dataclass
class Channel:
    clock: Clock
    samples: deque = field(default_factory=lambda: deque(maxlen=RATE * 10))
    next_index: int = 0
    last_sample_rx: float = 0.0
    last_anchor_rx: float = 0.0


@dataclass
class Capture:
    key: tuple | None = None
    channels: dict = field(default_factory=dict)
    pending: bytearray = field(default_factory=bytearray)
    ready: bool = False
    problem: str | None = None
    status: str = "Waiting for the Nano to start ECG capture…"
    discarding_line: bool = False
    # A USB disconnect requires reopening the viewer.
    link_failed: bool = False


def integer(text, low=0, high=(1 << 63) - 1, base=10):
    value = int(text, base)
    if not low <= value <= high:
        raise ValueError("field out of range")
    return value


def parse_record(line):
    """Decode an E2 record from the Nano."""
    prefix, kind, session, run, *fields = line.split(",")
    if prefix != "E2":
        raise ValueError("expected an E2 ECG record")
    key = (integer(session, 1, 0xFFFFFFFF), integer(run, 0, 0xFFFFFFFF))
    if kind == "R" and fields == ["1"] and key[1]:
        return kind, key, 0, None
    afe_text, *fields = fields
    afe = integer(afe_text, 0 if kind == "E" else 1, 3)
    if not key[1] and kind != "E":
        raise ValueError("missing capture number")
    if kind == "C" and len(fields) == 6:
        mode, chip, launch, ratio, delay, flag = fields
        part = integer(chip, high=0xFFFFFF, base=16) & 0xF03000
        if mode != "1" or part not in (0x501000, 0x503000):
            raise ValueError("unexpected ECG mode or chip ID")
        integer(launch)
        timing_window = SPI_TIMING_WINDOW_US if integer(flag, 0, 1) else 0
        if ratio != "1":
            raise ValueError("expected FIFO-position timestamps")
        return kind, key, afe, (integer(delay, 0, 32768), timing_window)
    if kind == "A" and len(fields) == 2:
        return kind, key, afe, (integer(fields[0], 1), integer(fields[1]))
    if kind == "D" and len(fields) == 2:
        index = integer(fields[0])
        words = fields[1].split(";")
        if not 1 <= len(words) <= 16:
            raise ValueError("invalid sample batch length")
        samples = []
        for word in words:
            adc, tag = word.split(":")
            samples.append((integer(adc, -131072, 131071), integer(tag, 0, 3)))
        return kind, key, afe, (index, samples)
    if kind == "S" and len(fields) == 4:
        status = integer(fields[0], high=0xFFFFFF, base=16)
        integer(fields[1])
        return kind, key, afe, (status, integer(fields[2]), integer(fields[3]))
    if kind == "T" and len(fields) == 5:
        return kind, key, afe, None  # Optional firmware IRQ diagnostics.
    if kind == "E" and len(fields) == 2:
        integer(fields[1])
        return kind, key, afe, fields[0]
    raise ValueError("unknown ECG record")


def accept_record(capture, record, received_at):
    kind, key, afe, data = record
    if kind == "C":
        if key != capture.key:
            capture.key = key
            capture.channels.clear()
            capture.ready = False
            capture.problem = None
        if capture.ready or afe in capture.channels:
            capture.problem = "Duplicate AFE configuration"
        else:
            clock = Clock(1, *data)
            reset_clock(clock)
            capture.channels[afe] = Channel(clock)
    elif kind == "E":
        if data == "MODE_CHANGE":
            capture.ready = False
            capture.status = "Waiting for a new capture…"
        else:
            capture.problem = f"AFE {afe}: {data}" if afe else data
    elif key == capture.key:
        if kind == "R":
            capture.ready = set(capture.channels) == {1, 2, 3}
            if not capture.ready:
                capture.problem = "Capture started before all three AFEs reported"
        elif kind == "S":
            status, lost_irq, lost_tx = data
            if status & ((1 << 22) | (1 << 8)) or lost_irq or lost_tx:
                capture.problem = f"AFE {afe}: FIFO, clock, or transport fault"
        elif kind in ("A", "D") and capture.ready:
            channel = capture.channels[afe]
            if kind == "A":
                if add_anchor(channel.clock, *data):
                    channel.last_anchor_rx = received_at
            else:
                first, samples = data
                if first != channel.next_index:
                    capture.problem = f"AFE {afe}: sample sequence gap"
                    return
                for index, (value, tag) in enumerate(samples, first):
                    channel.samples.append((index, value if tag in (0, 2) else None))
                channel.next_index = first + len(samples)
                channel.last_sample_rx = received_at


def feed_bytes(capture, data, received_at):
    """Keep partial lines across reads. Packet arrival time only checks freshness."""
    for byte in data:
        if byte == 10:
            if not capture.discarding_line:
                try:
                    line = capture.pending.decode("ascii").strip()
                    if line and not line.startswith(("INFO:", "Select ")):
                        accept_record(capture, parse_record(line), received_at)
                except (UnicodeDecodeError, ValueError) as error:
                    capture.problem = f"Invalid USB record: {error}"
            capture.pending.clear()
            capture.discarding_line = False
        elif not capture.discarding_line:
            capture.pending.append(byte)
            if len(capture.pending) > 1024:
                capture.pending.clear()
                capture.discarding_line = True
                capture.problem = "Oversized USB record"


def aligned_snapshot(capture, now):
    """Return three traces on the same time grid, with gaps preserved."""
    if capture.problem:
        return (), (), capture.problem
    if not capture.ready:
        return (), (), capture.status
    channels = [capture.channels[afe] for afe in range(1, 4)]
    if any(
        stamp and now - stamp > 1
        for ch in channels
        for stamp in (ch.last_anchor_rx, ch.last_sample_rx)
    ):
        return (), (), "Alignment unavailable: timing or samples stopped"
    if not all(clock_ready(ch.clock) and ch.samples for ch in channels):
        return (), (), "Collecting timing and samples from all three AFEs…"

    points = [
        [(i, sample_time_us(ch.clock, i), value) for i, value in ch.samples]
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
    x = [(i - last) / RATE for i in ticks]
    y = []
    for trace, sign in zip(points, POLARITY):
        values = (interpolate(trace, i * SAMPLE_US) for i in ticks)
        y.append([nan if value is None else sign * value for value in values])
    if any(ch.clock.timing_window_us for ch in channels):
        return x, y, "Aligned estimate from SPI FIFO timing"
    return x, y, "Aligned estimate · verify timing on your PCB"


def show_plot(port, command):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    capture = Capture()
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 6))
    lines = []
    for ax, lead in zip(axes, LEADS):
        (line,) = ax.plot([], [], linewidth=1)
        lines.append(line)
        ax.set(title=f"Lead {lead}", ylabel="ADC counts", xlim=(-WINDOW, 0))
        ax.grid(True)
    axes[-1].set_xlabel("Common time (seconds before the latest aligned point)")
    status = fig.suptitle(capture.status)
    fig.tight_layout()
    started = time.monotonic()

    def update(_):
        now = time.monotonic()
        if not capture.link_failed:
            try:
                feed_bytes(capture, port.read(min(port.in_waiting, 16384)), now)
            except OSError as error:
                capture.link_failed = True
                capture.problem = f"Nano USB connection lost: {error}"
        x, y, message = aligned_snapshot(capture, now)
        if capture.key is None and not capture.problem and now - started > 8:
            message = "No capture reply. Check the USB port and Nano firmware."
        status.set_text(message)
        for i, (ax, line) in enumerate(zip(axes, lines)):
            line.set_data(x, y[i] if y else [])
            if y:
                ax.relim()
                ax.autoscale_view(scalex=False)
        return (*lines, status)

    time.sleep(0.1)
    port.reset_input_buffer()
    port.write((command + "\n").encode("ascii"))
    animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
    plt.show()
    return animation


def main():
    import serial

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "port", help="PCB Nano USB port, e.g. /dev/cu.usbmodem101 on macOS"
    )
    args = parser.parse_args()
    while True:
        command = input("Type go, yes, or y to start ECG: ").strip().lower()
        if command in ("go", "yes", "y"):
            break
    with serial.Serial(args.port, 115200, timeout=0, write_timeout=1) as port:
        try:
            show_plot(port, command)
        finally:
            try:
                port.dtr = False
                port.rts = False
            except OSError:
                pass


if __name__ == "__main__":
    main()
