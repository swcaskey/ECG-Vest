"""Estimate sample times from PCB FIFO timestamps."""

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from math import fsum, isfinite, sqrt
from statistics import median

RATE = 128
SAMPLE_US = 1_000_000 / RATE
SPI_TIMING_WINDOW_US = 2_000  # Firmware discards wider STATUS brackets.
WINDOW = 5


@dataclass
class Clock:
    latency_ticks: int
    timing_window_us: int = 0
    rejected: int = 0


def reset_clock(clock):
    clock.anchors = deque(maxlen=256)
    clock._fit_ready = False
    clock._established = False
    clock._quality = deque(maxlen=64)
    clock.period_us = SAMPLE_US
    clock.residual_us = float("inf")
    clock._center_position = 0.0
    clock._center_us = 0.0
    clock._startup_rejections = 0


def clock_ready(clock):
    return clock._fit_ready and (
        len(clock._quality) < 8 or sum(clock._quality) >= 0.8 * len(clock._quality)
    )


def predict_time(clock, position):
    return clock._center_us + (position - clock._center_position) * clock.period_us


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
    if abs(slope / SAMPLE_US - 1) > 0.02:
        clock._fit_ready = False
        return
    clock.rejected += len(points) - len(inliers)
    clock.anchors = deque(inliers, maxlen=256)
    clock._center_position, clock._center_us = center_p, center_t
    clock.period_us = slope
    clock.residual_us = sqrt(
        fsum((t - predict_time(clock, p)) ** 2 for p, t in inliers) / len(inliers)
    )
    clock._fit_ready = (
        len(inliers) >= 8
        and inliers[-1][1] - inliers[0][1] >= 500_000
        and clock.residual_us <= max(250, clock.timing_window_us / 2)
    )


def add_anchor(clock, position, time_us):
    """Add a FIFO timestamp if it agrees with the clock fit."""
    accepted = accept_anchor(clock, position, time_us)
    clock._quality.append(accepted)
    if clock_ready(clock):
        clock._established = True
    return accepted


def accept_anchor(clock, position, time_us):
    if (
        not isinstance(position, int)
        or position < 0
        or not isinstance(time_us, (int, float))
        or not isfinite(time_us)
        or time_us < 0
    ):
        clock.rejected += 1
        return False
    if clock.anchors:
        last_p, last_t = clock.anchors[-1]
        if position <= last_p or time_us <= last_t:
            clock.rejected += 1
            return False
        nominal_delta = (position - last_p) * SAMPLE_US
        interval_tolerance = max(150.0, nominal_delta * 0.025) + clock.timing_window_us
        if abs((time_us - last_t) - nominal_delta) > interval_tolerance:
            clock.rejected += 1
            clock._startup_rejections += 1
            if clock._established or clock._startup_rejections < 3:
                return False
            # Recover from a bad first timestamp before the fit is established.
            reset_clock(clock)
        if clock._established and abs(time_us - predict_time(clock, position)) > max(
            150.0, clock.timing_window_us, 8 * clock.residual_us
        ):
            clock.rejected += 1
            return False
    clock._startup_rejections = 0
    clock.anchors.append((position, time_us))
    fit_clock(clock)
    return bool(clock.anchors and clock.anchors[-1] == (position, time_us))


def sample_time_us(clock, sample_index):
    """Estimate input time for FIFO position n, including settling entries.

    FIFO positions are one-based. Subtract the digital filter delay.
    """
    if not clock_ready(clock):
        raise ValueError("Clock model is not ready")
    return (
        predict_time(clock, sample_index + 1)
        - clock.latency_ticks * clock.period_us / 256
    )


def interpolate(points, time_us, max_gap_us=1.5 * SAMPLE_US):
    """Interpolate between consecutive valid samples in one capture."""
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
