from __future__ import annotations

import argparse
import ctypes
import json
from ctypes import wintypes
from pathlib import Path


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_CHAR = 0x0102
WM_TIMER = 0x0113
CW_USEDEFAULT = -2147483648
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
SW_SHOWNOACTIVATE = 4


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Background input smoke-test target window")
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--events-file", type=Path, required=True)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    args = parser.parse_args()

    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.events_file.parent.mkdir(parents=True, exist_ok=True)
    args.events_file.write_text("", encoding="utf-8")

    def record(event_type: str, **payload: object) -> None:
        row = {"event": event_type, **payload}
        with args.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")

    @ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    def wnd_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_LBUTTONDOWN:
            record("click", x=(lparam & 0xFFFF), y=((lparam >> 16) & 0xFFFF), button=1)
            return 0
        if message == WM_RBUTTONDOWN:
            record("click", x=(lparam & 0xFFFF), y=((lparam >> 16) & 0xFFFF), button=3)
            return 0
        if message == WM_CHAR:
            record("key", char=chr(wparam))
            return 0
        if message == WM_TIMER:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(
            wintypes.HWND(hwnd),
            wintypes.UINT(message),
            wintypes.WPARAM(wparam),
            wintypes.LPARAM(lparam),
        )

    h_instance = kernel32.GetModuleHandleW(None)
    class_name = "StsBotInputTestWindow"
    wnd_class = WNDCLASS()
    wnd_class.lpfnWndProc = wnd_proc
    wnd_class.hInstance = h_instance
    wnd_class.lpszClassName = class_name
    atom = user32.RegisterClassW(ctypes.byref(wnd_class))
    if atom == 0:
        raise RuntimeError("RegisterClassW failed for input smoke test window.")

    title = "STS Bot Input Test Window"
    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        title,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        420,
        240,
        0,
        0,
        h_instance,
        None,
    )
    if hwnd == 0:
        raise RuntimeError("CreateWindowExW failed for input smoke test window.")

    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.SetTimer(hwnd, 1, args.timeout_ms, None)
    args.ready_file.write_text(json.dumps({"title": title, "hwnd": int(hwnd)}, ensure_ascii=True), encoding="utf-8")

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
