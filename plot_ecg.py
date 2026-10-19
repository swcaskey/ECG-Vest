from collections import deque
import serial
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import os
import time

PORT = os.environ.get("ECG_PORT", "COM7")
SAMPLE_RATE = 128
WINDOW = SAMPLE_RATE * 5  # Five seconds

MODE_CONFIRMATIONS = {
    "0": "INFO: input=internal calibration",
    "1": "INFO: input=electrodes; bias=external VCM",
}


def choose_input_mode():
    print("0 = Internal test signal")
    print("1 = Electrodes (external VCM body bias)")
    while True:
        choice = input("Input mode [0]: ").strip() or "0"
        if choice in MODE_CONFIRMATIONS:
            return choice
        print("Enter 0 or 1.")


def read_serial_lines(port, pending):
    pending.extend(port.read(port.in_waiting))
    while b"\n" in pending:
        raw, _, remainder = pending.partition(b"\n")
        pending[:] = remainder
        yield raw.decode("ascii", errors="replace").strip()


def select_input_mode(port, pending, choice):
    port.reset_input_buffer()
    port.write((choice + "\n").encode("ascii"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for text in read_serial_lines(port, pending):
            if text == MODE_CONFIRMATIONS[choice]:
                print(text)
                return
            if text.startswith("ERROR:"):
                raise RuntimeError(text)
            if text and not text.startswith("ECG:"):
                print(text)
        time.sleep(0.01)
    raise TimeoutError(
        "No input-mode confirmation. Check the port and upload the "
        "prompt-enabled firmware before using this plot."
    )


def main():
    choice = choose_input_mode()
    with serial.Serial(PORT, 115200, timeout=0) as port:
        pending = bytearray()
        select_input_mode(port, pending, choice)
        samples = deque(maxlen=WINDOW)

        fig, ax = plt.subplots()
        fig.canvas.manager.set_window_title("ECG Vest")
        (line,) = ax.plot([], [], linewidth=1)
        ax.set(
            title="ECG Vest — Live Signal",
            xlabel="Time relative to newest sample (seconds)",
            ylabel="ADC counts",
            xlim=(-5, 0),
            ylim=(-8000, 8000),
        )
        ax.grid(True)

        def update(_):
            for text in read_serial_lines(port, pending):
                if text.startswith("ECG:"):
                    try:
                        samples.append(int(text[4:]))
                    except ValueError:
                        print("Invalid sample:", text)
                elif text:
                    print(text)

            values = list(samples)
            times = [(i - len(values) + 1) / SAMPLE_RATE for i in range(len(values))]
            line.set_data(times, values)
            return (line,)

        animation = FuncAnimation(fig, update, interval=30, cache_frame_data=False)
        plt.show()


if __name__ == "__main__":
    main()
