"""USB acquisition shared by the command-line and desktop apps."""

from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time

from .acquisition import Capture
from .protocol import MAX_LINE, parse_record
from .recording import Recorder

RAW_LIMIT = 128 * 1024


class CaptureStart:
    """Find the start of a fresh firmware epoch without rewriting wire bytes."""

    def __init__(self, previous_key, skip_line):
        self.previous_key = previous_key
        self.skip_line = skip_line
        self.pending = b""

    def feed(self, chunk):
        data = self.pending + chunk
        offset = 0
        while (end := data.find(b"\n", offset)) >= 0:
            if not self.skip_line and end - offset <= MAX_LINE:
                try:
                    kind, key, afe, _ = parse_record(data[offset:end].decode("ascii"))
                except ValueError:
                    pass
                else:
                    old = self.previous_key
                    if (
                        kind == "C"
                        and afe == 1
                        and (old is None or key[0] != old[0] or key[1] > old[1])
                    ):
                        return data[offset:]
            self.skip_line = False
            offset = end + 1
        self.pending = data[offset:]
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.skip_line = True
        return b""


class LiveCapture:
    """Own the USB port and recording file on one worker thread."""

    def __init__(self, port_name, transport, command="go", recorder=None, echo=False):
        self.port_name = port_name
        self.capture = Capture(transport)
        self.command = command
        self.recorder = recorder
        self.echo = echo
        self.lock = Lock()
        self.opened = Event()
        self.stop = Event()
        self.error = None
        self.start_time = time.monotonic()
        self.thread = Thread(target=self._run, daemon=True, name="ECG serial")
        self._raw = bytearray()
        self._raw_end = 0
        self._record_state = "idle"
        self._record_path = None
        self._record_started = None
        self._record_error = None
        self._cutover = None
        self._record_deadline = None
        self._record_file = None
        self._record_line_open = False

    def _read(self, port, size):
        chunk = port.read(size)
        if not chunk:
            return
        if self.recorder:
            self.recorder.write(chunk)
        self._record_chunk(chunk)
        if self.echo:
            sys.stdout.write(chunk.decode("ascii", errors="replace"))
            sys.stdout.flush()
        with self.lock:
            self._raw.extend(chunk)
            self._raw_end += len(chunk)
            del self._raw[:-RAW_LIMIT]
            self.capture.feed(chunk, time.monotonic())

    def _send_start(self, port):
        command = (self.command + "\n").encode("ascii")
        if port.write(command) != len(command):
            raise OSError("Could not send the capture command")

    def _run(self):
        try:
            import serial

            with serial.Serial(
                self.port_name, 115200, timeout=0.1, write_timeout=1
            ) as port:
                try:
                    time.sleep(0.1)
                    port.reset_input_buffer()
                    self._send_start(port)
                    self.opened.set()
                    while not self.stop.is_set():
                        self._service_recording(port)
                        self._read(port, min(max(1, port.in_waiting), 16384))
                    if self._record_file:
                        self._stop_file(port)
                    if self.recorder:
                        deadline = time.monotonic() + 0.3
                        while (
                            self.capture.pending or self.capture.discarding_line
                        ) and time.monotonic() < deadline:
                            self._read(port, 1)
                finally:
                    try:
                        port.dtr = False
                        port.rts = False
                    except OSError:
                        pass  # A removed USB port cannot be reset.
        except (OSError, RuntimeError, ImportError) as error:
            self.error = error
            with self.lock:
                self.capture.invalidate(f"Serial session stopped: {error}")
        finally:
            self.stop.set()
            with self.lock:
                recording = self._record_state != "idle"
            if recording:
                self._end_recording(str(self.error) if self.error else None)
            self.opened.set()

    def _service_recording(self, port):
        with self.lock:
            state = self._record_state
            key = self.capture.key
            skip_line = bool(self.capture.pending or self.capture.discarding_line)
        if state == "starting":
            if self._cutover is None:
                self._cutover = CaptureStart(key, skip_line)
                self._record_deadline = time.monotonic() + 8
                self._send_start(port)
            elif time.monotonic() >= self._record_deadline:
                self._end_recording("No new capture arrived")
        elif state == "stopping":
            self._stop_file(port)

    def _record_chunk(self, chunk):
        try:
            if self._cutover:
                chunk = self._cutover.feed(chunk)
                if not chunk:
                    return
                self._cutover = None
                with self.lock:
                    if self._record_state != "starting":
                        return
                    path = self._record_path
                    self._record_state = "recording"
                    self._record_started = time.monotonic()
                self._record_file = Recorder(path, self.capture.transport)
            if self._record_file:
                self._record_file.write(chunk)
                self._record_line_open = not chunk.endswith(b"\n")
        except (OSError, ValueError) as error:
            self._end_recording(str(error))

    def _stop_file(self, port):
        # Include the rest of a wire record if Stop fell between USB chunks.
        deadline = time.monotonic() + 0.3
        while (
            self._record_file and self._record_line_open and time.monotonic() < deadline
        ):
            self._read(port, 1)
        if self._record_state != "idle":
            message = (
                "Capture ended inside a serial message"
                if self._record_line_open
                else None
            )
            self._end_recording(message)

    def _end_recording(self, error):
        if self._record_file:
            try:
                self._record_file.file.close()
            except OSError as close_error:
                error = error or str(close_error)
        self._record_file = self._cutover = None
        self._record_line_open = False
        with self.lock:
            self._record_state = "idle"
            self._record_error = error

    def start_recording(self, path):
        path = Path(path)
        if path.suffix.lower() != ".csv":
            raise ValueError("Choose a .csv recording file")
        if path.exists():
            raise FileExistsError(f"A file already exists at {path}")
        with self.lock:
            if self.stop.is_set() or not self.capture.ready or self.capture.problem:
                raise RuntimeError("Wait for a live capture before recording")
            if self.recorder or self._record_state != "idle":
                raise RuntimeError("Recording already active")
            self._record_path = path
            self._record_state = "starting"
            self._record_started = self._record_error = None

    def stop_recording(self):
        with self.lock:
            if self._record_state != "idle":
                self._record_state = "stopping"

    def recording_status(self):
        with self.lock:
            elapsed = (
                time.monotonic() - self._record_started
                if self._record_started is not None and self._record_state != "idle"
                else 0.0
            )
            return self._record_state, self._record_path, elapsed, self._record_error

    def raw_since(self, cursor=0):
        """Return newly received raw text within the retained history."""
        with self.lock:
            end = self._raw_end
            offset = max(0, cursor - (end - len(self._raw)))
            return end, self._raw[offset:].decode("ascii", errors="replace")

    @property
    def ready(self):
        with self.lock:
            return (
                self.capture.ready
                and not self.capture.problem
                and not self.stop.is_set()
            )

    def health(self):
        with self.lock:
            return self.capture.health(time.monotonic())

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            x, y, status = self.capture.snapshot(now)
            if self.capture.key is None and not self.capture.problem:
                if self.capture.transport == "usb" and now - self.start_time > 8:
                    status = (
                        "No capture reply. Check the PCB Nano USB port and firmware."
                    )
                elif status.startswith("Looking for") and now - self.start_time > 15:
                    status = (
                        "Looking for the BLE transmitter. Check the PCB Nano power."
                    )
            return x, y, status

    def start_async(self):
        self.start_time = time.monotonic()
        self.thread.start()

    def close(self, timeout=2):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            raise TimeoutError("ECG serial worker did not close")

    def __enter__(self):
        self.start_async()
        try:
            if not self.opened.wait(4):
                raise TimeoutError("Timed out opening the ECG USB port")
            if self.error:
                raise self.error
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, error_type, *_):
        self.close()
        if error_type is None and self.error:
            raise self.error
