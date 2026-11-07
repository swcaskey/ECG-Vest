"""ASCII lines: E2,type,session,epoch,board,payload (R omits board).

C: mode,chip_hex,launch_us,pulse_ratio,latency_ticks,association_valid
R: mode; D: first_sequence,value:tag;value:tag;...
A: pulse_count,time_us; S: status_hex,pulse_count,irq_lost,tx_lost
E: event_code,time_us. Times use the MCU clock; values are raw ADC counts.
"""

from dataclasses import dataclass

MAX_LINE = 1024
MAX_BATCH = 16


class ProtocolError(ValueError):
    pass


class ProtocolMismatch(ProtocolError):
    pass


@dataclass(frozen=True)
class Record:
    kind: str
    session: int
    epoch: int
    board: int
    fields: tuple
    raw: str


def integer(text, low=0, high=(1 << 63) - 1, base=10):
    value = int(text, base)
    if not low <= value <= high:
        raise ProtocolError("Field outside protocol limits")
    return value


def decode_fifo(word):
    """Host reference for the hardware decoder; settling positions still count."""
    word = integer(str(word), 0, 0xFFFFFF)
    tag = (word >> 3) & 7
    sample = word >> 6
    if sample & 0x20000:
        sample -= 0x40000
    return sample if tag in (0, 2) else None, tag


def parse_line(line):
    if line.startswith("ECG:") or (line.startswith("E") and not line.startswith("E2,")):
        raise ProtocolMismatch("Expected E2 firmware; upload the two-board firmware.")
    if not line.startswith("E2,"):
        if line.startswith(("INFO:", "Select ", "0 =", "1 =")) or not line:
            return None
        raise ProtocolError("Unrecognized serial line: " + line[:80])
    parts = line.split(",")
    try:
        kind = parts[1]
        session, epoch = integer(parts[2], high=0xFFFFFFFF), integer(
            parts[3], 0 if kind == "E" else 1, 0xFFFFFFFF
        )
        if kind == "R":
            if len(parts) != 5:
                raise ProtocolError("Invalid ready record")
            return Record(kind, session, epoch, 0, (integer(parts[4], 0, 1),), line)
        board = integer(parts[4], 0 if kind == "E" else 1, 2)
        rest = parts[5:]
        if kind == "C" and len(rest) == 6:
            fields = (
                integer(rest[0], 0, 1),
                integer(rest[1], 0, 0xFFFFFF, 16),
                integer(rest[2]),
                integer(rest[3], 1, 16),
                integer(rest[4], 0, 32768),
                integer(rest[5], 0, 1),
            )
        elif kind == "D" and len(rest) == 2:
            first = integer(rest[0])
            items = rest[1].split(";")
            if not 1 <= len(items) <= MAX_BATCH:
                raise ProtocolError("Invalid sample batch length")
            samples = []
            for item in items:
                value, tag = item.split(":")
                samples.append((integer(value, -131072, 131071), integer(tag, 0, 7)))
            fields = (first, tuple(samples))
        elif kind == "A" and len(rest) == 2:
            fields = (integer(rest[0], 1), integer(rest[1]))
        elif kind == "S" and len(rest) == 4:
            fields = (
                integer(rest[0], 0, 0xFFFFFF, 16),
                integer(rest[1]),
                integer(rest[2], high=0xFFFFFFFF),
                integer(rest[3], high=0xFFFFFFFF),
            )
        elif kind == "E" and len(rest) == 2:
            if (
                not rest[0]
                or len(rest[0]) > 48
                or not rest[0].replace("_", "").isalnum()
            ):
                raise ProtocolError("Invalid event code")
            fields = (rest[0], integer(rest[1]))
        else:
            raise ProtocolError("Unknown E2 record type or field count")
        return Record(kind, session, epoch, board, fields, line)
    except (IndexError, ValueError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("Malformed E2 record") from exc


class Parser:
    def __init__(self):
        self.pending = bytearray()
        self.discarding = False
        self.errors = []

    def feed(self, chunk):
        self.errors = []
        records = []
        for byte in chunk:
            if byte == 10:
                if not self.discarding:
                    try:
                        line = self.pending.decode("ascii").strip()
                        record = parse_line(line)
                        if record is not None:
                            records.append(record)
                    except ProtocolMismatch:
                        self.pending.clear()
                        raise
                    except (ProtocolError, UnicodeDecodeError) as exc:
                        self.errors.append(str(exc))
                self.pending.clear()
                self.discarding = False
            elif not self.discarding:
                self.pending.append(byte)
                if len(self.pending) > MAX_LINE:
                    self.pending.clear()
                    self.discarding = True
                    self.errors.append("Oversized serial line discarded")
        return records
