"""Coordinates and time windows for the ECG vector displays."""

from bisect import bisect_left, bisect_right
from math import isfinite, nan, sqrt

SQRT_THREE = sqrt(3.0)


def _finite_point(values):
    return all(value is not None and isfinite(value) for value in values)


def frontal_coordinates(traces):
    """Project limb leads onto an idealized frontal plane, in ADC counts.

    Lead I points left and Lead II points 60 degrees below it in this model.
    The separately measured Lead III stays available for consistency checks.
    """
    lead_i, lead_ii, _ = traces
    horizontal = []
    vertical = []
    for i, ii in zip(lead_i, lead_ii, strict=True):
        valid = _finite_point((i, ii))
        horizontal.append(i if valid else nan)
        vertical.append((2 * ii - i) / SQRT_THREE if valid else nan)
    return horizontal, vertical


def einthoven_residual(traces):
    """Return Lead II − (Lead I + Lead III), in ADC counts."""
    return [
        ii - (i + iii) if _finite_point((i, ii, iii)) else nan
        for i, ii, iii in zip(*traces, strict=True)
    ]


def trailing_indices(times, axes, cursor, seconds=1.0, *, max_gap=None):
    """Find the latest contiguous, valid samples at or before the cursor.

    Timestamps are ascending and all coordinate arrays share that time base.
    When supplied, max_gap bounds the interval between consecutive timestamps.
    """
    if not isfinite(cursor):
        return range(0)
    stop = bisect_right(times, cursor)
    start = bisect_left(times, cursor - seconds, hi=stop)
    for index in range(stop - 1, start - 1, -1):
        if not _finite_point(axis[index] for axis in axes):
            return range(index + 1, stop)
        if (
            index + 1 < stop
            and max_gap is not None
            and times[index + 1] - times[index] > max_gap
        ):
            return range(index + 1, stop)
    return range(start, stop)
