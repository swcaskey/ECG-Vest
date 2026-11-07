import math
import random
import unittest

from ecg.acquisition import Acquisition
from ecg.protocol import MAX_LINE, Parser, ProtocolMismatch, decode_fifo


def config(session=123, epoch=1, mode=1, associated=0):
    return (
        f"E2,C,{session},{epoch},1,{mode},513742,1000000,4,4906,{associated}\n"
        f"E2,C,{session},{epoch},2,{mode},513742,1000000,4,4906,{associated}\n"
        f"E2,R,{session},{epoch},{mode}\n"
    ).encode()


def anchors(engine, epoch=1, session=123, start=0, count=20):
    for n in range(start, start + count):
        for board in (1, 2):
            engine.feed(
                f"E2,A,{session},{epoch},{board},{1+n*32},{1000000+round((1+n*32)*1953.125)}\n".encode()
            )


class ProtocolTests(unittest.TestCase):
    def test_pre_acquisition_events_do_not_corrupt_startup(self):
        engine = Acquisition()
        engine.feed(b"E2,E,123,0,0,MODE_CHANGE,200\n" + config())
        self.assertTrue(engine.ready)
        self.assertTrue(engine.valid)
        self.assertEqual(engine.errors, 0)

    def test_signed_boundaries_and_tags(self):
        for value in (-131072, -1, 0, 1, 131071):
            for tag in range(8):
                word = ((value & 0x3FFFF) << 6) | (tag << 3) | 7
                decoded, actual_tag = decode_fifo(word)
                self.assertEqual(actual_tag, tag)
                self.assertEqual(decoded, value if tag in (0, 2) else None)

    def test_fragmentation_and_multiple_records(self):
        data = config() + b"E2,D,123,1,1,0,-131072:0;131071:2\n"
        reference = Parser().feed(data)
        for size in (1, 2, 7, 103, len(data)):
            parser = Parser()
            actual = []
            for begin in range(0, len(data), size):
                actual.extend(parser.feed(data[begin : begin + size]))
            self.assertEqual(actual, reference)
            self.assertFalse(parser.errors)

    def test_malformed_oversized_unknown(self):
        for bad in (
            b"E2,D,123,1,1,0,x:0",
            b"E2,D,123,1,1,0,131072:0",
            b"E2,Q,123,1,1,0",
            b"\xff",
            b"x" * (MAX_LINE + 10),
        ):
            parser = Parser()
            records = parser.feed(bad + b"\n" + config())
            self.assertTrue(parser.errors)
            self.assertEqual(len(records), 3)
            self.assertLessEqual(len(parser.pending), MAX_LINE)
        parser = Parser()
        parser.feed(b"x" * 100_000)
        self.assertEqual(len(parser.pending), 0)

    def test_version_mismatch(self):
        for wire in (b"ECG:7\n", b"E3,R,1,1,0\n"):
            with self.assertRaisesRegex(ProtocolMismatch, "two-board"):
                Parser().feed(wire)


