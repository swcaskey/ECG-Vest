"""Render the vest's frontal-plane trajectory against a rotatable heart reference."""

from math import isfinite
from pathlib import Path

import numpy as np
from matplotlib.colors import to_rgba
from matplotlib.ticker import MaxNLocator
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

TRAJECTORY_COLOR = "#156b83"
ARROW_COLOR = "#683762"
FRONTAL_GRID = "#b8cbd1"
VIEW_BACKGROUND = "#fbfcfd"
HEART_MODEL = Path(__file__).with_name("data") / "heart_reference.npz"


def _heart_colors(faces, colors):
    normal = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    light = np.array((-0.32, -0.46, 0.83))
    lengths = np.maximum(np.linalg.norm(normal, axis=1), np.finfo(float).eps)
    shade = np.abs(normal @ light) / lengths
    shaded = np.clip(colors * (0.76 + 0.30 * shade[:, None]), 0, 1)
    return np.column_stack((shaded, np.full(len(faces), 0.38)))


class SpatialPlot:
    """Update the vest's vector trail without resetting the camera."""

    def __init__(self, axis):
        self.axis = axis
        self.trace_color = TRAJECTORY_COLOR
        axis.set_facecolor(VIEW_BACKGROUND)
        axis.tick_params(labelsize=8, pad=0, colors="#425963")
        axis.set_box_aspect((1, 1, 1), zoom=0.95)
        axis.set_proj_type("ortho")
        axis.computed_zorder = False
        axis.set(
            xlabel="Left-right (ADC counts)",
            ylabel="Superior-inferior (ADC counts)",
            zticks=[],
        )
        axis.zaxis.line.set_visible(False)
        axis.grid(False)
        for direction in (axis.xaxis, axis.yaxis, axis.zaxis):
            direction.pane.set_visible(False)
        for direction in (axis.xaxis, axis.yaxis):
            direction.label.set_size(9)
            direction.label.set_color("#425963")
            direction.labelpad = 4
            direction.set_major_locator(
                MaxNLocator(nbins=4, symmetric=True, integer=True)
            )
        self.plane = Poly3DCollection(
            [],
            facecolors=[to_rgba("#dfedf0", 0.38)],
            edgecolors=FRONTAL_GRID,
            linewidths=0.8,
            zorder=1.1,
        )
        self.plane_grid = Line3DCollection(
            [], colors=to_rgba(FRONTAL_GRID, 0.63), linewidths=0.8, zorder=1.2
        )
        axis.add_collection3d(self.plane, autolim=False)
        axis.add_collection3d(self.plane_grid, autolim=False)
        with np.load(HEART_MODEL, allow_pickle=False) as model:
            self.heart_faces = model["faces"]
            heart_colors = _heart_colors(self.heart_faces, model["colors"])
            self.credits_text = model["credit"].item()
            self.credits_url = model["source"].item()
            self.license_url = model["license"].item()
            self.changes_text = model["changes"].item()
        self.heart = Poly3DCollection(
            [],
            facecolors=heart_colors,
            edgecolors="none",
            zorder=1.5,
        )
        axis.add_collection3d(self.heart, autolim=False)
        self.trail = Line3DCollection([], linewidths=2.4, zorder=2.0)
        self.arrow = Line3DCollection(
            [], linewidths=1.4, colors=ARROW_COLOR, zorder=2.1
        )
        axis.add_collection3d(self.trail, autolim=False)
        axis.add_collection3d(self.arrow, autolim=False)
        (self.dot,) = axis.plot(
            [],
            [],
            [],
            marker="o",
            markersize=4.5,
            linestyle="none",
            color=TRAJECTORY_COLOR,
            zorder=2.2,
        )
        self.reset_view()
        self.set_extent(1000)

    def apply_theme(self, colors):
        self.axis.set_facecolor(colors.surface)
        self.axis.tick_params(colors=colors.muted)
        for direction in (self.axis.xaxis, self.axis.yaxis):
            direction.label.set_color(colors.muted)
            direction.line.set_color(colors.border)

        self.plane.set_facecolor(to_rgba(colors.plane, 0.38))
        self.plane.set_edgecolor(colors.border)
        self.plane_grid.set_color(to_rgba(colors.grid, 0.63))
        self.arrow.set_color(colors.vector)
        self.dot.set_color(colors.trace)
        self.trace_color = colors.trace
        trail_colors = self.trail.get_colors().copy()
        trail_colors[:, :3] = to_rgba(self.trace_color)[:3]
        self.trail.set_color(trail_colors)

    def reset_view(self):
        self.axis.view_init(elev=14, azim=-24, vertical_axis="y")

    def set_extent(self, limit):
        if not isfinite(limit) or limit <= 0:
            raise ValueError("Spatial plot extent must be positive and finite")
        self.limit = limit
        limits = (-limit, limit)
        self.axis.set(xlim=limits, ylim=limits[::-1], zlim=limits)
        self.heart.set_verts(self.heart_faces * limit)
        self.plane.set_verts(
            [
                [
                    (-limit, -limit, 0),
                    (limit, -limit, 0),
                    (limit, limit, 0),
                    (-limit, limit, 0),
                ]
            ]
        )
        locations = (-0.5 * limit, 0, 0.5 * limit)
        self.plane_grid.set_segments(
            [((-limit, y, 0), (limit, y, 0)) for y in locations]
            + [((x, -limit, 0), (x, limit, 0)) for x in locations]
        )

    def clear(self):
        self.trail.set_segments([])
        self.arrow.set_segments([])
        self.dot.set_data_3d([], [], [])

    def draw(self, points):
        """Draw the newest continuous run from an array of frontal-plane points."""
        points = np.asarray(points, dtype=float)
        if not points.size:
            self.clear()
            return
        # Keep the latest continuous run if the caller supplies missing samples.
        gaps = np.flatnonzero(~np.isfinite(points).all(axis=1))
        if gaps.size:
            points = points[gaps[-1] + 1 :]
        if not len(points):
            self.clear()
            return
        points = points.copy()
        points[:, 2] = 0

        segments = np.stack((points[:-1], points[1:]), axis=1)
        self.trail.set_segments(segments)
        colors = np.tile(to_rgba(self.trace_color), (len(segments), 1))
        colors[:, 3] = np.linspace(0.12, 1.0, len(segments))
        self.trail.set_color(colors)
        tip = points[-1]
        self.dot.set_data_3d(*tip[:, None])
        self._draw_arrow(tip)

    def _draw_arrow(self, tip):
        magnitude = np.linalg.norm(tip)
        if not magnitude:
            self.arrow.set_segments([])
            return
        direction = tip / magnitude
        normal = np.array((-direction[1], direction[0], 0))
        head = min(0.17 * magnitude, 0.07 * self.limit)
        base = tip - head * direction
        self.arrow.set_segments(
            [
                (np.zeros(3), tip),
                (tip, base + 0.45 * head * normal),
                (tip, base - 0.45 * head * normal),
            ]
        )
