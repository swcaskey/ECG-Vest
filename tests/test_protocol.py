import math
import random
import unittest

from ecg.acquisition import Acquisition
from ecg.protocol import MAX_LINE, Parser, ProtocolMismatch, decode_fifo


def config(session=123, epoch=1, mode=1, associated=0, ratio=4):
    return (
        f"E2,C,{session},{epoch},1,{mode},513742,1000000,{ratio},4906,{associated}\n"
        f"E2,C,{session},{epoch},2,{mode},513742,1000000,{ratio},4906,{associated}\n"
        f"E2,R,{session},{epoch},{mode}\n"
    ).encode()


def anchors(engine, epoch=1, session=123, start=0, count=20, ratio=4):
    for n in range(start, start + count):
        for board in (1, 2):
            engine.feed(
                f"E2,A,{session},{epoch},{board},{1+n*32},{1000000+round((1+n*32)*1e6/(128*ratio))}\n".encode()
            )


def voltage_samples(engine, session=123, epoch=1, first=0, count=8):
    for board in (1, 2):
        values = ";".join(f"{board * 100}:0" for _ in range(count))
        engine.feed(f"E2,D,{session},{epoch},{board},{first},{values}\n".encode())


def stream_samples(
    engine, session=123, epoch=1, start=0, count=256, ratio=1, boards=(1, 2)
):
    for first in range(start, start + count, 8):
        for board in (1, 2):
            if first == 0 or first * ratio % 32 == 0:
                pulse = first * ratio if first else 1
                stamp = 1000000 + round(pulse * 1e6 / (128 * ratio))
                engine.feed(
                    f"E2,A,{session},{epoch},{board},{pulse},{stamp}\n".encode()
                )
            if board in boards:
                values = ";".join(f"{board * 100}:0" for _ in range(8))
                engine.feed(
                    f"E2,D,{session},{epoch},{board},{first},{values}\n".encode()
                )


