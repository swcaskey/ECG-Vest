"""Numerical truth is generated from independent clock/signal equations."""

import math
import random
import unittest

from ecg.timing import ClockModel, interpolate


def anchors(fs, duration=20, offset=0, jitter=0, seed=7, step=32):
    rng = random.Random(seed)
    count = int(duration * fs * 4)
    return [
        (p, offset + p * 1e6 / (4 * fs) + rng.uniform(-jitter, jitter))
        for p in range(0, count + 1, step)
    ]


def feed(model, records):
    for pulse, stamp in records:
        model.add(pulse, stamp)
    return model


class ClockTests(unittest.TestCase):
    def test_observed_one_pulse_per_sample_ratio(self):
        model = ClockModel(ratio=1)
        for pulse in range(1, 2562, 32):
            model.add(pulse, 1_000_000 + pulse * 1e6 / 128)
        self.assertTrue(model.ready)
        self.assertAlmostEqual(1e6 / model.period_us, 128)
        self.assertAlmostEqual(
            model.time_us(100), 1_000_000 + (101 - 4906 / 256) / 128 * 1e6
        )

    def test_known_initial_offset_positive_negative_and_exaggerated_drift(self):
        for ppm in (0, 90, -120, 10_000, -10_000):
            with self.subTest(ppm=ppm):
                fs1 = 128.0
                fs2 = fs1 * (1 + ppm / 1e6)
                first = feed(ClockModel(), anchors(fs1, offset=10**12))
                third = feed(ClockModel(), anchors(fs2, offset=10**12 + 2345))
                self.assertTrue(first.ready and third.ready)
                measured_ppm = 1e6 * (first.period_us / third.period_us - 1)
                self.assertAlmostEqual(measured_ppm, ppm, delta=0.001)
                for index in (0, 512, 2500):
                    # Derived independently from physical sample-rate units.
                    truth1 = 10**12 + ((index + 1) / fs1 - 4906 / (fs1 * 256)) * 1e6
                    truth2 = (
                        10**12 + 2345 + ((index + 1) / fs2 - 4906 / (fs2 * 256)) * 1e6
                    )
                    self.assertAlmostEqual(first.time_us(index), truth1, delta=0.001)
                    self.assertAlmostEqual(third.time_us(index), truth2, delta=0.001)

    def test_jitter_outlier_and_large_microsecond_clock(self):
        model = ClockModel()
        records = anchors(128.0128, offset=10**12, jitter=12)
        records[140] = (records[140][0], records[140][1] + 800)
        feed(model, records)
        self.assertTrue(model.ready)
        self.assertGreater(model.rejected, 0)
        self.assertLess(model.residual_us, 15)
        self.assertAlmostEqual(1e6 / model.period_us, 128.0128, delta=0.002)

    def test_corrupt_first_timestamp_does_not_poison_startup(self):
        model = ClockModel()
        records = anchors(128, offset=1_000_000)
        records[0] = (records[0][0], records[0][1] + 10_000)
        feed(model, records)
        self.assertTrue(model.ready)
        self.assertGreaterEqual(model.rejected, 3)
        self.assertAlmostEqual(
            model.time_us(300), 1_000_000 + (301 - 4906 / 256) / 128 * 1e6, delta=0.001
        )

    def test_excessive_jitter_never_qualifies_as_ready(self):
        model = ClockModel()
        for k in range(320):
            pulse = k * 32
            stamp = 1_000_000 + pulse / 512 * 1e6 + (400 if k % 2 else -400)
            model.add(pulse, stamp)
        # A robust fit can select one half of this alternating jitter and have
        # low residuals. Its rejection fraction must still block readiness.
        self.assertGreater(model.rejected, 100)
        self.assertFalse(model.ready)
        self.assertTrue(model.stale(model.latest_us))

    def test_high_rms_residual_blocks_otherwise_plausible_fit(self):
        model = ClockModel()
        rng = random.Random(11)
        for k in range(320):
            model.add(k * 32, 1_000_000 + k / 16 * 1e6 + rng.uniform(-600, 600))
        self.assertEqual(model.rejected, 0)
        self.assertGreater(model.residual_us, 250)
        self.assertFalse(model.ready)

    def test_rejected_anchors_do_not_refresh_stale_model(self):
        model = feed(ClockModel(), anchors(128, duration=2, offset=1_000_000))
        last_p, last_t = model.anchors[-1]
        for k in range(1, 25):
            self.assertFalse(model.add(last_p + k * 32, last_t + k / 16 * 1e6 + 2000))
        self.assertEqual(model.latest_us, last_t)
        self.assertFalse(model.ready)
        self.assertTrue(model.stale(last_t + 1_500_000))
        self.assertGreaterEqual(model.rejected, 24)

    def test_fragmented_transport_arrival_delays_do_not_change_clock(self):
        records = anchors(127.9872, offset=700_000, jitter=3)
        immediate = feed(ClockModel(), records)
        delayed = ClockModel()
        # Packet batching and varying arrival delays do not enter add().
        start = 0
        rng = random.Random(4)
        while start < len(records):
            batch_size = rng.randint(1, 19)
            for pulse, stamp in records[start : start + batch_size]:
                simulated_arrival = stamp + rng.uniform(0, 500_000)
                self.assertGreaterEqual(simulated_arrival, stamp)
                delayed.add(pulse, stamp)
            start += batch_size
        self.assertEqual(immediate.period_us, delayed.period_us)
        self.assertEqual(immediate.time_us(900), delayed.time_us(900))

    def test_ten_minute_two_clock_run_remains_bounded(self):
        first = ClockModel()
        third = ClockModel()
        max_time_error = 0
        rng = random.Random(2)
        # 600s, 4 anchors/s; independent clocks differ by 175 ppm.
        for k in range(2401):
            p = k * 128
            t1 = 5_000_000 + p / 512 * 1e6
            t3 = 5_002_000 + p / (512 * 1.000175) * 1e6
            first.add(p, t1 + rng.uniform(-6, 6))
            third.add(p, t3 + rng.uniform(-6, 6))
            if k > 20:
                n = p // 4
                expected = 5_002_000 + ((n + 1) - 4906 / 256) / 128.0224 * 1e6
                max_time_error = max(max_time_error, abs(third.time_us(n) - expected))
        self.assertLessEqual(len(first.anchors), 256)
        self.assertLessEqual(len(third.anchors), 256)
        self.assertLess(max_time_error, 8)
        relative_ppm = 1e6 * (first.period_us / third.period_us - 1)
        self.assertAlmostEqual(relative_ppm, 175, delta=1)

    def test_rolling_model_tracks_continuously_changing_clock(self):
        model = ClockModel()
        base_rate = 512 * 1.0001
        acceleration = 512 * 0.8e-6  # frequency grows by 0.8 ppm/second
        last_t = 0
        for pulse in range(0, 307201, 64):
            # Solve p=f0*t + acceleration*t^2/2 independently.
            t = (
                2
                * pulse
                / (base_rate + math.sqrt(base_rate**2 + 2 * acceleration * pulse))
            )
            model.add(pulse, 2_000_000 + t * 1e6)
            last_t = t
        instantaneous_rate = (base_rate + acceleration * last_t) / 4
        self.assertTrue(model.ready)
        self.assertAlmostEqual(1e6 / model.period_us, instantaneous_rate, delta=0.003)

    def test_stale_invalid_inputs_recovery_and_wrong_pulse_ratio(self):
        model = feed(ClockModel(), anchors(128))
        self.assertFalse(model.stale(model.latest_us + 999_999))
        self.assertTrue(model.stale(model.latest_us + 1_000_001))
        self.assertTrue(model.stale(model.latest_us - 1))
        self.assertFalse(model.add(1, 1))  # reordered sequence/time
        self.assertFalse(model.add(-1, 2))
        self.assertFalse(model.add(12000, float("nan")))
        model.invalidate()
        self.assertFalse(model.ready)
        with self.assertRaises(ValueError):
            model.time_us(0)
        feed(model, anchors(128, offset=30_000_000))
        self.assertTrue(model.ready)
        wrong_ratio = feed(ClockModel(), anchors(32))
        self.assertFalse(wrong_ratio.ready)

    def test_missing_pulse_association_is_rejected_not_silently_renumbered(self):
        model = feed(ClockModel(), anchors(128, duration=2))
        pulse, stamp = model.anchors[-1]
        # 33 physical cycles elapsed, but the ISR counter only advanced 32.
        self.assertFalse(model.add(pulse + 32, stamp + 33 / 512 * 1e6))
        self.assertGreater(model.rejected, 0)


