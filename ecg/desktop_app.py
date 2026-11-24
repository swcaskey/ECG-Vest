"""Windows desktop viewer for the ECG Vest."""

from datetime import datetime
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image, ImageTk
from serial.tools import list_ports

from ecg.desktop_recording import DesktopPlayback, export_samples, list_recordings
from ecg.desktop_theme import (
    DARK,
    DARK_TTK_THEME,
    LIGHT,
    SELECTION,
    create_dark_theme,
    style_axis,
    style_title_bar,
)
from ecg.live import LiveCapture
from ecg.protocol import LEADS
from ecg.timing import WINDOW
from ecg.vector_view import VectorView

TRANSPORTS = {"usb": "USB cable", "ble": "Wireless"}
NANO_USB_ID = (0x2341, 0x0070)  # Nano ESP32 in the pinned Arduino core.
CSV_FILES = [("ECG recordings", "*.csv")]
RAW_LIMIT = 100_000


def device_id(port):
    if port.serial_number:
        return f"{port.vid}:{port.pid}:{port.serial_number}"
    return None


def available_ports(ports):
    return [
        port for port in ports if not port.hwid.upper().startswith(("BTHENUM", "BTHLE"))
    ]


def connection_ports(ports, remembered, other_device):
    """Prefer a remembered device or offer eligible Nano ESP32 ports."""
    if remembered:
        matches = [port for port in ports if device_id(port) == remembered]
        if len(matches) == 1:
            return matches
    return [
        port
        for port in ports
        if (port.vid, port.pid) == NANO_USB_ID
        and (not other_device or device_id(port) != other_device)
    ]


