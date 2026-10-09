"""
Arrange the icons on the Windows desktop into tidy groups.

The desktop is an Explorer folder view, so we talk to it through the
documented shell interfaces:
    ShellWindows.FindWindowSW(SWC_DESKTOP) -> IServiceProvider
      -> IShellBrowser -> active IShellView -> IFolderView2
IFolderView2 lets us read each icon's position and move it.

Layout: one block of columns per category (folders, shortcuts, documents,
pictures, ...), alphabetical within each category, filling top-to-bottom
from the top-left of the primary monitor. Each category starts a new column.

Before arranging, the current positions are saved so `restore_layout()`
can put every icon back. Windows only.
"""
from __future__ import annotations

import ctypes
import json
from ctypes import POINTER, byref, c_int, c_uint, c_void_p, wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import (
    APP_DIR,
    EXT_ARCHIVE,
    EXT_AUDIO,
    EXT_CODE,
    EXT_DOCUMENT,
    EXT_IMAGE,
    EXT_PRESENTATION,
    EXT_SPREADSHEET,
    EXT_VIDEO,
    IS_WINDOWS,
)

LAYOUT_BACKUP_PATH = APP_DIR / "desktop_layout_backup.json"

# Category order on the desktop, left to right.
CATEGORIES: list[tuple[str, set[str]]] = [
    ("Shortcuts", {".lnk", ".url", ".website"}),
    ("Documents", EXT_DOCUMENT),
    ("Spreadsheets", EXT_SPREADSHEET | {".xlsm", ".xlsb"}),
    ("Presentations", EXT_PRESENTATION),
    ("Pictures", EXT_IMAGE),
    ("Videos", EXT_VIDEO),
    ("Audio", EXT_AUDIO),
    ("Code", EXT_CODE),
    ("Archives", EXT_ARCHIVE),
    ("Installers", {".exe", ".msi"}),
]
# Non-file items (Recycle Bin, This PC) go first, folders second, "Other" last.
_ORDER = ["System", "Folders"] + [name for name, _ in CATEGORIES] + ["Other"]


class DesktopIconError(RuntimeError):
    pass


@dataclass
class DesktopIcon:
    name: str                 # display name, e.g. "Recycle Bin", "notes.txt"
    path: Optional[Path]      # None for virtual items (Recycle Bin, This PC)
    x: int
    y: int
    category: str = ""


# ---------- COM interface definitions ----------
# Only the methods we call need accurate signatures, but every method must be
# listed so the vtable slots line up.

