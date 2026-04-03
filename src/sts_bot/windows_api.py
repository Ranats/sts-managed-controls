from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes

from sts_bot.config import Rect

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9
SW_SHOW = 5
SW_SHOWMAXIMIZED = 3
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    client_rect: Rect


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]

def find_window(title_substring: str) -> WindowInfo:
    matches: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if title_substring.lower() in title.lower():
            matches.append((hwnd, title))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not matches:
        raise RuntimeError(f"Could not find a visible window containing title: {title_substring!r}")

    exact_match = next(((hwnd, title) for hwnd, title in matches if title == title_substring), None)
    hwnd, title = exact_match or matches[0]
    client_rect = client_rect_for_hwnd(hwnd)
    return WindowInfo(hwnd=hwnd, title=title, client_rect=client_rect)


def find_window_rect(title_substring: str) -> Rect:
    return find_window(title_substring).client_rect


def client_rect_for_hwnd(hwnd: int) -> Rect:
    client_rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        raise RuntimeError("GetClientRect failed.")

    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise RuntimeError("ClientToScreen failed.")

    width = client_rect.right - client_rect.left
    height = client_rect.bottom - client_rect.top
    return Rect(origin.x, origin.y, width, height)


def focus_window(title_substring: str | None = None, hwnd: int | None = None) -> None:
    target_title = title_substring
    if hwnd is not None and target_title is None:
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            target_title = buffer.value
        except Exception:
            target_title = None
    if hwnd is None and title_substring is not None:
        hwnd = find_window(title_substring).hwnd
    if target_title:
        try:
            import pygetwindow as gw

            windows = gw.getWindowsWithTitle(target_title)
            if windows:
                windows[0].activate()
        except Exception:
            pass
    if hwnd:
        _focus_hwnd(hwnd)


def capture_window_client(hwnd: int):
    from PIL import Image

    rect = client_rect_for_hwnd(hwnd)
    width = rect.width
    height = rect.height
    hwnd_dc = user32.GetDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_obj = gdi32.SelectObject(mem_dc, bitmap)

    PW_CLIENTONLY = 0x00000001
    result = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY)
    if result != 1:
        gdi32.SelectObject(mem_dc, old_obj)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        raise RuntimeError("PrintWindow failed.")

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buffer_len = width * height * 4
    buffer = ctypes.create_string_buffer(buffer_len)
    lines = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)

    gdi32.SelectObject(mem_dc, old_obj)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    if lines == 0:
        raise RuntimeError("GetDIBits failed.")
    return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)


def capture_screen_client_region(hwnd: int):
    from PIL import Image

    rect = client_rect_for_hwnd(hwnd)
    width = rect.width
    height = rect.height
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old_obj = gdi32.SelectObject(mem_dc, bitmap)

    SRCCOPY = 0x00CC0020
    success = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, SRCCOPY)
    if success == 0:
        gdi32.SelectObject(mem_dc, old_obj)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
        raise RuntimeError("BitBlt failed for client region capture.")

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buffer_len = width * height * 4
    buffer = ctypes.create_string_buffer(buffer_len)
    lines = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)

    gdi32.SelectObject(mem_dc, old_obj)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)

    if lines == 0:
        raise RuntimeError("GetDIBits failed for client region capture.")
    return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)


def _focus_hwnd(hwnd: int) -> None:
    foreground_hwnd = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
    attached_threads: set[int] = set()
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        elif user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        for thread_id in (foreground_thread, target_thread):
            if thread_id and thread_id != current_thread and thread_id not in attached_threads:
                user32.AttachThreadInput(current_thread, thread_id, True)
                attached_threads.add(thread_id)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    finally:
        for thread_id in attached_threads:
            user32.AttachThreadInput(current_thread, thread_id, False)