class InterpolationTests(unittest.TestCase):
    def test_fitted_clocks_and_resampling_preserve_known_sum(self):
        clocks = [
            feed(ClockModel(), anchors(128.0, offset=1_000_000, jitter=4)),
            feed(ClockModel(), anchors(128.0256, offset=1_002_700, jitter=4, seed=9)),
        ]
        signals = [
            lambda t: 600 * math.sin(2 * math.pi * 5 * t),
            lambda t: -200 * math.cos(2 * math.pi * 3 * t),
        ]
        points = []
        for clock, fs, offset, signal in zip(
            clocks, (128, 128.0256), (1, 1.0027), signals
        ):
            channel = []
            for n in range(2600):
                true_t = offset + ((n + 1) - 4906 / 256) / fs
                channel.append((n, clock.time_us(n), signal(true_t)))
            points.append(channel)
        errors = []
        for k in range(256, 2432):
            t = k / 128
            estimated = interpolate(points[0], t * 1e6) + interpolate(
                points[1], t * 1e6
            )
            errors.append(estimated - sum(signal(t) for signal in signals))
        self.assertLess(max(abs(error) for error in errors), 5.5)

    def test_no_extrapolation_settling_missing_samples_or_large_time_gap(self):
        points = [(0, 0, 10), (1, 7812.5, 20), (2, 15625, None), (3, 23437.5, 40)]
        self.assertEqual(interpolate(points, 3906.25), 15)
        self.assertIsNone(interpolate(points, -1))
        self.assertIsNone(interpolate(points, 24000))
        self.assertIsNone(interpolate(points, 12000))
        self.assertIsNone(interpolate(points, 19000))
        self.assertIsNone(interpolate([(1, 0, 1), (3, 8000, 3)], 4000))
        self.assertIsNone(interpolate([(1, 0, 1), (2, 25000, 3)], 12000))
        self.assertIsNone(interpolate([], 0))

    def test_time_aligned_addition_of_different_shapes_against_independent_truth(self):
        rate1, rate3 = 128.0, 128.032
        shift1, shift3 = 0.0013, 0.0047

        def lead1(t):
            return 700 * math.sin(2 * math.pi * 5 * t)

        def lead3(t):
            # Different polarity, amplitudes and morphology; no peak matching.
            return -250 * math.cos(2 * math.pi * 3 * t) + 100 * math.sin(
                2 * math.pi * 11 * t
            )

        p1 = [
            (n, (shift1 + n / rate1) * 1e6, lead1(shift1 + n / rate1))
            for n in range(1300)
        ]
        p3 = [
            (n, (shift3 + n / rate3) * 1e6, lead3(shift3 + n / rate3))
            for n in range(1300)
        ]
        errors = []
        for k in range(128, 1152):
            t = k / 128
            actual = interpolate(p1, t * 1e6) + interpolate(p3, t * 1e6)
            truth = lead1(t) + lead3(t)
            errors.append(actual - truth)
        # Classical linear-interpolation curvature bound, independently derived.
        bound = (
            700 * (2 * math.pi * 5) ** 2 / rate1**2
            + (250 * (2 * math.pi * 3) ** 2 + 100 * (2 * math.pi * 11) ** 2) / rate3**2
        ) / 8
        self.assertLess(max(abs(x) for x in errors), bound)
        self.assertLess(math.sqrt(sum(x * x for x in errors) / len(errors)), 5)

    def test_band_edge_interpolation_error_is_characterized(self):
        frequency = 28.35
        points = [
            (n, n / 128 * 1e6, math.sin(2 * math.pi * frequency * n / 128))
            for n in range(256)
        ]
        errors = []
        for n in range(255):
            t = (n + 0.5) / 128
            errors.append(
                abs(
                    interpolate(points, t * 1e6) - math.sin(2 * math.pi * frequency * t)
                )
            )
        # Maximum midpoint attenuation for a sampled sine is 1-cos(pi*f/fs).
        bound = 1 - math.cos(math.pi * frequency / 128)
        self.assertLessEqual(max(errors), bound + 1e-12)
        self.assertGreater(max(errors), 0.22)  # not a hidden full-band accuracy claim


if __name__ == "__main__":
    unittest.main()
