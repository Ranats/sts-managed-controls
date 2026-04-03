from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from sts_bot.config import Rect

user32 = ctypes.windll.user32


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MK_LBUTTON = 0x0001

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MAPVK_VK_TO_VSC = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1


VK_BY_NAME = {
    "enter": 0x0D,
    "escape": 0x1B,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "backtick": 0xC0,
    "grave": 0xC0,
    "oem_3": 0xC0,
    "`": 0xC0,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


def click_window_point(window_rect: Rect, point: tuple[int, int], delay_ms: int = 0) -> None:
    import pyautogui

    target_x = window_rect.left + point[0]
    target_y = window_rect.top + point[1]
    pyautogui.moveTo(target_x, target_y, duration=0.05)
    pyautogui.click()
    _sleep(delay_ms)


def press_key(key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    import pyautogui

    pyautogui.keyDown(key)
    _sleep(hold_ms)
    pyautogui.keyUp(key)
    _sleep(delay_ms)


def click_hwnd_point(hwnd: int, point: tuple[int, int], delay_ms: int = 0) -> None:
    x, y = point
    lparam = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    _sleep(delay_ms)


def press_key_hwnd(hwnd: int, key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    vk = VK_BY_NAME.get(key.lower())
    if vk is None:
        raise ValueError(f"Unsupported key for hwnd injection: {key}")
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    _sleep(hold_ms)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
    _sleep(delay_ms)


def click_directinput(window_rect: Rect, point: tuple[int, int], delay_ms: int = 0) -> None:
    import pydirectinput

    target_x = window_rect.left + point[0]
    target_y = window_rect.top + point[1]
    pydirectinput.moveTo(target_x, target_y)
    pydirectinput.click()
    _sleep(delay_ms)


def press_key_directinput(key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    import pydirectinput

    pydirectinput.keyDown(key)
    _sleep(hold_ms)
    pydirectinput.keyUp(key)
    _sleep(delay_ms)


def drag_window_path(
    window_rect: Rect,
    start: tuple[int, int],
    end: tuple[int, int],
    delay_ms: int = 0,
    duration_ms: int = 220,
) -> None:
    import pyautogui

    start_x = window_rect.left + start[0]
    start_y = window_rect.top + start[1]
    end_x = window_rect.left + end[0]
    end_y = window_rect.top + end[1]
    pyautogui.moveTo(start_x, start_y, duration=0.05)
    pyautogui.dragTo(end_x, end_y, duration=max(0.05, duration_ms / 1000), button="left")
    _sleep(delay_ms)


def drag_directinput(
    window_rect: Rect,
    start: tuple[int, int],
    end: tuple[int, int],
    delay_ms: int = 0,
    duration_ms: int = 220,
) -> None:
    import pydirectinput

    start_x = window_rect.left + start[0]
    start_y = window_rect.top + start[1]
    end_x = window_rect.left + end[0]
    end_y = window_rect.top + end[1]
    pydirectinput.moveTo(start_x, start_y)
    pydirectinput.mouseDown()
    pydirectinput.moveTo(end_x, end_y, duration=max(0.05, duration_ms / 1000))
    pydirectinput.mouseUp()
    _sleep(delay_ms)


def click_sendinput(window_rect: Rect, point: tuple[int, int], delay_ms: int = 0) -> None:
    target_x = window_rect.left + point[0]
    target_y = window_rect.top + point[1]
    _send_mouse_absolute(target_x, target_y)
    _send_mouse_button(MOUSEEVENTF_LEFTDOWN)
    _sleep(40)
    _send_mouse_button(MOUSEEVENTF_LEFTUP)
    _sleep(delay_ms)


def press_key_sendinput(key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    vk = _resolve_vk(key)
    _send_keyboard(vk=vk, scan_code=0, flags=0)
    _sleep(hold_ms)
    _send_keyboard(vk=vk, scan_code=0, flags=KEYEVENTF_KEYUP)
    _sleep(delay_ms)


def press_key_sendinput_scan(key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    vk = _resolve_vk(key)
    scan_code = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_SCANCODE | _extended_key_flag(vk)
    _send_keyboard(vk=0, scan_code=scan_code, flags=flags)
    _sleep(hold_ms)
    _send_keyboard(vk=0, scan_code=scan_code, flags=flags | KEYEVENTF_KEYUP)
    _sleep(delay_ms)


def click_legacy_event(window_rect: Rect, point: tuple[int, int], delay_ms: int = 0) -> None:
    target_x = window_rect.left + point[0]
    target_y = window_rect.top + point[1]
    user32.SetCursorPos(target_x, target_y)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    _sleep(40)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    _sleep(delay_ms)


def press_key_legacy_event(key: str, delay_ms: int = 0, hold_ms: int = 40) -> None:
    vk = _resolve_vk(key)
    scan_code = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    user32.keybd_event(vk, scan_code, 0, 0)
    _sleep(hold_ms)
    user32.keybd_event(vk, scan_code, KEYEVENTF_KEYUP, 0)
    _sleep(delay_ms)


def perform_click(
    *,
    backend: str,
    window_rect: Rect,
    point: tuple[int, int],
    hwnd: int | None,
    delay_ms: int = 0,
) -> str:
    for candidate in _backend_order(backend):
        try:
            if candidate == "hwnd" and hwnd is not None:
                click_hwnd_point(hwnd, point, delay_ms=delay_ms)
                return candidate
            if candidate == "sendinput":
                click_sendinput(window_rect, point, delay_ms=delay_ms)
                return candidate
            if candidate == "legacy_event":
                click_legacy_event(window_rect, point, delay_ms=delay_ms)
                return candidate
            if candidate == "directinput":
                click_directinput(window_rect, point, delay_ms=delay_ms)
                return candidate
            if candidate == "pyautogui":
                click_window_point(window_rect, point, delay_ms=delay_ms)
                return candidate
        except Exception:
            continue
    raise RuntimeError(f"No usable click backend for: {backend}")


def perform_key(
    *,
    backend: str,
    key: str,
    hwnd: int | None,
    delay_ms: int = 0,
    hold_ms: int = 40,
) -> str:
    for candidate in _backend_order(backend):
        try:
            if candidate == "hwnd" and hwnd is not None:
                press_key_hwnd(hwnd, key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
            if candidate == "sendinput_scan":
                press_key_sendinput_scan(key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
            if candidate == "sendinput":
                press_key_sendinput(key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
            if candidate == "legacy_event":
                press_key_legacy_event(key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
            if candidate == "directinput":
                press_key_directinput(key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
            if candidate == "pyautogui":
                press_key(key, delay_ms=delay_ms, hold_ms=hold_ms)
                return candidate
        except Exception:
            continue
    raise RuntimeError(f"No usable key backend for: {backend}")


def perform_drag(
    *,
    backend: str,
    window_rect: Rect,
    start: tuple[int, int],
    end: tuple[int, int],
    hwnd: int | None,
    delay_ms: int = 0,
    duration_ms: int = 220,
) -> str:
    del hwnd
    for candidate in _backend_order(backend):
        try:
            if candidate == "directinput":
                drag_directinput(window_rect, start, end, delay_ms=delay_ms, duration_ms=duration_ms)
                return candidate
            if candidate == "pyautogui":
                drag_window_path(window_rect, start, end, delay_ms=delay_ms, duration_ms=duration_ms)
                return candidate
        except Exception:
            continue
    raise RuntimeError(f"No usable drag backend for: {backend}")


def _backend_order(backend: str) -> list[str]:
    mapping = {
        "combined": ["sendinput_scan", "sendinput", "legacy_event", "directinput", "pyautogui", "hwnd"],
        "hwnd": ["hwnd"],
        "sendinput": ["sendinput"],
        "sendinput_scan": ["sendinput_scan"],
        "legacy_event": ["legacy_event"],
        "directinput": ["directinput"],
        "pyautogui": ["pyautogui"],
        "directinput_first": ["directinput", "legacy_event", "sendinput_scan", "sendinput", "pyautogui", "hwnd"],
        "sendinput_first": ["sendinput_scan", "sendinput", "legacy_event", "directinput", "pyautogui", "hwnd"],
    }
    return mapping.get(backend, [backend])


def backend_candidates(backend: str, *, include_key_only: bool = False) -> list[str]:
    if backend == "all":
        candidates = ["sendinput_scan", "sendinput", "legacy_event", "directinput", "pyautogui", "hwnd"]
    else:
        candidates = _backend_order(backend)
    if not include_key_only:
        return [candidate for candidate in candidates if candidate != "sendinput_scan"]
    return candidates


def _send_mouse_absolute(target_x: int, target_y: int) -> None:
    screen_width = max(1, user32.GetSystemMetrics(SM_CXSCREEN) - 1)
    screen_height = max(1, user32.GetSystemMetrics(SM_CYSCREEN) - 1)
    normalized_x = round(target_x * 65535 / screen_width)
    normalized_y = round(target_y * 65535 / screen_height)
    payload = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(
                dx=normalized_x,
                dy=normalized_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_inputs([payload])


def _send_mouse_button(flags: int) -> None:
    payload = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_inputs([payload])


def _send_keyboard(*, vk: int, scan_code: int, flags: int) -> None:
    payload = INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=vk,
                wScan=scan_code,
                dwFlags=flags,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    _send_inputs([payload])


def _send_inputs(payloads: list[INPUT]) -> None:
    array_type = INPUT * len(payloads)
    result = user32.SendInput(len(payloads), array_type(*payloads), ctypes.sizeof(INPUT))
    if result != len(payloads):
        raise RuntimeError("SendInput failed.")


def _resolve_vk(key: str) -> int:
    normalized = key.lower()
    vk = VK_BY_NAME.get(normalized)
    if vk is None:
        if normalized.startswith("key") and len(normalized) == 4 and normalized[-1].isdigit():
            normalized = normalized[-1]
        if len(normalized) == 1 and normalized.isalnum():
            return ord(normalized.upper())
        raise ValueError(f"Unsupported key: {key}")
    return vk


def _extended_key_flag(vk: int) -> int:
    if vk in {0x25, 0x26, 0x27, 0x28}:
        return KEYEVENTF_EXTENDEDKEY
    return 0


def _sleep(delay_ms: int) -> None:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000)
