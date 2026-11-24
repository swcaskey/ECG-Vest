"""Colors and native widget styles for the desktop app."""

from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class Palette:
    background: str
    surface: str
    text: str
    muted: str
    border: str
    grid: str
    trace: str
    secondary: str
    tertiary: str
    cursor: str
    vector: str
    plane: str


LIGHT = Palette(
    background="#ffffff",
    surface="#fbfcfd",
    text="#202930",
    muted="#425963",
    border="#c7d1d9",
    grid="#b8cbd1",
    trace="#156b83",
    secondary="#785578",
    tertiary="#ab6534",
    cursor="#b86a27",
    vector="#683762",
    plane="#dfedf0",
)
DARK = Palette(
    background="#15191f",
    surface="#1c222b",
    text="#e8eef5",
    muted="#aab7c6",
    border="#445164",
    grid="#65758a",
    trace="#68cce3",
    secondary="#c8a9e8",
    tertiary="#f3ad79",
    cursor="#f4bf77",
    vector="#e4abdc",
    plane="#4b6376",
)
DARK_TTK_THEME = "ecg-dark"
SELECTION = "#285574"


def create_dark_theme(style):
    if DARK_TTK_THEME in style.theme_names():
        return
    hover = "#2d3a49"
    disabled = "#778596"
    style.theme_create(
        DARK_TTK_THEME,
        parent="clam",
        settings={
            ".": {
                "configure": {
                    "background": DARK.background,
                    "foreground": DARK.text,
                    "fieldbackground": DARK.surface,
                    "troughcolor": DARK.surface,
                    "bordercolor": DARK.border,
                    "lightcolor": DARK.border,
                    "darkcolor": DARK.border,
                    "selectbackground": SELECTION,
                    "selectforeground": DARK.text,
                    "focuscolor": DARK.trace,
                    "arrowcolor": DARK.text,
                },
                "map": {"foreground": [("disabled", disabled)]},
            },
            "TButton": {
                "configure": {"background": DARK.surface},
                "map": {
                    "background": [("pressed", SELECTION), ("active", hover)],
                },
            },
            "TCheckbutton": {
                "map": {
                    "background": [("active", hover)],
                    "indicatorbackground": [
                        ("selected", SELECTION),
                        ("", DARK.surface),
                    ],
                    "indicatorforeground": [("", DARK.text)],
                },
            },
            "TRadiobutton": {
                "map": {
                    "background": [("active", hover)],
                    "indicatorbackground": [
                        ("selected", SELECTION),
                        ("", DARK.surface),
                    ],
                    "indicatorforeground": [("", DARK.text)],
                },
            },
            "TCombobox": {
                "map": {
                    "fieldbackground": [("readonly", DARK.surface)],
                    "background": [("active", hover), ("readonly", DARK.surface)],
                    "selectbackground": [("readonly", DARK.surface)],
                },
            },
            "TNotebook.Tab": {
                "configure": {"background": DARK.surface},
                "map": {
                    "background": [("selected", SELECTION), ("active", hover)],
                    "foreground": [("selected", DARK.text), ("", DARK.muted)],
                },
            },
            "Treeview": {
                "configure": {"background": DARK.surface},
                "map": {
                    "background": [("selected", SELECTION)],
                    "foreground": [("selected", DARK.text)],
                },
            },
            "Treeview.Heading": {"configure": {"background": hover}},
            "TScale": {"configure": {"background": DARK.border}},
            "TScrollbar": {"configure": {"background": DARK.border}},
        },
    )


def style_axis(axis, colors):
    axis.set_facecolor(colors.surface)
    axis.tick_params(colors=colors.muted, grid_color=colors.grid)
    for label in (axis.title, axis.xaxis.label, axis.yaxis.label):
        label.set_color(colors.text)
    for spine in axis.spines.values():
        spine.set_color(colors.border)


def style_title_bar(window, dark):
    if sys.platform != "win32":
        return
    from ctypes import byref, sizeof, windll
    from ctypes.wintypes import DWORD, HWND

    window.update_idletasks()
    handle = HWND(int(window.frame(), 0))
    # Windows 11 caption and text colors. Older versions use the system title bar.
    for attribute, color in ((35, DARK.background), (36, DARK.text)):
        value = DWORD(
            int.from_bytes(bytes.fromhex(color[1:]), "little") if dark else 0xFFFFFFFF
        )
        windll.dwmapi.DwmSetWindowAttribute(
            handle, attribute, byref(value), sizeof(value)
        )
