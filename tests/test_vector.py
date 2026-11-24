from math import inf, isnan, nan, sqrt
import unittest

from ecg.vector import einthoven_residual, frontal_coordinates, trailing_indices


class VectorTests(unittest.TestCase):
    def test_frontal_axes_point_left_and_inferior(self):
        traces = ([10, 0], [5, 10 * sqrt(3) / 2], [-5, 10 * sqrt(3) / 2])
        x, y = frontal_coordinates(traces)
        self.assertEqual(x, [10, 0])
        self.assertAlmostEqual(y[0], 0)
        self.assertAlmostEqual(y[1], 10)

    def test_missing_input_breaks_coordinate_pair(self):
        x, y = frontal_coordinates(([5, nan, 5], [2.5, 2.5, inf], [0, 0, 0]))
        self.assertEqual((x[0], y[0]), (5, 0))
        self.assertTrue(all(isnan(value) for value in x[1:] + y[1:]))

    def test_residual_preserves_invalid_samples(self):
        residual = einthoven_residual(([10, 10, 10], [20, 21, 22], [10, 10, nan]))
        self.assertEqual(residual[:2], [0, 1])
        self.assertTrue(isnan(residual[2]))

    def test_trail_respects_cursor_and_duration(self):
        times = [0, 0.5, 1.0, 1.5, 2.0]
        self.assertEqual(
            trailing_indices(times, ([10] * 5,), 1.75, seconds=1.0), range(2, 4)
        )
        self.assertEqual(trailing_indices(times, ([10] * 5,), -1), range(0))

    def test_trail_does_not_cross_missing_values_or_timestamp_gap(self):
        times = [0, 0.125, 0.25, 0.5, 0.625, 0.75]
        self.assertEqual(
            trailing_indices(times, ([10] * 6, [10, nan, 10, 10, 10, 10]), 0.25),
            range(2, 3),
        )
        self.assertEqual(
            trailing_indices(times, ([10, nan, 10, 10, 10, 10],), 0.125),
            range(2, 2),
        )
        self.assertEqual(
            trailing_indices(times, ([10] * 6,), 0.75, max_gap=0.1875), range(3, 6)
        )
