from collections import deque
import serial
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import os

PORT = os.environ.get("ECG_PORT", "COM7")
SAMPLE_RATE = 128
WINDOW = SAMPLE_RATE * 5  # Five seconds

samples = deque(maxlen=WINDOW)
port = serial.Serial(PORT, 115200, timeout=0)
pending = bytearray()

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
    pending.extend(port.read(port.in_waiting))

    while b"\n" in pending:
        raw, _, remainder = pending.partition(b"\n")
        pending[:] = remainder
        text = raw.decode("ascii", errors="replace").strip()

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

try:
    plt.show()
finally:
    port.close()
