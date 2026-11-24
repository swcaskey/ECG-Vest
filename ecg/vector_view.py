"""Frontal-plane views of live or recorded vest ECG."""

import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.collections import LineCollection
from matplotlib.colors import to_rgba
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch
import numpy as np

from .desktop_theme import style_axis
from .protocol import LEADS
from .spatial_plot import SpatialPlot
from .timing import RATE, WINDOW
from .vector import einthoven_residual, frontal_coordinates, trailing_indices

TRACE = "#17677d"
CURSOR = "#b86a27"


class FrontalView:
    def __init__(self, parent):
        self.following = True
        self.times = np.empty(0)
        self.points = np.empty((0, 2))
        self.dragging = False

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(0, 4))
        self.source = tk.StringVar(value="Connect the vest or open a recording")
        ttk.Label(actions, textvariable=self.source).pack(side="left")
        self.follow = ttk.Button(actions, text="Follow Latest", command=self.resume)
        self.follow.pack(side="right")
        self.follow.state(["disabled"])
        self.agreement = tk.StringVar()
        ttk.Label(actions, textvariable=self.agreement).pack(side="right", padx=12)

        figure = Figure(figsize=(7, 5), layout="constrained")
        self.plane, self.rhythm = figure.subplots(2, 1, height_ratios=(4, 1))
        self.plane.set(
            xlabel="Left (+) · ADC counts",
            ylabel="Inferior (+) · ADC counts",
            xlim=(-1, 1),
            ylim=(1, -1),
            aspect="equal",
        )
        self.guides = (
            self.plane.axhline(0, linewidth=0.7, color="#b2b9bd"),
            self.plane.axvline(0, linewidth=0.7, color="#b2b9bd"),
        )
        self.plane.grid(alpha=0.18)
        self.trail = LineCollection([], linewidths=1.9)
        self.plane.add_collection(self.trail)
        self.arrow = FancyArrowPatch(
            (0, 0), (0, 0), arrowstyle="-|>", mutation_scale=13, color=CURSOR
        )
        self.arrow.set_visible(False)
        self.plane.add_patch(self.arrow)
        self.rhythm.set(
            title="Lead II · drag to inspect",
            xlabel="Time relative to latest sample (s)",
            xlim=(-WINDOW, 0),
        )
        self.rhythm.grid(alpha=0.2)
        (self.trace,) = self.rhythm.plot([], [], color=TRACE, linewidth=1)
        self.cursor = self.rhythm.axvline(0, color=CURSOR, linewidth=1, visible=False)
        self.canvas = FigureCanvasTkAgg(figure, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self.begin_inspection)
        self.canvas.mpl_connect("motion_notify_event", self.inspect)
        self.canvas.mpl_connect("button_release_event", self.end_inspection)

    def apply_theme(self, colors):
        self.canvas.figure.set_facecolor(colors.background)
        self.canvas.get_tk_widget().configure(background=colors.background)
        for axis in (self.plane, self.rhythm):
            style_axis(axis, colors)
        for guide in self.guides:
            guide.set_color(colors.border)
        self.trace.set_color(colors.trace)
        self.cursor.set_color(colors.cursor)
        self.arrow.set_color(colors.cursor)
        self.color_trail(len(self.trail.get_segments()))
        self.canvas.draw_idle()

    def update(self, times, traces, name):
        if not self.following:
            return
        if not len(times):
            self.clear(name)
            return
        self.source.set(name)
        self.times = np.asarray(times)
        self.points = np.column_stack(frontal_coordinates(traces))
        self.trace.set_data(self.times, traces[1])
        self.rhythm.relim()
        self.rhythm.autoscale_view(scalex=False)
        residuals = np.asarray(einthoven_residual(traces))
        residuals = residuals[np.isfinite(residuals)]
        self.agreement.set(
            f"II − I − III RMS  {np.sqrt(np.mean(residuals ** 2)):,.0f} counts"
            if residuals.size
            else ""
        )
        finite = self.points[np.isfinite(self.points)]
        limit = max(1.0, np.max(np.abs(finite)) * 1.1) if finite.size else 1.0
        self.plane.set(xlim=(-limit, limit), ylim=(limit, -limit))
        self.show_cursor(len(self.times) - 1)

    def show_cursor(self, index):
        t = self.times[index]
        indices = trailing_indices(self.times, self.points.T, t, max_gap=1.5 / RATE)
        points = self.points[indices.start : indices.stop]
        self.trail.set_segments(np.stack((points[:-1], points[1:]), axis=1))
        self.color_trail(max(0, len(points) - 1))
        self.arrow.set_visible(bool(len(points)))
        if len(points):
            self.arrow.set_positions((0, 0), points[-1])
        self.cursor.set_visible(True)
        self.cursor.set_xdata((t, t))
        self.canvas.draw_idle()

    def color_trail(self, segment_count):
        color = self.trace.get_color()
        self.trail.set_colors(
            [to_rgba(color, alpha) for alpha in np.linspace(0.12, 1, segment_count)]
        )

    def begin_inspection(self, event):
        if event.inaxes is self.rhythm and event.button == 1 and len(self.times):
            self.dragging = True
            self.following = False
            self.follow.state(["!disabled"])
            self.inspect(event)

    def inspect(self, event):
        if self.dragging and event.inaxes is self.rhythm:
            index = int(np.abs(self.times - event.xdata).argmin())
            self.show_cursor(index)

    def end_inspection(self, _event):
        self.dragging = False

    def resume(self):
        self.following = True
        self.follow.state(["disabled"])
        if len(self.times):
            self.show_cursor(len(self.times) - 1)

    def clear(self, message="Connect the vest or open a recording"):
        self.times = np.empty(0)
        self.points = np.empty((0, 2))
        self.dragging = False
        self.resume()
        self.source.set(message)
        self.agreement.set("")
        self.trace.set_data([], [])
        self.trail.set_segments([])
        self.arrow.set_visible(False)
        self.cursor.set_visible(False)
        self.canvas.draw_idle()


