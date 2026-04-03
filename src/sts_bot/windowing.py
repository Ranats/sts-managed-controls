from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass, field
from ctypes import wintypes

from sts_bot.config import Rect

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
MONITOR_DEFAULTTONEAREST = 2
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


@dataclass(slots=True)
class WindowSelector:
    process_name: str | None = None
    title_regex: str | None = None
    class_name: str | None = None
    pid: int | None = None
    hwnd: int | None = None


@dataclass(slots=True)
class TargetWindow:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str
    window_rect: Rect
    client_rect: Rect
    dpi: int
    valid: bool = True
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def scale(self) -> float:
        return self.dpi / 96.0 if self.dpi else 1.0

    @property
    def client_size(self) -> tuple[int, int]:
        return (self.client_rect.width, self.client_rect.height)

    def refresh(self) -> "TargetWindow":
        refreshed = describe_window(self.hwnd)
        self.title = refreshed.title
        self.class_name = refreshed.class_name
        self.pid = refreshed.pid
        self.process_name = refreshed.process_name
        self.window_rect = refreshed.window_rect
        self.client_rect = refreshed.client_rect
        self.dpi = refreshed.dpi
        self.valid = refreshed.valid
        self.metadata = dict(refreshed.metadata)
        return self


class WindowLocator:
    def __init__(self, selector: WindowSelector) -> None:
        self.selector = selector

    def locate(self) -> TargetWindow:
        if self.selector.hwnd is not None:
            return describe_window(self.selector.hwnd)

        title_pattern = re.compile(self.selector.title_regex, re.IGNORECASE) if self.selector.title_regex else None
        class_pattern = re.compile(self.selector.class_name, re.IGNORECASE) if self.selector.class_name else None
        process_pattern = re.compile(self.selector.process_name, re.IGNORECASE) if self.selector.process_name else None

        candidates: list[tuple[int, TargetWindow]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd: int, _: int) -> bool:
            if not user32.IsWindow(hwnd):
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            try:
                candidate = describe_window(hwnd)
            except RuntimeError:
                return True
            score = 0
            if self.selector.pid is not None:
                if candidate.pid != self.selector.pid:
                    return True
                score += 100
            if title_pattern is not None:
                if not title_pattern.search(candidate.title):
                    return True
                score += 30
            if class_pattern is not None:
                if not class_pattern.search(candidate.class_name):
                    return True
                score += 20
            if process_pattern is not None:
                if not process_pattern.search(candidate.process_name):
                    return True
                score += 40
            if title_pattern is None and class_pattern is None and process_pattern is None and self.selector.pid is None:
                score += 1
            if candidate.client_rect.width <= 0 or candidate.client_rect.height <= 0:
                return True
            candidates.append((score, candidate))
            return True

        user32.EnumWindows(enum_proc, 0)
        if not candidates:
            raise RuntimeError(
                "No matching window found for selector "
                f"(process={self.selector.process_name!r}, title_regex={self.selector.title_regex!r}, class_name={self.selector.class_name!r}, pid={self.selector.pid!r})."
            )

        candidates.sort(
            key=lambda item: (
                item[0],
                item[1].client_rect.width * item[1].client_rect.height,
                item[1].hwnd,
            ),
            reverse=True,
        )
        best = candidates[0][1]
        best.metadata["candidate_count"] = len(candidates)
        best.metadata["candidate_titles"] = [candidate.title for _, candidate in candidates[:5]]
        return best


@dataclass(slots=True)
class CoordinateTransform:
    reference_width: int
    reference_height: int
    target: TargetWindow

    def refresh(self) -> None:
        self.target.refresh()

    @property
    def scale_x(self) -> float:
        return self.target.client_rect.width / max(1, self.reference_width)

    @property
    def scale_y(self) -> float:
        return self.target.client_rect.height / max(1, self.reference_height)

    def reference_to_client(self, point: tuple[int, int]) -> tuple[int, int]:
        return (
            round(point[0] * self.scale_x),
            round(point[1] * self.scale_y),
        )

    def reference_rect_to_client(self, rect: Rect) -> Rect:
        return Rect(
            left=round(rect.left * self.scale_x),
            top=round(rect.top * self.scale_y),
            width=max(1, round(rect.width * self.scale_x)),
            height=max(1, round(rect.height * self.scale_y)),
        )

    def client_to_screen(self, point: tuple[int, int]) -> tuple[int, int]:
        return (
            self.target.client_rect.left + point[0],
            self.target.client_rect.top + point[1],
        )


@dataclass(slots=True)
class ChildWindowEntry:
    hwnd: int
    title: str
    class_name: str
    visible: bool
    enabled: bool
    client_rect: Rect
    depth: int


