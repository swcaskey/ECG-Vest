"""Lossless raw serial chunks in CSV; replay never uses packet arrival times."""

import csv
import json
from pathlib import Path

FORMAT = "ecg-e2-csv-1"


class Recorder:
    def __init__(self, path, options=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("x", newline="", encoding="utf8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(("index", "wire_hex", "text"))
        self.writer.writerow(
            (
                0,
                "",
                json.dumps(
                    {
                        "format": FORMAT,
                        "protocol": "E2",
                        "units": "ADC counts; ESP monotonic microseconds",
                        "options": options or {},
                    }
                ),
            )
        )
        self.index = 0

    def write(self, chunk):
        self.index += 1
        self.writer.writerow(
            (self.index, chunk.hex(), chunk.decode("ascii", errors="replace"))
        )
        self.file.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def replay(path):
    with open(path, newline="", encoding="utf8") as stream:
        rows = csv.DictReader(stream)
        if rows.fieldnames != ["index", "wire_hex", "text"]:
            raise ValueError("Not an ECG CSV recording")
        metadata = next(rows, None)
        if metadata is None or json.loads(metadata["text"]).get("format") != FORMAT:
            raise ValueError("Unsupported ECG recording format")
        for index, row in enumerate(rows, 1):
            if int(row["index"]) != index:
                raise ValueError("Missing/reordered CSV chunk")
            yield bytes.fromhex(row["wire_hex"])