class AcquisitionTests(unittest.TestCase):
    def test_engine_common_grid_sum_against_independent_waveform_truth(self):
        engine = Acquisition(True)
        engine.feed(config())
        signals = (
            lambda t: 600 * math.sin(2 * math.pi * 5 * t),
            lambda t: -200 * math.cos(2 * math.pi * 3 * t),
        )
        truth_latest = 0
        for n in range(768):
            for board, rate, offset in ((1, 128, 1.0), (2, 128.0256, 1.0027)):
                if n % 8 == 0:
                    pulse = 4 * n + 1
                    stamp = round((offset + pulse / (rate * 4)) * 1e6)
                    engine.feed(f"E2,A,123,1,{board},{pulse},{stamp}\n".encode())
                stamp = offset + ((n + 1) - 4906 / 256) / rate
                truth_latest = max(truth_latest, stamp)
                voltage = round(signals[board - 1](stamp))
                engine.feed(f"E2,D,123,1,{board},{n},{voltage}:0\n".encode())
        _, (xs, ys), _ = engine.snapshot()
        self.assertGreater(len(xs), 500)
        self.assertTrue(all(math.isfinite(y) for y in ys))
        error = max(
            abs(y - sum(signal(x + truth_latest) for signal in signals))
            for x, y in zip(xs, ys)
        )
        self.assertLess(error, 4)
        channel = engine.channels[1]
        seq, _ = channel.samples[-100]
        channel.samples[-100] = (seq, None)
        self.assertTrue(any(math.isnan(y) for y in engine.snapshot()[1][1]))

    def test_ready_requires_both_boards(self):
        engine = Acquisition()
        engine.feed(b"E2,C,123,1,1,0,513742,1000000,4,4906,0\nE2,R,123,1,0\n")
        self.assertFalse(engine.ready)
        self.assertFalse(engine.valid)

    def test_mode_switch_and_old_session(self):
        engine = Acquisition()
        engine.feed(config())
        engine.feed(b"E2,D,123,1,1,0,10:0;11:2\n")
        engine.feed(config(epoch=2, mode=0))
        self.assertFalse(engine.channels[1].samples)
        self.assertEqual(engine.mode, 0)
        self.assertFalse(engine.channels[1].clock.ready)
        engine.feed(config() + b"E2,D,123,1,1,2,12:2\n")
        self.assertEqual(engine.key, (123, 2))
        self.assertFalse(engine.channels[1].samples)
        engine.feed(config(session=456))
        engine.feed(config(epoch=2))
        self.assertEqual(engine.key, (456, 1))

    def test_settling_keeps_position_and_time(self):
        engine = Acquisition()
        engine.feed(config())
        engine.feed(b"E2,D,123,1,1,0,10:0;77:1;20:0;99:3\n")
        self.assertEqual(
            list(engine.channels[1].samples), [(0, 10), (1, None), (2, 20), (3, None)]
        )
        points = engine.channels[1].points(engine.now_us)
        self.assertAlmostEqual(points[2][1] - points[0][1], 2e6 / 128)

    def test_default_sum_gated_and_optional_estimate(self):
        engine = Acquisition()
        engine.feed(config())
        anchors(engine)
        for board in (1, 2):
            values = ";".join(f"{board*100}:0" for _ in range(8))
            engine.feed(f"E2,D,123,1,{board},0,{values}\n".encode())
        self.assertTrue(all(c.clock.ready for c in engine.channels.values()))
        self.assertEqual(engine.snapshot()[1], ([], []))
        self.assertIn("unvalidated", engine.status())
        engine.allow_unvalidated = True
        xs, ys = engine.snapshot()[1]
        self.assertTrue(xs)
        self.assertTrue(all(abs(y - 300) < 1e-8 for y in ys))
        engine.now_us += 2_000_000
        self.assertEqual(engine.snapshot()[1], ([], []))
        self.assertIn("stale", engine.status())

    def test_gap_faults_and_recovery(self):
        for code in (
            "FIFO_OVERFLOW",
            "TIMING_MISSED",
            "IRQ_QUEUE_OVERFLOW",
            "PLL_UNLOCK",
            "RESERVED_TAG",
            "TX_QUEUE_OVERFLOW",
        ):
            engine = Acquisition(True)
            engine.feed(config())
            anchors(engine)
            engine.feed(f"E2,E,123,1,1,{code},2300000\n".encode())
            self.assertFalse(engine.valid)
            self.assertEqual(engine.snapshot()[1], ([], []))
            engine.feed(config(epoch=2))
            anchors(engine, epoch=2)
            self.assertTrue(engine.valid)
            self.assertTrue(engine.channels[1].clock.ready)
        for wire in (
            b"E2,D,123,1,1,2,3:2\n",
            b"E2,D,123,1,1,0,0:7\n",
            b"E2,S,123,1,1,000100,20,0,0\n",
            b"E2,D,123,1,1,0,bad:0\n",
        ):
            engine = Acquisition(True)
            engine.feed(config())
            engine.feed(wire)
            self.assertFalse(engine.valid)

    def test_loss_counts_and_invalid_mode(self):
        engine = Acquisition()
        engine.feed(config())
        engine.feed(b"E2,S,123,1,1,000000,32,0,0\nE2,S,123,1,1,000000,64,1,0\n")
        self.assertFalse(engine.valid)
        engine.feed(config(epoch=2))
        engine.feed(b"E2,E,123,2,0,INVALID_MODE,2000000\n")
        self.assertTrue(engine.valid)

    def test_transport_batching_does_not_change_acquisition_times(self):
        wire = config()
        for i in range(24):
            for board in (1, 2):
                wire += f"E2,A,123,1,{board},{i*32+1},{1000000+round((i*32+1)*1953.125)}\n".encode()
                wire += f"E2,D,123,1,{board},{i},{i}:0\n".encode()
        expected = Acquisition(True)
        expected.feed(wire)
        actual = Acquisition(True)
        rng = random.Random(71)
        start = 0
        while start < len(wire):
            end = start + rng.randint(1, 1300)
            actual.feed(wire[start:end])
            start = end
        self.assertEqual(expected.diagnostics(), actual.diagnostics())
        self.assertEqual(expected.snapshot(), actual.snapshot())


if __name__ == "__main__":
    unittest.main()
