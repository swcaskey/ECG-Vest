from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class FirmwareDecoderTests(unittest.TestCase):
    def test_actual_cpp_decoder(self):
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest(
                "No native C++ compiler; Arduino Build still compiles the shared decoder"
            )
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "decoder.exe"
            subprocess.run(
                [
                    compiler,
                    "-std=c++11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(root),
                    str(root / "tests/firmware_decoder_test.cpp"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(binary)], check=True, capture_output=True, text=True)