def elapsed(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


class VestApp:
    def __init__(self, root, state_dir=None, recordings=None):
        self.root = root
        self.state_dir = (
            state_dir or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ECG Vest"
        )
        self.settings_path = self.state_dir / "settings.json"
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.settings = data if isinstance(data, dict) else {}
        self.theme = tk.StringVar(
            value="Dark" if self.settings.get("theme") == "Dark" else "Light"
        )
        self.style = ttk.Style(root)
        self.light_theme = self.style.theme_use()
        create_dark_theme(self.style)
        devices = self.settings.get("devices")
        self.devices = devices if isinstance(devices, dict) else {}
        saved_folder = self.settings.get("recordings")
        self.folder = recordings or (
            Path(saved_folder)
            if isinstance(saved_folder, str) and saved_folder
            else Path.home() / "Documents" / "ECG Vest" / "Recordings"
        )
        saved_transport = self.settings.get("transport")
        self.transport = tk.StringVar(
            value=(
                "ble"
                if saved_transport in ("ble", "Wireless via BLE receiver")
                else "usb"
            )
        )
        self.connection = tk.StringVar(value="Disconnected")
        self.notice = tk.StringVar()
        self.record_label = tk.StringVar()
        self.folder_label = tk.StringVar(value=str(self.folder))
        self.position = tk.DoubleVar()
        self.position_label = tk.StringVar()
        self.speed = tk.StringVar(value="1x")
        self.pause_plot = tk.BooleanVar()
        self.pause_serial = tk.BooleanVar()
        self.saved = {}
        self.live = None
        self.replay = None
        self.replay_controls = []
        self.live_port_id = None
        self.replay_name = ""
        self.last_record_state = "idle"
        self.last_raw = ""
        self.raw_cursor = 0
        self.last_status = None
        self.has_trace = False
        self.seeking = False
        self.busy = False
        self.library_loaded = False
        self.library_dirty = False
        self.jobs = SimpleQueue()

        root.title("ECG Vest")
        root.geometry("1080x820")
        root.minsize(820, 610)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_layout()
        self.apply_theme()
        self.update_controls()
        self.timer = root.after(100, self.tick)

    def build_layout(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        title = ttk.Frame(outer)
        title.pack(fill="x", pady=(0, 12))
        ttk.Label(title, text="ECG Vest", font=("Segoe UI", 19, "bold")).pack(
            side="left"
        )
        self.theme_box = ttk.Combobox(
            title,
            textvariable=self.theme,
            values=("Light", "Dark"),
            width=7,
            state="readonly",
        )
        self.theme_box.configure(postcommand=lambda: self.style_popup(self.theme_box))
        self.theme_box.bind("<<ComboboxSelected>>", self.change_theme)
        self.theme_box.pack(side="right")
        ttk.Label(title, text="Theme").pack(side="right", padx=(0, 6))
        ttk.Label(title, textvariable=self.connection).pack(side="right", padx=(0, 16))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 12))
        self.transport_buttons = []
        for key, label in TRANSPORTS.items():
            button = ttk.Radiobutton(
                toolbar,
                text=label,
                variable=self.transport,
                value=key,
                command=lambda: self.notice.set(""),
            )
            button.pack(side="left", padx=(0, 14))
            self.transport_buttons.append(button)
        self.connect_button = ttk.Button(toolbar, text="Connect", command=self.connect)
        self.connect_button.pack(side="left", padx=(0, 8))
        self.choose_port_button = ttk.Button(
            toolbar, text="Choose Port…", command=lambda: self.connect(manual=True)
        )
        self.choose_port_button.pack(side="left")

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.live_tab = ttk.Frame(self.tabs, padding=10)
        self.vector_tab = ttk.Frame(self.tabs, padding=10)
        self.serial_tab = ttk.Frame(self.tabs, padding=10)
        self.library_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.live_tab, text="Live")
        self.tabs.add(self.vector_tab, text="Vector View")
        self.tabs.add(self.serial_tab, text="Serial")
        self.tabs.add(self.library_tab, text="Recordings")
        self.tabs.bind("<<NotebookTabChanged>>", self.on_tab)

        self.make_plot()
        self.vector = VectorView(self.vector_tab)
        self.make_replay_controls(self.vector.frontal_tab)
        self.make_replay_controls(self.vector.heart.replay_controls)
        self.make_serial_view()
        self.make_library()
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.notice).pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(footer, textvariable=self.record_label).pack(side="right")

    def change_theme(self, _event):
        self.apply_theme()
        self.save_preferences()

    def apply_theme(self):
        dark = self.theme.get() == "Dark"
        colors = DARK if dark else LIGHT
        self.style.theme_use(DARK_TTK_THEME if dark else self.light_theme)
        self.root.configure(background=colors.background)
        self.raw.configure(
            background=colors.surface,
            foreground=colors.text,
            selectbackground=SELECTION,
            selectforeground="#ffffff",
            highlightbackground=colors.border,
            highlightcolor=colors.trace,
        )
        self.figure.set_facecolor(colors.background)
        for axis, line in zip(self.axes, self.lines):
            style_axis(axis, colors)
            line.set_color(colors.trace)
        self.canvas.get_tk_widget().configure(background=colors.background)
        self.canvas.draw_idle()
        self.vector.apply_theme(colors)
        style_title_bar(self.root, dark)

    def style_popup(self, box):
        colors = DARK if self.theme.get() == "Dark" else LIGHT
        popup = box.tk.call("ttk::combobox::PopdownWindow", box)
        box.tk.call(
            f"{popup}.f.l",
            "configure",
            "-background",
            colors.surface,
            "-foreground",
            colors.text,
            "-selectbackground",
            SELECTION,
            "-selectforeground",
            "#ffffff",
        )

    def make_plot(self):
        actions = ttk.Frame(self.live_tab)
        actions.pack(fill="x", pady=(0, 6))
        self.record_button = ttk.Button(
            actions, text="Start Recording", command=self.record
        )
        self.record_button.pack(side="left", padx=(0, 8))
        ttk.Checkbutton(actions, text="Pause display", variable=self.pause_plot).pack(
            side="left"
        )
        self.image_button = ttk.Button(
            actions, text="Save Plot Image", command=self.save_plot
        )
        self.image_button.pack(side="right")

        self.figure = Figure(figsize=(8, 5), layout="constrained")
        self.axes = self.figure.subplots(3, 1, sharex=True)
        self.lines = []
        for axis, lead in zip(self.axes, LEADS):
            (line,) = axis.plot([], [], linewidth=1.1, color="#156b83")
            self.lines.append(line)
            axis.set(title=f"Lead {lead}", ylabel="ADC counts", xlim=(-WINDOW, 0))
            axis.grid(alpha=0.25)
        self.axes[-1].set_xlabel("Estimated time (s)")
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.live_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()
        self.make_replay_controls(self.live_tab)

    def make_replay_controls(self, parent):
        controls = ttk.Frame(parent)
        play = ttk.Button(controls, text="Pause", command=self.play_pause)
        play.pack(side="left", padx=(0, 8))
        speed_box = ttk.Combobox(
            controls,
            textvariable=self.speed,
            values=("0.5x", "1x", "2x", "4x"),
            width=5,
            state="readonly",
        )
        speed_box.configure(postcommand=lambda: self.style_popup(speed_box))
        speed_box.pack(side="left", padx=(0, 8))
        speed_box.bind("<<ComboboxSelected>>", self.change_speed)
        ttk.Button(controls, text="Close Replay", command=self.close_replay).pack(
            side="right", padx=(8, 0)
        )
        ttk.Label(controls, textvariable=self.position_label, width=15).pack(
            side="right"
        )
        slider = ttk.Scale(controls, variable=self.position, from_=0, to=1)
        slider.pack(side="left", fill="x", expand=True, padx=8)
        slider.bind("<ButtonPress-1>", self.begin_seek)
        slider.bind("<ButtonRelease-1>", self.seek)
        slider.bind("<KeyRelease>", self.seek)
        self.replay_controls.append((controls, play, slider))

    def make_serial_view(self):
        actions = ttk.Frame(self.serial_tab)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(actions, text="Pause display", variable=self.pause_serial).pack(
            side="left"
        )
        ttk.Button(actions, text="Copy", command=self.copy_serial).pack(side="right")
        scroll = ttk.Scrollbar(self.serial_tab)
        scroll.pack(side="right", fill="y")
        self.raw = tk.Text(
            self.serial_tab,
            wrap="none",
            font=("Consolas", 10),
            yscrollcommand=scroll.set,
            state="disabled",
            padx=8,
            pady=6,
        )
        self.raw.pack(fill="both", expand=True)
        scroll.configure(command=self.raw.yview)

    def make_library(self):
        actions = ttk.Frame(self.library_tab)
        actions.pack(fill="x", pady=(0, 8))
        for label, action in (
            ("Open File…", self.open_file),
            ("Choose Folder…", self.choose_folder),
            ("Refresh", self.refresh_library),
        ):
            ttk.Button(actions, text=label, command=action).pack(
                side="left", padx=(0, 8)
            )
        ttk.Label(self.library_tab, textvariable=self.folder_label).pack(
            anchor="w", pady=(0, 8)
        )
        columns = ("name", "date", "duration", "source")
        tree_frame = ttk.Frame(self.library_tab)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse"
        )
        for key, title, width in zip(
            columns, ("Name", "Date", "Duration", "Source"), (325, 140, 90, 85)
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key == "name")
        scroll = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _: self.play_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.update_controls())
        actions = ttk.Frame(self.library_tab)
        actions.pack(fill="x", pady=(8, 0))
        self.open_button = ttk.Button(
            actions, text="Replay", command=self.play_selected
        )
        self.open_button.pack(side="left", padx=(0, 8))
        self.export_button = ttk.Button(
            actions, text="Export Samples…", command=self.export_selected
        )
        self.export_button.pack(side="left")

    def save_preferences(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(
                    {
                        "transport": self.transport.get(),
                        "theme": self.theme.get(),
                        "recordings": str(self.folder),
                        "devices": self.devices,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def choose_port(self, ports, transport):
        dialog = tk.Toplevel(self.root)
        wireless = transport == "ble"
        dialog.title("Choose Wireless Receiver" if wireless else "Choose USB Port")
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=(
                "Select the receiver Nano plugged into this computer."
                if wireless
                else "Select the vest’s Nano plugged into this computer."
            ),
        ).pack(anchor="w", pady=(0, 8))
        table = ttk.Treeview(
            body,
            columns=("port", "device"),
            show="headings",
            selectmode="browse",
            height=min(len(ports), 6),
        )
        table.heading("port", text="Port")
        table.heading("device", text="Device")
        table.column("port", width=70, stretch=False)
        table.column("device", width=280)
        by_name = {port.device: port for port in ports}
        for name in sorted(by_name, key=lambda name: (len(name), name)):
            item = by_name[name]
            description = (
                "Arduino Nano ESP32"
                if (item.vid, item.pid) == NANO_USB_ID
                else (item.description or "Serial device").removesuffix(f" ({name})")
            )
            table.insert("", "end", iid=name, values=(name, description))
        table.pack(fill="both", expand=True)
        selected = None

        def accept(_event=None):
            nonlocal selected
            selection = table.selection()
            if selection:
                selected = by_name[selection[0]]
                dialog.destroy()

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        connect_button = ttk.Button(
            actions, text="Connect", command=accept, state="disabled"
        )
        connect_button.pack(side="right")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(
            side="right", padx=(0, 8)
        )
        table.bind(
            "<<TreeviewSelect>>",
            lambda _: connect_button.configure(
                state="normal" if table.selection() else "disabled"
            ),
        )
        table.bind("<Double-1>", accept)
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        table.focus_set()
        style_title_bar(dialog, self.theme.get() == "Dark")
        dialog.grab_set()
        self.root.wait_window(dialog)
        return selected

    def connect(self, manual=False):
        if self.live:
            self.disconnect()
            return
        if self.busy:
            return
        transport = self.transport.get()
        wireless = transport == "ble"
        ports = available_ports(list_ports.comports())
        candidates = (
            ports
            if manual
            else connection_ports(
                ports,
                self.devices.get(transport),
                self.devices.get("usb" if wireless else "ble"),
            )
        )
        if not candidates:
            device = "wireless receiver Nano" if wireless else "vest’s Nano"
            self.notice.set(f"Plug the {device} into this computer by USB.")
            return
        port = (
            self.choose_port(candidates, transport)
            if manual or len(candidates) > 1
            else candidates[0]
        )
        if port is None:
            return
        self.close_replay()
        self.clear_display()
        self.live_port_id = device_id(port)
        self.live = LiveCapture(port.device, transport)
        self.live.start_async()
        self.tabs.tab(self.live_tab, text="Live")
        self.tabs.select(self.live_tab)
        self.connection.set(f"Connecting ({TRANSPORTS[transport]})…")
        self.notice.set("")
        self.update_controls()

    def disconnect(self):
        live = self.live
        if not live:
            return
        try:
            live.close()
        except (OSError, RuntimeError) as error:
            messagebox.showerror("Disconnect", str(error), parent=self.root)
            if live.thread.is_alive():
                return
        self.finish_live()

    def finish_live(self):
        _, path, _, error = self.live.recording_status()
        if path and path.is_file():
            self.library_dirty = True
        problem = self.live.error or error
        self.live = None
        self.vector.frontal.clear()
        self.vector.heart.clear_vest()
        self.connection.set("Disconnected")
        if problem:
            self.notice.set(str(problem))
        self.record_label.set("")
        self.last_record_state = "idle"
        self.update_controls()

    def record(self):
        if not self.live:
            return
        state, _, _, _ = self.live.recording_status()
        try:
            if state in ("recording", "starting"):
                self.live.stop_recording()
                return
            self.folder.mkdir(parents=True, exist_ok=True)
            name = filedialog.asksaveasfilename(
                parent=self.root,
                title="Start a new recorded capture",
                initialdir=self.folder,
                initialfile=datetime.now().strftime("ECG-%Y%m%d-%H%M%S.csv"),
                defaultextension=".csv",
                filetypes=CSV_FILES,
                confirmoverwrite=False,
            )
            if not name:
                return
            path = Path(name)
            if path.suffix.lower() != ".csv" or path.exists():
                raise ValueError("Choose a new .csv filename.")
            self.live.start_recording(path)
            if self.folder != path.parent:
                self.saved.clear()
                self.tree.delete(*self.tree.get_children())
                self.folder = path.parent
                self.folder_label.set(str(self.folder))
                self.library_dirty = True
                self.save_preferences()
            self.notice.set("Starting a new capture for recording…")
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Recording", str(error), parent=self.root)

    def save_plot(self):
        name = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Plot Image",
            initialfile="ECG.png",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
        )
        if name:
            try:
                self.figure.savefig(name, dpi=180, format="png")
                self.notice.set(f"Saved {Path(name).name}")
            except (OSError, ValueError) as error:
                messagebox.showerror("Save Plot Image", str(error), parent=self.root)

    def copy_serial(self):
        text = self.raw.get("1.0", "end-1c")
        if self.raw.tag_ranges("sel"):
            text = self.raw.get("sel.first", "sel.last")
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.notice.set("Serial text copied")

    def show_serial(self, text, replace=False):
        if not text and not replace:
            return
        at_bottom = self.raw.yview()[1] >= 0.999
        self.raw.configure(state="normal")
        if replace:
            self.raw.delete("1.0", "end")
        self.raw.insert("end", text)
        count = self.raw.count("1.0", "end-1c", "chars")
        if count and count[0] > RAW_LIMIT:
            self.raw.delete("1.0", f"1.0+{count[0] - RAW_LIMIT}c")
        self.raw.configure(state="disabled")
        if at_bottom or replace:
            self.raw.see("end")

    def run_job(self, action, on_success):
        if self.busy:
            return
        self.busy = True
        self.update_controls()

        def worker():
            try:
                self.jobs.put((on_success, action(), None))
            except Exception as error:
                self.jobs.put((on_success, None, error))

        Thread(target=worker, daemon=True).start()

    def on_tab(self, _event=None):
        if self.tabs.select() == str(self.library_tab) and (
            not self.library_loaded or self.library_dirty
        ):
            self.refresh_library()

    def refresh_library(self):
        if self.busy:
            return
        folder = self.folder
        self.library_dirty = False
        self.notice.set("Reading recordings…")
        self.run_job(lambda: list_recordings(folder), self.populate_library)

    def populate_library(self, records):
        self.tree.delete(*self.tree.get_children())
        self.saved.clear()
        for i, item in enumerate(records):
            key = str(i)
            self.saved[key] = item.path
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    item.name,
                    item.modified.strftime("%Y-%m-%d %H:%M"),
                    elapsed(item.duration),
                    item.transport.upper(),
                ),
            )
        self.library_loaded = True
        self.notice.set("" if records else "No recordings in this folder")
        self.update_controls()

    def choose_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title="Recordings Folder")
        if folder:
            self.folder = Path(folder)
            self.folder_label.set(str(self.folder))
            self.save_preferences()
            self.refresh_library()

    def selected_path(self):
        selection = self.tree.selection()
        return self.saved.get(selection[0]) if selection else None

    def play_selected(self):
        path = self.selected_path()
        if path:
            self.open_replay(path)

    def open_file(self):
        if self.busy:
            return
        name = filedialog.askopenfilename(
            parent=self.root,
            title="Open Recording",
            initialdir=self.folder,
            filetypes=CSV_FILES,
        )
        if name:
            self.open_replay(Path(name))

    def open_replay(self, path):
        if self.busy:
            return
        if self.live:
            self.notice.set("Disconnect live capture before opening a replay")
            return
        self.close_replay()
        self.notice.set(f"Opening {path.name}…")
        self.run_job(
            lambda: DesktopPlayback(path),
            lambda player: self.start_replay(path, player),
        )

    def start_replay(self, path, player):
        self.clear_display()
        self.replay = player
        self.replay_name = path.name
        self.replay.speed = float(self.speed.get().rstrip("x"))
        self.replay.play()
        self.pause_plot.set(False)
        for controls, _, slider in self.replay_controls:
            slider.configure(to=max(0.01, player.duration))
            controls.pack(fill="x", pady=(8, 0))
        self.tabs.tab(self.live_tab, text="Replay")
        self.tabs.select(self.live_tab)
        self.connection.set(f"Replay | {path.name}")
        self.update_controls()

    def close_replay(self):
        if self.replay:
            self.replay.close()
            self.replay = None
            for controls, _, _ in self.replay_controls:
                controls.pack_forget()
            self.tabs.tab(self.live_tab, text="Live")
            self.connection.set("Disconnected")
            self.notice.set("")
            self.clear_display()
            self.update_controls()

    def play_pause(self):
        if not self.replay:
            return
        if self.replay.done:
            self.replay.seek(0)
            self.replay.play()
            self.vector.frontal.resume()
            self.vector.heart.follow_latest()
        elif self.replay.paused:
            self.replay.play()
            self.vector.frontal.resume()
            self.vector.heart.follow_latest()
        else:
            self.replay.pause()

    def change_speed(self, _event):
        if self.replay:
            self.replay.speed = float(self.speed.get().rstrip("x"))

    def begin_seek(self, _event):
        self.seeking = True

    def seek(self, _event):
        self.seeking = False
        if self.replay:
            self.replay.seek(self.position.get())
            self.vector.frontal.resume()
            self.vector.heart.follow_latest()

    def export_selected(self):
        path = self.selected_path()
        if not path or self.busy:
            return
        target = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Samples",
            initialfile=f"{path.stem}-samples.csv",
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
        )
        if target:
            self.notice.set("Exporting samples…")
            self.run_job(
                lambda: export_samples(path, Path(target)),
                lambda count: self.notice.set(
                    f"Exported {count:,} sample rows to {Path(target).name}"
                ),
            )

    def clear_display(self):
        self.has_trace = False
        self.last_status = None
        self.last_raw = ""
        self.raw_cursor = 0
        self.show_serial("", replace=True)
        self.vector.frontal.clear()
        self.vector.heart.clear_vest()
        for line in self.lines:
            line.set_data([], [])
        self.canvas.draw_idle()

    def update_controls(self):
        connected = self.live is not None
        state = "disabled" if connected or self.busy else "normal"
        for button in self.transport_buttons:
            button.configure(state=state)
        self.choose_port_button.configure(state=state)
        self.connect_button.configure(
            text="Disconnect" if connected else "Connect",
            state="disabled" if self.busy and not connected else "normal",
        )
        record_state = self.live.recording_status()[0] if connected else "idle"
        record_enabled = (
            connected
            and record_state != "stopping"
            and (self.live.ready or record_state in ("recording", "starting"))
        )
        self.record_button.configure(
            text="Stop Recording" if record_state != "idle" else "Start Recording",
            state="normal" if record_enabled else "disabled",
        )
        selection = self.selected_path()
        self.open_button.configure(
            state="normal" if selection and not self.busy else "disabled"
        )
        self.export_button.configure(
            state="normal" if selection and not self.busy else "disabled"
        )
        self.image_button.configure(state="normal" if self.has_trace else "disabled")

    def draw(self, source):
        x, traces, status = source.snapshot()
        if status != self.last_status:
            self.notice.set(status)
            self.last_status = status
        tab = self.tabs.select()
        if tab == str(self.vector_tab):
            name = "ECG recording" if self.replay else "Live ECG"
            if self.vector.showing_frontal:
                self.vector.frontal.update(
                    x, traces, status or f"{name} · frontal plane"
                )
            else:
                label = f"{name} · ADC counts · frontal vector · drag heart to rotate"
                self.vector.heart.update_vest(x, traces, status or label)
        if tab != str(self.live_tab) or self.pause_plot.get():
            return
        self.has_trace = bool(traces)
        for i, (axis, line) in enumerate(zip(self.axes, self.lines)):
            line.set_data(x, traces[i] if traces else [])
            if traces:
                axis.relim()
                axis.autoscale_view(scalex=False)
        self.canvas.draw_idle()

    def tick_live(self):
        if self.live.stop.is_set() and not self.live.thread.is_alive():
            self.finish_live()
            return
        transport = self.live.capture.transport
        port = self.live.port_name
        if self.live.ready:
            rates = [
                timing["estimated_sample_rate_hz"]
                for timing in self.live.health()["timing"].values()
                if timing["estimated_sample_rate_hz"] is not None
            ]
            rate = (
                f" · {sum(rates) / len(rates):.1f} samples/s" if len(rates) == 3 else ""
            )
            self.connection.set(f"{TRANSPORTS[transport]} ({port}){rate}")
            if self.live_port_id:
                self.devices[transport] = self.live_port_id
                self.save_preferences()
                self.live_port_id = None
        elif self.live.opened.is_set():
            if transport == "ble":
                self.connection.set(f"Waiting for the vest (receiver on {port})…")
            else:
                self.connection.set(f"Waiting for ECG on {port}…")
        self.draw(self.live)
        if self.tabs.select() == str(self.serial_tab) and not self.pause_serial.get():
            self.raw_cursor, text = self.live.raw_since(self.raw_cursor)
            self.show_serial(text)
        state, path, seconds, error = self.live.recording_status()
        self.record_label.set(
            f"Recording {elapsed(seconds)} | {path.name}"
            if state == "recording" and path
            else {"starting": "Starting recording…", "stopping": "Saving…"}.get(
                state, ""
            )
        )
        if state == "idle" and self.last_record_state != "idle":
            self.notice.set(
                str(error) if error else (f"Saved {path.name}" if path else "")
            )
            self.library_dirty = True
        self.last_record_state = state

    def tick_replay(self):
        try:
            self.replay.advance()
        except (OSError, ValueError) as error:
            self.close_replay()
            self.notice.set(str(error))
            return
        if not self.seeking and not self.replay.seeking:
            self.position.set(self.replay.position)
        self.position_label.set(
            f"{elapsed(self.position.get())} / {elapsed(self.replay.duration)}"
        )
        label = (
            "Restart"
            if self.replay.done
            else ("Play" if self.replay.paused else "Pause")
        )
        for _, play, _ in self.replay_controls:
            play.configure(text=label)
        self.draw(self.replay)
        if self.tabs.select() == str(self.serial_tab) and not self.pause_serial.get():
            text = self.replay.raw_text
            if self.last_raw != text:
                self.show_serial(text, replace=True)
                self.last_raw = text

    def tick(self):
        try:
            callback, result, error = self.jobs.get_nowait()
        except Empty:
            pass
        else:
            self.busy = False
            if error:
                self.notice.set(str(error))
            else:
                callback(result)
        if self.live:
            self.tick_live()
        elif self.replay:
            self.tick_replay()
        self.update_controls()
        self.timer = self.root.after(100, self.tick)

    def close(self):
        if self.busy:
            self.notice.set("Wait for the current file operation")
            return
        self.disconnect()
        if self.live:
            return
        self.close_replay()
        self.save_preferences()
        self.root.after_cancel(self.timer)
        self.root.destroy()


