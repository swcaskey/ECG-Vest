"""Decode the PCB E2 stream shared by USB and the BLE receiver."""

from dataclasses import dataclass

from .timing import SPI_TIMING_WINDOW_US

LEADS = ("I", "II", "III")
# AFE order and signs assume the documented PCB wiring. Verify on the bench.
POLARITY = (1, 1, 1)
MAX_LINE = 1024


@dataclass(frozen=True)
class Configuration:
    chip_id: int
    launch_us: int
    latency_ticks: int
    timing_window_us: int


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
        chip_id = integer(chip, high=0xFFFFFF, base=16)
        part = chip_id & 0xF03000
        if mode != "1" or part not in (0x501000, 0x503000):
            raise ValueError("unexpected ECG mode or chip ID")
        timing_window = SPI_TIMING_WINDOW_US if integer(flag, 0, 1) else 0
        if ratio != "1":
            raise ValueError("expected FIFO-position timestamps")
        return (
            kind,
            key,
            afe,
            Configuration(
                chip_id, integer(launch), integer(delay, 0, 32768), timing_window
            ),
        )
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
    if kind == "E" and len(fields) == 2:
        integer(fields[1])
        return kind, key, afe, fields[0]
    raise ValueError("unknown ECG record")
