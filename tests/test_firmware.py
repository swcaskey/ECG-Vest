import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class FirmwareTests(unittest.TestCase):
    def test_shared_firmware_with_simulated_spi(self):
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest("Native C++ compiler unavailable")
        root = Path(__file__).resolve().parents[1]
        tests = root / "tests"
        library = root / "firmware" / "libraries" / "EcgPcb" / "src"
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "pcb_firmware_test.exe"
            warnings = ["-Wall", "-Wextra", "-Werror"]
            if os.name == "nt" and Path(compiler).stem.lower() == "g++":
                # Older MinGW checks printf against MSVCRT's length modifiers.
                warnings.append("-Wno-format")
            command = [
                compiler,
                "-std=c++11",
                *warnings,
                "-I",
                str(tests / "mock_arduino"),
                "-I",
                str(library),
                str(tests / "firmware_test.cpp"),
                str(library / "EcgPcb.cpp"),
                "-o",
                str(executable),
            ]
            for invocation in (command, [str(executable)]):
                result = subprocess.run(invocation, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