class ProtocolTests(unittest.TestCase):
    def test_timing_context_signed_interval_and_limits(self):
        wire = b"E2,T,123,1,1,50360,561084432,-181,50352,0\n"
        for size in (1, 7, len(wire)):
            parser = Parser()
            records = []
            for i in range(0, len(wire), size):
                records.extend(parser.feed(wire[i : i + size]))
            self.assertFalse(parser.errors)
            self.assertEqual(records[0].kind, "T")
            self.assertEqual(records[0].fields, (50360, 561084432, -181, 50352, 0))
        for bad in (
            b"E2,T,123,1,0,1,1,1,1,0\n",
            b"E2,T,123,1,1,0,1,1,1,0\n",
            b"E2,T,123,1,1,1,1,1,1,4294967296\n",
        ):
            parser = Parser()
            parser.feed(bad)
            self.assertTrue(parser.errors)

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
    def test_fifo_threshold_anchors_use_known_output_sample_positions(self):
        engine = Acquisition(lead_i_board=2, bias_board=2)
        engine.feed(config(associated=1, ratio=1))
        for first in range(0, 256, 8):
            for board in (1, 2):
                count = first + 8
                stamp = 1000000 + round(count * 1e6 / 128)
                engine.feed(f"E2,A,123,1,{board},{count},{stamp}\n".encode())
                values = ";".join(f"{board * 100}:0" for _ in range(8))
                engine.feed(f"E2,D,123,1,{board},{first},{values}\n".encode())
        self.assertTrue(engine.health()["healthy"])
        self.assertTrue(engine.health()["timing_validated"])
        self.assertEqual(engine.channels[1].clock.anchors[0][0], 8)
        self.assertAlmostEqual(
            engine.diagnostics()["timing"]["1"]["anchor_sample_rate_hz"], 128
        )
        self.assertTrue(engine.snapshot()[1][0])
        self.assertTrue(all(abs(y - 300) < 1e-8 for y in engine.snapshot()[1][1]))

    def test_logical_lead_mapping_changes_drift_reference_not_raw_sum(self):
        engines = [Acquisition(lead_i_board=board) for board in (1, 2, None)]
        wire = config(associated=1, ratio=1)
        for first in range(0, 256, 8):
            for board, rate in ((1, 128.0128), (2, 128.0)):
                pulse = first + 1
                stamp = 1000000 + round(pulse * 1e6 / rate)
                wire += f"E2,A,123,1,{board},{pulse},{stamp}\n".encode()
                values = ";".join(f"{board * 100}:0" for _ in range(8))
                wire += f"E2,D,123,1,{board},{first},{values}\n".encode()
        for engine in engines:
            engine.feed(wire)
        self.assertAlmostEqual(engines[1].diagnostics()["relative_ppm"], 100, delta=0.2)
        self.assertLess(engines[0].diagnostics()["relative_ppm"], -99)
        self.assertNotIn("relative_ppm", engines[2].diagnostics())
        for engine in engines:
            self.assertLess(engine.diagnostics()["board2_vs_board1_ppm"], -99)
            self.assertTrue(all(abs(y - 300) < 1e-8 for y in engine.snapshot()[1][1]))
        self.assertEqual(engines[0].snapshot()[1], engines[1].snapshot()[1])
        self.assertNotIn("Lead II", engines[2].status())

    def test_timing_fault_preserves_measured_stream_and_mapping(self):
        for mode in (0, 1):
            engine = Acquisition(allow_unvalidated=True)
            engine.feed(config(mode=mode, associated=1, ratio=1))
            stream_samples(engine, count=256, ratio=1)
            mapping = (
                engine.channels[1].clock.time_us(0),
                engine.channels[1].clock.period_us,
            )
            engine.feed(b"E2,E,123,1,1,TIMING_GLITCH_D181_P7813_R7812,3000000\n")
            self.assertTrue(engine.raw_valid)
            self.assertFalse(engine.valid)
            self.assertEqual(engine.channels[1].last_mapping, mapping)
            for first in range(256, 320, 8):
                voltage_samples(engine, first=first)
            measured, derived, _ = engine.snapshot()
            self.assertEqual(engine.key, (123, 1))
            self.assertEqual(engine.epochs, 1)
            self.assertEqual(engine.sample_count, [320, 320])
            self.assertTrue(
                all(math.isfinite(y) for _, ys in measured.values() for y in ys)
            )
            self.assertEqual(derived, ([], []))
            self.assertIn("measured ECG continues", engine.status())
            self.assertFalse(engine.health()["healthy"])
            self.assertEqual(engine.health()["fault_count"], 1)

    def test_timing_context_is_bounded_and_not_an_anchor_or_recovery(self):
        engine = Acquisition()
        engine.feed(config(ratio=1))
        stream_samples(engine, count=256, ratio=1)
        stamp = engine.now_us
        fit = list(engine.channels[1].clock.anchors)
        for pulse in range(1, 41):
            engine.feed(f"E2,T,123,1,1,{pulse},9000000000,-181,256,0\n".encode())
        self.assertEqual(len(engine.timing_context), 18)
        self.assertEqual(engine.now_us, stamp)
        self.assertEqual(list(engine.channels[1].clock.anchors), fit)
        self.assertTrue(engine.health()["healthy"])
        engine.feed(b"E2,E,123,1,1,TIMING_GLITCH,3000000\n")
        engine.feed(b"E2,T,123,1,1,50361,3000181,181,256,0\n")
        self.assertFalse(engine.valid)
        self.assertTrue(engine.raw_valid)
        self.assertEqual(engine.fault_count, 1)

    def test_only_timing_faults_preserve_raw_validity(self):
        for code in (
            "TIMING_STARTUP",
            "TIMING_COUNT",
            "TIMING_MISSED",
            "TIMING_STALE",
            "IRQ_QUEUE_OVERFLOW",
        ):
            engine = Acquisition()
            engine.feed(config() + f"E2,E,123,1,1,{code},1100000\n".encode())
            engine.feed(b"E2,S,123,1,1,000000,20,1,0\n")
            self.assertTrue(engine.raw_valid)
            self.assertFalse(engine.valid)
            count = engine.fault_count
            engine.feed(b"E2,S,123,1,1,000000,20,1,0\n")
            self.assertEqual(engine.fault_count, count)
        for suffix in (
            b"E2,E,123,1,1,FIFO_OVERFLOW,1100000\n",
            b"E2,E,123,1,1,PLL_UNLOCK,1100000\n",
            b"E2,E,123,1,1,DATA_STALE,1100000\n",
            b"E2,S,123,1,1,000000,20,1,1\n",
            b"invalid\n",
        ):
            engine = Acquisition()
            engine.feed(config() + suffix)
            self.assertFalse(engine.raw_valid)

    def test_healthy_capture_requires_both_data_and_clock_fits(self):
        for mode in (0, 1):
            engine = Acquisition()
            self.assertFalse(engine.health()["healthy"])
            engine.feed(b"E2,E,123,0,0,MODE_CHANGE,200\n")
            engine.feed(config(mode=mode, ratio=1))
            self.assertFalse(engine.health()["healthy"])
            stream_samples(engine, ratio=1, boards=(1,))
            self.assertFalse(engine.health()["healthy"])
            self.assertEqual(engine.health()["fault_count"], 0)
            engine = Acquisition()
            engine.feed(config(mode=mode, ratio=1))
            stream_samples(engine, ratio=1)
            self.assertEqual(
                engine.health(),
                {
                    "healthy": True,
                    "issues": [],
                    "fault_count": 0,
                    "timing_validated": False,
                },
            )
            self.assertEqual(engine.diagnostics()["health"], engine.health())
            self.assertEqual(engine.snapshot()[1], ([], []))

    def test_ready_rejects_mismatched_board_settings(self):
        for launch, ratio, latency in (
            (1001000, 4, 4906),
            (1000000, 1, 4906),
            (1000000, 4, 0),
        ):
            engine = Acquisition()
            engine.feed(
                b"E2,C,123,1,1,0,513742,1000000,4,4906,0\n"
                + f"E2,C,123,1,2,0,513742,{launch},{ratio},{latency},0\n".encode()
                + b"E2,R,123,1,0\n"
            )
            self.assertFalse(engine.ready)
            self.assertFalse(engine.health()["healthy"])
            self.assertGreater(engine.health()["fault_count"], 0)

    def test_settling_only_is_not_valid_capture_data(self):
        engine = Acquisition()
        engine.feed(config())
        anchors(engine, count=12)
        for board in (1, 2):
            engine.feed(f"E2,D,123,1,{board},0,10:1;20:3\n".encode())
        health = engine.health()
        self.assertFalse(health["healthy"])
        self.assertEqual(health["fault_count"], 0)
        self.assertTrue(all("no valid-voltage" in issue for issue in health["issues"]))

    def test_capture_fault_history_survives_recovery_and_recent_event_eviction(self):
        engine = Acquisition()
        engine.feed(config())
        anchors(engine, count=12)
        voltage_samples(engine)
        self.assertTrue(engine.health()["healthy"])
        for n in range(40):
            engine.feed(f"E2,E,123,1,1,FAULT_{n},2300000\n".encode())
        self.assertEqual(len(engine.events), 12)
        self.assertEqual(engine.health()["fault_count"], 40)
        self.assertLessEqual(len(engine.fault_reasons), 16)
        self.assertEqual(sum(engine.fault_reasons.values()), 40)
        self.assertTrue(any("FAULT_0" in issue for issue in engine.health()["issues"]))
        engine.feed(config(epoch=2))
        anchors(engine, epoch=2, count=12)
        voltage_samples(engine, epoch=2)
        self.assertTrue(engine.ready and engine.valid)
        self.assertFalse(engine.health()["healthy"])
        self.assertGreater(engine.health()["fault_count"], 40)

    def test_restarts_and_parser_or_data_faults_cannot_be_hidden_by_recovery(self):
        for fault in (
            b"broken\n",
            b"E2,D,123,1,1,8,10:2\n",
            b"E2,S,123,1,1,000000,32,1,0\n",
            b"E2,S,123,1,1,000100,32,0,0\n",
            b"E2,E,123,1,0,MODE_CHANGE,2300000\n",
            b"",  # An unannounced new epoch is itself a discontinuity.
        ):
            engine = Acquisition()
            engine.feed(config())
            anchors(engine, count=12)
            voltage_samples(engine)
            engine.feed(fault + config(epoch=2))
            anchors(engine, epoch=2, count=12)
            voltage_samples(engine, epoch=2)
            self.assertFalse(engine.health()["healthy"])
            self.assertGreater(engine.health()["fault_count"], 0)

    def test_old_records_and_invalid_mode_do_not_add_capture_faults(self):
        engine = Acquisition()
        engine.feed(config())
        engine.feed(config(session=456))
        prior = engine.health()["fault_count"]
        engine.feed(config() + b"E2,E,123,1,1,FIFO_OVERFLOW,2300000\n")
        engine.feed(b"E2,E,456,1,0,INVALID_MODE,2000000\n")
        self.assertEqual(engine.health()["fault_count"], prior)
        self.assertEqual(engine.key, (456, 1))

    def test_fifo_progress_detects_both_stale_anchor_streams(self):
        engine = Acquisition(True)
        engine.feed(config())
        anchors(engine, count=12)
        voltage_samples(engine)
        self.assertTrue(engine.health()["healthy"])
        for first in range(8, 400, 8):
            voltage_samples(engine, first=first)
        self.assertFalse(engine.health()["healthy"])
        self.assertTrue(any("stale" in issue for issue in engine.health()["issues"]))
        self.assertEqual(engine.snapshot()[1], ([], []))
        faults = engine.health()["fault_count"]
        self.assertEqual(faults, 2)
        anchors(engine, start=20, count=40)
        self.assertTrue(
            all(not c.clock.stale(engine.now_us) for c in engine.channels.values())
        )
        self.assertEqual(engine.health()["fault_count"], faults)
        self.assertFalse(engine.health()["healthy"])

    def test_validated_internal_and_external_status_labels(self):
        for mode, expected in ((0, "Internal-test sum"), (1, "Lead II estimate")):
            engine = Acquisition()
            engine.feed(config(mode=mode, associated=1))
            anchors(engine, count=12)
            voltage_samples(engine)
            self.assertTrue(engine.health()["timing_validated"])
            self.assertEqual(engine.status(), expected)

    def test_data_stall_remains_a_capture_fault_after_data_resume(self):
        engine = Acquisition(True)
        engine.feed(config())
        stream_samples(engine, ratio=4)
        self.assertTrue(engine.health()["healthy"])
        self.assertTrue(engine.snapshot()[1][0])
        anchors(engine, start=32, count=20)
        self.assertFalse(engine.health()["healthy"])
        self.assertIn("stale sample data", engine.status())
        self.assertEqual(engine.snapshot()[1], ([], []))
        faults = engine.health()["fault_count"]
        self.assertEqual(faults, 2)
        for first in range(256, 424, 8):
            voltage_samples(engine, first=first)
        self.assertNotIn("stale sample data", engine.status())
        self.assertEqual(engine.health()["fault_count"], faults)
        self.assertFalse(engine.health()["healthy"])

    def test_ratio_override_cannot_keep_validated_association(self):
        engine = Acquisition(samp_ratio=1)
        engine.feed(config(associated=1))
        stream_samples(engine, ratio=1)
        self.assertTrue(engine.health()["healthy"])
        self.assertFalse(engine.health()["timing_validated"])
        self.assertEqual(engine.snapshot()[1], ([], []))
        self.assertIn("unvalidated", engine.status())

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
        anchors(engine, count=12)
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
                values = ";".join(f"{i}:0" for _ in range(8))
                wire += f"E2,D,123,1,{board},{i*8},{values}\n".encode()
        expected = Acquisition(True)
        expected.feed(wire)
        self.assertTrue(expected.health()["healthy"])
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