def main():
    root = tk.Tk()
    with Image.open(Path(__file__).with_name("assets") / "icon.png") as icon:
        photos = [
            ImageTk.PhotoImage(
                icon.resize((size, size), Image.Resampling.LANCZOS), master=root
            )
            for size in (48, 32, 16)
        ]
        root.iconphoto(True, *photos)
        root.iconphoto(False, *photos)
    if sys.argv[1:2] == ["--smoke-test"]:
        root.withdraw()
        with TemporaryDirectory() as temp:
            app = VestApp(root, state_dir=Path(temp), recordings=Path(temp))
            root.update()
            assert [axis.get_title() for axis in app.axes] == [
                f"Lead {lead}" for lead in LEADS
            ]
            app.theme_box.set("Dark")
            app.theme_box.event_generate("<<ComboboxSelected>>")
            assert app.style.theme_use() == DARK_TTK_THEME
            assert app.raw.cget("background") == DARK.surface
            assert (
                json.loads(app.settings_path.read_text(encoding="utf-8"))["theme"]
                == "Dark"
            )
            heart = app.vector.heart
            assert not any(len(trace.get_xdata()) for _, trace, _ in heart.strips)
            if len(sys.argv) == 3:
                path = Path(sys.argv[2])
                player = DesktopPlayback(path)
                app.start_replay(path, player)
                player.pause()
                player.seek(player.duration)
                while player.seeking:
                    app.tick_replay()
                assert app.has_trace and len(app.lines[0].get_xdata()) > 0
                assert "E2,D," in player.raw_text
                plot_image = Path(temp) / "ECG.png"
                app.figure.savefig(plot_image, dpi=180, format="png")
                with plot_image.open("rb") as image:
                    assert image.read(8) == b"\x89PNG\r\n\x1a\n"
                app.tabs.select(app.vector_tab)
                app.vector.tabs.select(app.vector.frontal_tab)
                app.tick_replay()
                frontal = app.vector.frontal
                assert len(frontal.trail.get_segments()) and frontal.arrow.get_visible()
                assert frontal.cursor.get_xdata()[0] == frontal.times[-1]
                app.vector.tabs.select(app.vector.heart_tab)
                app.tick_replay()
                heart.canvas.draw()
                assert len(heart.spatial.trail.get_segments())
                assert all(len(trace.get_xdata()) for _, trace, _ in heart.strips)
                assert all(point[2] == 0 for point in heart.vest_points)
                assert all(
                    cursor.get_xdata()[0] == heart.vest_times[-1]
                    for _, _, cursor in heart.strips
                )
            app.theme_box.set("Light")
            app.theme_box.event_generate("<<ComboboxSelected>>")
            assert app.style.theme_use() == app.light_theme
            assert app.raw.cget("background") == LIGHT.surface
            app.close()
    else:
        VestApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
