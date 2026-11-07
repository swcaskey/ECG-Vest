"""Fit SAMP clocks and interpolate valid samples without bridging gaps.

FIFO-to-pulse phase is unvalidated; a good clock fit alone cannot enable Lead II.
"""

from bisect import bisect_left
from collections import deque
from math import fsum, isfinite, sqrt
from statistics import median


class ClockModel:
    """Fit MCU microseconds against each board's SAMP pulse numbers.

    Ratio comes from firmware; observed SAMP/FIFO rate is 1, phase unvalidated.
    LPF latency: 4906 master-clock ticks at 128 sps (p19 Table 4).
    """

    def __init__(self, ratio=4, latency_ticks=4906):
        if ratio <= 0 or latency_ticks < 0:
            raise ValueError("Invalid pulse ratio or filter latency")
        self.ratio = ratio
        self.latency_ticks = latency_ticks
        self.rejected = 0
        self.invalidate()

    def invalidate(self):
        self.anchors = deque(maxlen=256)
        self._fit_ready = False
        self._established = False
        self._quality = deque(maxlen=64)
        self.pulse_period_us = 1e6 / (128 * self.ratio)
        self.period_us = 1e6 / 128
        self.residual_us = float("inf")
        self.latest_us = None
        self._center_pulse = 0.0
        self._center_us = 0.0
        self._startup_rejections = 0

    @property
    def ready(self):
        # A low-residual fit to a cherry-picked minority is not a good clock.
        return self._fit_ready and (
            len(self._quality) < 8 or sum(self._quality) >= 0.8 * len(self._quality)
        )

    def _predict_pulse(self, pulse):
        return self._center_us + (pulse - self._center_pulse) * self.pulse_period_us

    @staticmethod
    def _least_squares(points):
        # Center both axes before multiplication; esp_timer is a large int64.
        origin_p, origin_t = points[0]
        xs = [p - origin_p for p, _ in points]
        ys = [t - origin_t for _, t in points]
        mx = fsum(xs) / len(xs)
        my = fsum(ys) / len(ys)
        variance = fsum((x - mx) ** 2 for x in xs)
        slope = fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / variance
        return origin_p + mx, origin_t + my, slope

    def _fit(self):
        points = list(self.anchors)
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
        threshold = max(100.0, 8 * 1.4826 * mad)
        inliers = [
            point
            for point, residual in zip(points, offsets)
            if abs(residual - offset) <= threshold
        ]
        if len(inliers) < 3:
            self._fit_ready = False
            return
        center_p, center_t, slope = self._least_squares(inliers)
        output_period = slope * self.ratio
        plausible = abs(output_period / (1e6 / 128) - 1) <= 0.02
        if not plausible:
            self._fit_ready = False
            return
        self.rejected += len(points) - len(inliers)
        self.anchors = deque(inliers, maxlen=256)
        self.latest_us = inliers[-1][1]
        self._center_pulse, self._center_us = center_p, center_t
        self.pulse_period_us = slope
        self.period_us = output_period
        self.residual_us = sqrt(
            fsum((t - self._predict_pulse(p)) ** 2 for p, t in inliers) / len(inliers)
        )
        # Engineering quality gate, separate from the unproven pulse/FIFO
        # association: low residuals alone still do not validate sample phase.
        self._fit_ready = (
            len(inliers) >= 8
            and inliers[-1][1] - inliers[0][1] >= 500_000
            and self.residual_us <= 250
        )

    def add(self, pulse, time_us):
        """Accept a valid timing anchor; return False when rejected.

        Pulse numbers preserve batching gaps; the MCU detects missing GPIO edges.
        """
        accepted = self._add(pulse, time_us)
        self._quality.append(accepted)
        if self.ready:
            self._established = True
        return accepted

    def _add(self, pulse, time_us):
        if (
            not isinstance(pulse, int)
            or pulse < 0
            or not isinstance(time_us, (int, float))
            or not isfinite(time_us)
            or time_us < 0
        ):
            self.rejected += 1
            return False
        if self.anchors:
            last_p, last_t = self.anchors[-1]
            if pulse <= last_p or time_us <= last_t:
                self.rejected += 1
                return False
            nominal_delta = (pulse - last_p) * 1e6 / (128 * self.ratio)
            if abs((time_us - last_t) - nominal_delta) > max(
                150.0, nominal_delta * 0.025
            ):
                self.rejected += 1
                self._startup_rejections += 1
                if self._established or self._startup_rejections < 3:
                    return False
                # Before any usable fit exists, a single corrupt first
                # timestamp must not poison every subsequent interval check.
                # Keep explicit pulse numbers; do not invent/reset their phase.
                self.invalidate()
            if self._established and abs(time_us - self._predict_pulse(pulse)) > max(
                150.0, 8 * self.residual_us
            ):
                self.rejected += 1
                return False
        self._startup_rejections = 0
        self.anchors.append((pulse, time_us))
        self.latest_us = time_us
        self._fit()
        return bool(self.anchors and self.anchors[-1] == (pulse, time_us))

    def time_us(self, sample_index):
        """Estimate input time for FIFO position n, including settling entries.

        Assumes pulse ratio*(n+1), minus LPF delay; startup phase is UNVALIDATED.
        """
        if not self.ready:
            raise ValueError("Clock model is not ready")
        return (
            self._predict_pulse((sample_index + 1) * self.ratio)
            - self.latency_ticks * self.period_us / 256
        )

    def stale(self, now_us, max_age=1_000_000):
        return (
            not self.ready
            or self.latest_us is None
            or now_us < self.latest_us
            or now_us - self.latest_us > max_age
        )

    @staticmethod
    def interpolate(points, time_us, max_gap_us=1.5e6 / 128):
        return interpolate(points, time_us, max_gap_us)


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