if IS_WINDOWS:
    import comtypes
    import comtypes.client
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IShellBrowser(IUnknown):
        _iid_ = GUID("{000214E2-0000-0000-C000-000000000046}")
        _methods_ = [
            # IOleWindow
            COMMETHOD([], HRESULT, "GetWindow", (["out"], POINTER(wintypes.HWND), "phwnd")),
            COMMETHOD([], HRESULT, "ContextSensitiveHelp", (["in"], wintypes.BOOL, "fEnterMode")),
            # IShellBrowser
            COMMETHOD([], HRESULT, "InsertMenusSB", (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetMenuSB", (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "RemoveMenusSB", (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetStatusTextSB", (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "EnableModelessSB", (["in"], wintypes.BOOL)),
            COMMETHOD([], HRESULT, "TranslateAcceleratorSB", (["in"], c_void_p), (["in"], wintypes.WORD)),
            COMMETHOD([], HRESULT, "BrowseObject", (["in"], c_void_p), (["in"], c_uint)),
            COMMETHOD([], HRESULT, "GetViewStateStream", (["in"], wintypes.DWORD), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "GetControlWindow", (["in"], c_uint), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SendControlMsg", (["in"], c_uint), (["in"], c_uint),
                      (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "QueryActiveShellView", (["out"], POINTER(POINTER(IUnknown)), "ppshv")),
            COMMETHOD([], HRESULT, "OnViewWindowActive", (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetToolbarItems", (["in"], c_void_p), (["in"], c_uint), (["in"], c_uint)),
        ]

    class IFolderView2(IUnknown):
        _iid_ = GUID("{1af3a467-214f-4298-908e-06b03e0b39f9}")
        _methods_ = [
            # IFolderView
            COMMETHOD([], HRESULT, "GetCurrentViewMode", (["out"], POINTER(c_uint))),
            COMMETHOD([], HRESULT, "SetCurrentViewMode", (["in"], c_uint)),
            COMMETHOD([], HRESULT, "GetFolder", (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "Item", (["in"], c_int, "iItemIndex"), (["out"], POINTER(c_void_p), "ppidl")),
            COMMETHOD([], HRESULT, "ItemCount", (["in"], c_uint, "uFlags"), (["out"], POINTER(c_int), "pcItems")),
            COMMETHOD([], HRESULT, "Items", (["in"], c_uint), (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "GetSelectionMarkedItem", (["out"], POINTER(c_int))),
            COMMETHOD([], HRESULT, "GetFocusedItem", (["out"], POINTER(c_int))),
            COMMETHOD([], HRESULT, "GetItemPosition", (["in"], c_void_p, "pidl"),
                      (["out"], POINTER(wintypes.POINT), "ppt")),
            COMMETHOD([], HRESULT, "GetSpacing", (["out"], POINTER(wintypes.POINT), "ppt")),
            COMMETHOD([], HRESULT, "GetDefaultSpacing", (["out"], POINTER(wintypes.POINT), "ppt")),
            COMMETHOD([], HRESULT, "GetAutoArrange"),
            COMMETHOD([], HRESULT, "SelectItem", (["in"], c_int), (["in"], wintypes.DWORD)),
            COMMETHOD([], HRESULT, "SelectAndPositionItems", (["in"], c_uint, "cidl"),
                      (["in"], POINTER(c_void_p), "apidl"), (["in"], POINTER(wintypes.POINT), "apt"),
                      (["in"], wintypes.DWORD, "dwFlags")),
            # IFolderView2
            COMMETHOD([], HRESULT, "SetGroupBy", (["in"], c_void_p), (["in"], wintypes.BOOL)),
            COMMETHOD([], HRESULT, "GetGroupBy", (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetViewProperty", (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "GetViewProperty", (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetTileViewProperties", (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetExtendedTileViewProperties", (["in"], c_void_p), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetText", (["in"], c_int), (["in"], c_void_p)),
            COMMETHOD([], HRESULT, "SetCurrentFolderFlags", (["in"], wintypes.DWORD, "dwMask"),
                      (["in"], wintypes.DWORD, "dwFlags")),
            COMMETHOD([], HRESULT, "GetCurrentFolderFlags", (["out"], POINTER(wintypes.DWORD), "pdwFlags")),
        ]

    _CLSID_ShellWindows = "{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"
    _SID_STopLevelBrowser = GUID("{4C96BE40-915C-11CF-99D3-00AA004AE837}")

    _shell32 = ctypes.windll.shell32
    _ole32 = ctypes.windll.ole32
    _user32 = ctypes.windll.user32
    _shell32.SHGetNameFromIDList.argtypes = [c_void_p, c_int, POINTER(c_void_p)]
    _ole32.CoTaskMemFree.argtypes = [c_void_p]

CSIDL_DESKTOP = 0
SWC_DESKTOP = 8
SWFO_NEEDDISPATCH = 1
SVGIO_ALLVIEW = 2
SVSI_POSITIONITEM = 0x80
FWF_AUTOARRANGE = 0x1
SIGDN_NORMALDISPLAY = 0
SIGDN_FILESYSPATH = -2147123200   # 0x80058000
SPI_GETWORKAREA = 0x0030
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77


# ---------- low-level helpers ----------

def _desktop_view() -> "IFolderView2":
    if not IS_WINDOWS:
        raise DesktopIconError("Arranging desktop icons is only supported on Windows.")
    try:
        windows = comtypes.client.CreateObject(_CLSID_ShellWindows)
        _hwnd, disp = windows.FindWindowSW(CSIDL_DESKTOP, None, SWC_DESKTOP, SWFO_NEEDDISPATCH)
        provider = disp.QueryInterface(comtypes.IServiceProvider)
        browser = provider.QueryService(_SID_STopLevelBrowser, IShellBrowser)
        view = browser.QueryActiveShellView()
        return view.QueryInterface(IFolderView2)
    except Exception as e:
        raise DesktopIconError(f"Could not reach the desktop (is Explorer running?): {e}") from e


def _name_of(pidl: int, sigdn: int) -> Optional[str]:
    out = c_void_p()
    # Desktop items' pidls are relative to the desktop, which is the
    # namespace root, so they're already absolute.
    if _shell32.SHGetNameFromIDList(pidl, sigdn, byref(out)) != 0:
        return None
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _ole32.CoTaskMemFree(out)


def _categorize(icon: DesktopIcon) -> str:
    if icon.path is None:
        return "System"
    if icon.path.is_dir():
        return "Folders"
    ext = icon.path.suffix.lower()
    for name, exts in CATEGORIES:
        if ext in exts:
            return name
    return "Other"


def _read_icons(view) -> tuple[list[DesktopIcon], list[int]]:
    """Return icons and their pidls (caller must free the pidls)."""
    icons, pidls = [], []
    for i in range(view.ItemCount(SVGIO_ALLVIEW)):
        pidl = view.Item(i)
        pt = view.GetItemPosition(pidl)
        fs_path = _name_of(pidl, SIGDN_FILESYSPATH)
        icon = DesktopIcon(
            name=_name_of(pidl, SIGDN_NORMALDISPLAY) or f"item {i}",
            path=Path(fs_path) if fs_path else None,
            x=pt.x,
            y=pt.y,
        )
        icon.category = _categorize(icon)
        icons.append(icon)
        pidls.append(pidl)
    return icons, pidls


def _free(pidls: list[int]) -> None:
    for p in pidls:
        _ole32.CoTaskMemFree(p)


def _key(icon: DesktopIcon) -> str:
    """Stable identity for the backup file: full path, or name for virtual items."""
    return str(icon.path) if icon.path else f"::{icon.name}"


def _work_area_in_view() -> tuple[int, int, int, int]:
    """
    Primary monitor's work area (minus taskbar), in the desktop view's
    coordinates. The view spans the whole virtual screen, so shift by its origin.
    """
    rect = wintypes.RECT()
    _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, byref(rect), 0)
    ox = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    oy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return rect.left - ox, rect.top - oy, rect.right - ox, rect.bottom - oy


def _position(view, pidls: list[int], points: list[tuple[int, int]]) -> None:
    n = len(pidls)
    if n == 0:
        return
    arr = (c_void_p * n)(*pidls)
    pts = (wintypes.POINT * n)(*[wintypes.POINT(x, y) for x, y in points])
    # Auto-arrange would immediately undo custom positions.
    view.SetCurrentFolderFlags(FWF_AUTOARRANGE, 0)
    view.SelectAndPositionItems(n, arr, pts, SVSI_POSITIONITEM)


# ---------- public API ----------

def list_icons() -> list[DesktopIcon]:
    view = _desktop_view()
    icons, pidls = _read_icons(view)
    _free(pidls)
    return icons


def _grid_offset(icons: list[DesktopIcon], sx: int, sy: int) -> tuple[int, int]:
    """
    Explorer's icon grid isn't at (0, 0) — positions include a small margin.
    Use the offset most existing icons already sit on.
    """
    offsets: dict[tuple[int, int], int] = {}
    for i in icons:
        key = (i.x % sx, i.y % sy)
        offsets[key] = offsets.get(key, 0) + 1
    return max(offsets, key=offsets.get) if offsets else (0, 0)


def plan_layout(icons: list[DesktopIcon], spacing: tuple[int, int],
                area: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """
    Pure layout math: return a position for each icon (same order as `icons`).
    Categories in _ORDER, alphabetical inside, each category starts a new column.
    """
    sx, sy = spacing
    ox, oy = _grid_offset(icons, sx, sy)
    left, top, _right, bottom = area
    left, top = left + ox, top + oy
    rows = max(1, (bottom - top) // sy)

    order = sorted(
        range(len(icons)),
        key=lambda i: (_ORDER.index(icons[i].category), icons[i].name.lower()),
    )
    positions: list[tuple[int, int]] = [(0, 0)] * len(icons)
    col = row = 0
    prev_cat = None
    for i in order:
        cat = icons[i].category
        if prev_cat is not None and (cat != prev_cat or row >= rows):
            col += 1
            row = 0
        positions[i] = (left + col * sx, top + row * sy)
        row += 1
        prev_cat = cat
    return positions


def arrange_desktop() -> dict[str, int]:
    """
    Save the current layout, then arrange icons by category.
    Returns {category: count} for display.
    """
    view = _desktop_view()
    icons, pidls = _read_icons(view)
    try:
        auto_arrange = bool(view.GetCurrentFolderFlags() & FWF_AUTOARRANGE)
        save_layout(icons, auto_arrange)
        sp = view.GetSpacing()
        positions = plan_layout(icons, (sp.x, sp.y), _work_area_in_view())
        _position(view, pidls, positions)
    finally:
        _free(pidls)

    counts: dict[str, int] = {}
    for icon in icons:
        counts[icon.category] = counts.get(icon.category, 0) + 1
    return counts


def save_layout(icons: list[DesktopIcon], auto_arrange: bool,
                path: Path = LAYOUT_BACKUP_PATH) -> None:
    data = {
        "auto_arrange": auto_arrange,
        "positions": {_key(i): [i.x, i.y] for i in icons},
    }
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def has_saved_layout(path: Path = LAYOUT_BACKUP_PATH) -> bool:
    return path.exists()


def restore_layout(path: Path = LAYOUT_BACKUP_PATH) -> int:
    """Put icons back where they were before the last arrange. Returns how many moved."""
    if not path.exists():
        raise DesktopIconError("No saved desktop layout to restore.")
    data = json.loads(path.read_text(encoding="utf-8"))
    saved = data["positions"]

    view = _desktop_view()
    icons, pidls = _read_icons(view)
    try:
        sel_pidls, points = [], []
        for icon, pidl in zip(icons, pidls):
            pos = saved.get(_key(icon))
            if pos is not None:
                sel_pidls.append(pidl)
                points.append((pos[0], pos[1]))
        _position(view, sel_pidls, points)
        if data.get("auto_arrange"):
            view.SetCurrentFolderFlags(FWF_AUTOARRANGE, FWF_AUTOARRANGE)
    finally:
        _free(pidls)
    return len(sel_pidls)