class HeartView:
    def __init__(self, parent):
        self.vest_times = np.empty(0)
        self.vest_points = np.empty((0, 3))
        self.hold = False
        self.message = tk.StringVar(value="Connect the vest or open a recording")

        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, textvariable=self.message).pack(side="left")
        ttk.Button(top, text="Reset View", command=self.reset_view).pack(side="right")

        figure = Figure(figsize=(7, 5))
        grid = figure.add_gridspec(
            3,
            2,
            width_ratios=(1.25, 1),
            left=0.02,
            right=0.98,
            bottom=0.12,
            top=0.94,
            wspace=0.22,
            hspace=0.4,
        )
        self.spatial = SpatialPlot(figure.add_subplot(grid[:, 0], projection="3d"))
        self.strips = []
        for row, (lead, color) in enumerate(zip(LEADS, (TRACE, "#785578", "#ab6534"))):
            first = self.strips[0][0] if self.strips else None
            axis = figure.add_subplot(grid[row, 1], sharex=first, sharey=first)
            axis.set_title(f"Lead {lead}", loc="left", fontsize=10)
            axis.set_facecolor("#f6f8fa")
            axis.grid(alpha=0.24)
            axis.tick_params(labelsize=8, labelbottom=row == 2)
            for border in axis.spines.values():
                border.set_color("#c7d1d9")
            (trace,) = axis.plot([], [], color=color, linewidth=1.15)
            cursor = axis.axvline(0, color="#394957", linewidth=1, visible=False)
            self.strips.append((axis, trace, cursor))
        self.strips[0][0].set_xlim(-WINDOW, 0)
        self.strips[-1][0].set_xlabel("Time relative to latest sample (s)")
        self.canvas = FigureCanvasTkAgg(figure, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self.seek_strip)

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(5, 2))
        self.follow_button = ttk.Button(
            controls, text="Follow Latest", command=self.follow_latest
        )
        self.follow_button.pack(side="right")
        self.follow_button.state(["disabled"])
        self.replay_controls = ttk.Frame(controls)
        self.replay_controls.pack(side="left", fill="x", expand=True, padx=(0, 8))

        model_credits = ttk.Frame(parent)
        model_credits.pack(fill="x")
        ttk.Label(
            model_credits, text="Heart model · Human Reference Atlas · CC BY 4.0"
        ).pack(side="left")
        ttk.Button(
            model_credits, text="Model Credits", command=self.show_model_credits
        ).pack(side="right")

    def apply_theme(self, colors):
        self.canvas.figure.set_facecolor(colors.background)
        self.canvas.get_tk_widget().configure(background=colors.background)
        self.spatial.apply_theme(colors)
        for lead, color, (axis, trace, cursor) in zip(
            LEADS, (colors.trace, colors.secondary, colors.tertiary), self.strips
        ):
            style_axis(axis, colors)
            axis.set_title(f"Lead {lead}", loc="left", fontsize=10, color=colors.text)
            trace.set_color(color)
            cursor.set_color(colors.muted)
        self.canvas.draw_idle()

    def reset_view(self):
        self.spatial.reset_view()
        self.canvas.draw_idle()

    def show_model_credits(self):
        view = self.spatial
        messagebox.showinfo(
            "Heart Model Credits",
            "\n\n".join(
                (
                    view.credits_text,
                    view.changes_text,
                    view.credits_url,
                    view.license_url,
                )
            ),
            parent=self.canvas.get_tk_widget(),
        )

    def set_traces(self, times, traces):
        for i, (axis, trace, _) in enumerate(self.strips):
            trace.set_data(times, traces[i] if len(times) else ())
            axis.relim()
        if len(times):
            self.strips[0][0].autoscale_view(scalex=False)

    def set_cursor(self, t):
        for _, _, cursor in self.strips:
            cursor.set_visible(t is not None)
            if t is not None:
                cursor.set_xdata((t, t))

    def clear_vest(self, message="Connect the vest or open a recording"):
        self.hold = False
        self.follow_button.state(["disabled"])
        self.vest_times = np.empty(0)
        self.vest_points = np.empty((0, 3))
        self.message.set(message)
        self.spatial.clear()
        self.set_traces((), ())
        self.set_cursor(None)
        self.canvas.draw_idle()

    def update_vest(self, times, traces, message):
        if not len(times):
            self.clear_vest(message)
        elif not self.hold:
            self.message.set(message)
            self.vest_times = np.asarray(times)
            x, y = frontal_coordinates(traces)
            self.vest_points = np.column_stack((x, y, np.zeros(len(times))))
            finite = self.vest_points[np.isfinite(self.vest_points)]
            limit = max(1.0, np.max(np.abs(finite)) * 1.15)
            self.spatial.set_extent(limit)
            self.set_traces(self.vest_times, traces)
            self.draw_vest(self.vest_times[-1])

    def draw_vest(self, t):
        indices = trailing_indices(
            self.vest_times, self.vest_points.T, t, max_gap=1.5 / RATE
        )
        self.spatial.draw(self.vest_points[indices.start : indices.stop])
        self.set_cursor(t)
        self.canvas.draw_idle()

    def follow_latest(self):
        self.hold = False
        self.follow_button.state(["disabled"])
        if len(self.vest_times):
            self.draw_vest(self.vest_times[-1])

    def seek_strip(self, event):
        if (
            not len(self.vest_times)
            or event.button != 1
            or all(event.inaxes is not axis for axis, _, _ in self.strips)
        ):
            return
        self.hold = True
        self.follow_button.state(["!disabled"])
        index = int(np.abs(self.vest_times - event.xdata).argmin())
        self.draw_vest(self.vest_times[index])


class VectorView:
    def __init__(self, parent):
        self.tabs = ttk.Notebook(parent)
        self.tabs.pack(fill="both", expand=True)
        self.frontal_tab = ttk.Frame(self.tabs, padding=8)
        self.heart_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.heart_tab, text="Heart & Leads")
        self.tabs.add(self.frontal_tab, text="Frontal Plane")
        self.frontal = FrontalView(self.frontal_tab)
        self.heart = HeartView(self.heart_tab)

    def apply_theme(self, colors):
        self.frontal.apply_theme(colors)
        self.heart.apply_theme(colors)

    @property
    def showing_frontal(self):
        return self.tabs.select() == str(self.frontal_tab)