@dataclass(slots=True)
class GuiThreadState:
    active_hwnd: int | None
    focus_hwnd: int | None
    capture_hwnd: int | None
    menu_owner_hwnd: int | None
    move_size_hwnd: int | None
    caret_hwnd: int | None
    flags: int


def describe_window(hwnd: int) -> TargetWindow:
    if not user32.IsWindow(hwnd):
        raise RuntimeError(f"Invalid window handle: {hwnd}")
    title = _window_text(hwnd)
    class_name = _window_class_name(hwnd)
    pid = _window_pid(hwnd)
    process_name = _process_name_for_pid(pid)
    window_rect = _window_rect(hwnd)
    client_rect = _client_rect(hwnd)
    dpi = _window_dpi(hwnd)
    return TargetWindow(
        hwnd=hwnd,
        title=title,
        class_name=class_name,
        pid=pid,
        process_name=process_name,
        window_rect=window_rect,
        client_rect=client_rect,
        dpi=dpi,
        valid=True,
    )


def enumerate_child_windows(hwnd: int, *, max_depth: int = 4) -> list[ChildWindowEntry]:
    entries: list[ChildWindowEntry] = []

    def visit(parent_hwnd: int, depth: int) -> None:
        if depth > max_depth:
            return

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(child_hwnd: int, _: int) -> bool:
            try:
                child = describe_window(child_hwnd)
            except RuntimeError:
                return True
            entries.append(
                ChildWindowEntry(
                    hwnd=child.hwnd,
                    title=child.title,
                    class_name=child.class_name,
                    visible=bool(user32.IsWindowVisible(child_hwnd)),
                    enabled=bool(user32.IsWindowEnabled(child_hwnd)),
                    client_rect=child.client_rect,
                    depth=depth,
                )
            )
            visit(child_hwnd, depth + 1)
            return True

        user32.EnumChildWindows(parent_hwnd, enum_proc, 0)

    visit(hwnd, 1)
    return entries


def gui_thread_state(hwnd: int) -> GuiThreadState:
    thread_id = user32.GetWindowThreadProcessId(hwnd, None)
    if not thread_id:
        raise RuntimeError(f"GetWindowThreadProcessId failed for hwnd {hwnd}.")
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        raise RuntimeError(f"GetGUIThreadInfo failed for hwnd {hwnd}.")
    def hwnd_value(value: object) -> int | None:
        raw = getattr(value, "value", value)
        if raw in (None, 0):
            return None
        return int(raw)

    return GuiThreadState(
        active_hwnd=hwnd_value(info.hwndActive),
        focus_hwnd=hwnd_value(info.hwndFocus),
        capture_hwnd=hwnd_value(info.hwndCapture),
        menu_owner_hwnd=hwnd_value(info.hwndMenuOwner),
        move_size_hwnd=hwnd_value(info.hwndMoveSize),
        caret_hwnd=hwnd_value(info.hwndCaret),
        flags=int(info.flags),
    )


def resolve_message_target(hwnd: int, client_point: tuple[int, int]) -> tuple[int, tuple[int, int], list[int]]:
    root_client = _client_rect(hwnd)
    screen_point = POINT(root_client.left + client_point[0], root_client.top + client_point[1])
    current_hwnd = hwnd
    path = [hwnd]

    child_window_from_point = getattr(user32, "ChildWindowFromPointEx", None)
    while child_window_from_point is not None:
        child_point = POINT(screen_point.x, screen_point.y)
        if not user32.ScreenToClient(current_hwnd, ctypes.byref(child_point)):
            break
        next_hwnd = child_window_from_point(
            current_hwnd,
            child_point,
            CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT,
        )
        next_hwnd = int(next_hwnd)
        if not next_hwnd or next_hwnd == current_hwnd:
            return current_hwnd, (child_point.x, child_point.y), path
        current_hwnd = next_hwnd
        path.append(current_hwnd)

    fallback_point = POINT(screen_point.x, screen_point.y)
    user32.ScreenToClient(current_hwnd, ctypes.byref(fallback_point))
    return current_hwnd, (fallback_point.x, fallback_point.y), path


def foreground_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    return _window_text(hwnd)


def cursor_position() -> tuple[int, int]:
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise RuntimeError("GetCursorPos failed.")
    return (point.x, point.y)


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> Rect:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed.")
    return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _client_rect(hwnd: int) -> Rect:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetClientRect failed.")
    origin = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise RuntimeError("ClientToScreen failed.")
    return Rect(origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)


def _window_dpi(hwnd: int) -> int:
    try:
        get_dpi = user32.GetDpiForWindow
    except AttributeError:
        return 96
    dpi = int(get_dpi(hwnd))
    return dpi if dpi > 0 else 96


def _process_name_for_pid(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"pid:{pid}"
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return f"pid:{pid}"
    finally:
        kernel32.CloseHandle(handle)
