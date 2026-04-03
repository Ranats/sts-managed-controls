from __future__ import annotations

import ctypes
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sts_bot.config import Rect
from sts_bot.input import perform_click, perform_drag, perform_key
from sts_bot.windowing import TargetWindow, gui_thread_state, resolve_message_target

user32 = ctypes.windll.user32

WM_NULL = 0x0000
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_MOUSEACTIVATE = 0x0021
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_ACTIVATE = 0x0006
WM_ACTIVATEAPP = 0x001C
WM_NCACTIVATE = 0x0086
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001

CHILDID_SELF = 0
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004

MAPVK_VK_TO_VSC = 0
WA_ACTIVE = 1
WA_CLICKACTIVE = 2
MA_ACTIVATE = 1
HTCLIENT = 1

VK_BY_NAME = {
    "enter": 0x0D,
    "escape": 0x1B,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


@dataclass(slots=True)
class InputDiagnostics:
    backend: str
    background_capable: bool
    foreground_only: bool
    dry_run: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class InputBackend(ABC):
    name = "input"
    foreground_only = False

    def __init__(self, *, dry_run: bool = False) -> None:
        self.target: TargetWindow | None = None
        self.dry_run = dry_run

    @property
    def is_background_capable(self) -> bool:
        return not self.foreground_only

    def open(self, target_window: TargetWindow) -> None:
        self.target = target_window

    def close(self) -> None:
        self.target = None

    @abstractmethod
    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        raise NotImplementedError

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def key_down(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def key_up(self, key: str) -> None:
        raise NotImplementedError

    def key_press(self, key: str, *, hold_ms: int = 40) -> None:
        self.key_down(key)
        if not self.dry_run and hold_ms > 0:
            time.sleep(hold_ms / 1000)
        self.key_up(key)

    def text(self, value: str) -> None:
        for char in value:
            self._char(char)

    def scroll(self, x: int, y: int, delta: int) -> None:
        raise RuntimeError(f"{self.name} does not support scroll.")

    @abstractmethod
    def diagnostics(self) -> InputDiagnostics:
        raise NotImplementedError

    def _char(self, char: str) -> None:
        raise RuntimeError(f"{self.name} does not support text input.")


class UnsupportedInputBackend(InputBackend):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(dry_run=False)
        self.name = name
        self.reason = reason

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        del x, y, button, double
        raise RuntimeError(f"{self.name} input backend is unsupported: {self.reason}")

    def move(self, x: int, y: int) -> None:
        del x, y
        raise RuntimeError(f"{self.name} input backend is unsupported: {self.reason}")

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
        del x1, y1, x2, y2, duration_ms
        raise RuntimeError(f"{self.name} input backend is unsupported: {self.reason}")

    def key_down(self, key: str) -> None:
        del key
        raise RuntimeError(f"{self.name} input backend is unsupported: {self.reason}")

    def key_up(self, key: str) -> None:
        del key
        raise RuntimeError(f"{self.name} input backend is unsupported: {self.reason}")

    def diagnostics(self) -> InputDiagnostics:
        return InputDiagnostics(
            backend=self.name,
            background_capable=False,
            foreground_only=False,
            dry_run=False,
            detail=self.reason,
        )


class WindowMessageInputBackend(InputBackend):
    name = "window_messages"

    def __init__(self, *, dry_run: bool = False, delivery: str = "send", activation: str = "none") -> None:
        super().__init__(dry_run=dry_run)
        self._sent_messages = 0
        self.delivery = delivery
        self.activation = activation
        self._last_target_hwnd: int | None = None
        self._last_key_targets: list[int] = []
        self._last_message_path: list[int] = []

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        hwnd, local_point = self._resolve_mouse_target(x, y)
        if self.dry_run:
            self._sent_messages += 3 if double else 2
            return
        self._maybe_activate_for_click(hwnd)
        move_lparam = self._move_to(hwnd, local_point)
        if button == "left":
            down_msg = WM_LBUTTONDOWN
            up_msg = WM_LBUTTONUP
            wparam = MK_LBUTTON
        elif button == "right":
            down_msg = WM_RBUTTONDOWN
            up_msg = WM_RBUTTONUP
            wparam = MK_RBUTTON
        else:
            raise ValueError(f"Unsupported button: {button}")
        self._send(hwnd, down_msg, wparam, move_lparam)
        self._send(hwnd, up_msg, 0, move_lparam)
        if double:
            self._send(hwnd, down_msg, wparam, move_lparam)
            self._send(hwnd, up_msg, 0, move_lparam)
        self._sent_messages += 5 if double else 3

    def move(self, x: int, y: int) -> None:
        hwnd, local_point = self._resolve_mouse_target(x, y)
        if self.dry_run:
            self._sent_messages += 1
            return
        self._maybe_activate_for_click(hwnd)
        self._move_to(hwnd, local_point)
        self._sent_messages += 1

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
        hwnd, local_start = self._resolve_mouse_target(x1, y1)
        _, local_end = self._resolve_mouse_target(x2, y2)
        points = max(2, min(12, duration_ms // 25 if duration_ms > 0 else 4))
        path = [
            (
                round(local_start[0] + (local_end[0] - local_start[0]) * (index / (points - 1))),
                round(local_start[1] + (local_end[1] - local_start[1]) * (index / (points - 1))),
            )
            for index in range(points)
        ]
        if self.dry_run:
            self._sent_messages += len(path) + 2
            return
        self._maybe_activate_for_click(hwnd)
        self._move_to(hwnd, local_start)
        self._send(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _make_lparam(*local_start))
        for point in path[1:]:
            self._send(hwnd, WM_MOUSEMOVE, MK_LBUTTON, _make_lparam(*point))
        self._send(hwnd, WM_LBUTTONUP, 0, _make_lparam(*local_end))
        self._sent_messages += len(path) + 2

    def scroll(self, x: int, y: int, delta: int) -> None:
        hwnd, local_point = self._resolve_mouse_target(x, y)
        if self.dry_run:
            self._sent_messages += 1
            return
        self._maybe_activate_for_click(hwnd)
        self._send(hwnd, WM_MOUSEWHEEL, (delta << 16), _make_lparam(*local_point))
        self._sent_messages += 1

    def key_down(self, key: str) -> None:
        key_targets = self._resolve_key_targets()
        if self.dry_run:
            self._sent_messages += len(key_targets)
            return
        self._maybe_activate_for_key(key_targets)
        vk = _resolve_vk(key)
        scan_code = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        lparam = _key_lparam(scan_code, extended=_is_extended(vk), released=False)
        for hwnd in key_targets:
            self._send(hwnd, WM_KEYDOWN, vk, lparam)
        self._sent_messages += len(key_targets)

    def key_up(self, key: str) -> None:
        key_targets = self._resolve_key_targets()
        if self.dry_run:
            self._sent_messages += len(key_targets)
            return
        self._maybe_activate_for_key(key_targets)
        vk = _resolve_vk(key)
        scan_code = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        lparam = _key_lparam(scan_code, extended=_is_extended(vk), released=True)
        for hwnd in key_targets:
            self._send(hwnd, WM_KEYUP, vk, lparam)
        self._sent_messages += len(key_targets)

    def key_press(self, key: str, *, hold_ms: int = 40) -> None:
        super().key_press(key, hold_ms=hold_ms)
        if len(key) == 1 and key.isprintable():
            self._char(key)

    def diagnostics(self) -> InputDiagnostics:
        return InputDiagnostics(
            backend=self.name,
            background_capable=True,
            foreground_only=False,
            dry_run=self.dry_run,
            detail="Message-based input uses client-coordinate WM_* messages. Delivery is background-capable, but the game may ignore these messages.",
            extra={
                "sent_messages": self._sent_messages,
                "delivery": self.delivery,
                "activation": self.activation,
                "last_target_hwnd": self._last_target_hwnd,
                "last_key_targets": self._last_key_targets,
                "last_message_path": self._last_message_path,
            },
        )

    def _char(self, char: str) -> None:
        key_targets = self._resolve_key_targets()
        if self.dry_run:
            self._sent_messages += len(key_targets)
            return
        self._maybe_activate_for_key(key_targets)
        for hwnd in key_targets:
            self._send(hwnd, WM_CHAR, ord(char), 1)
        self._sent_messages += len(key_targets)

    def _resolve_mouse_target(self, x: int, y: int) -> tuple[int, tuple[int, int]]:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        self.target.refresh()
        hwnd, point, path = resolve_message_target(self.target.hwnd, (x, y))
        self._last_target_hwnd = hwnd
        self._last_message_path = path
        return hwnd, point

    def _resolve_key_targets(self) -> list[int]:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        self.target.refresh()
        targets: list[int] = [self.target.hwnd]
        try:
            state = gui_thread_state(self.target.hwnd)
        except Exception:
            state = None
        if state is not None:
            for hwnd in (state.focus_hwnd, state.active_hwnd):
                if hwnd and hwnd not in targets:
                    targets.insert(0, hwnd)
        self._last_key_targets = targets
        return targets

    def _maybe_activate_for_key(self, targets: list[int]) -> None:
        if self.activation not in {"key", "all"}:
            return
        if self.target is None:
            return
        root_hwnd = self.target.hwnd
        for hwnd in targets:
            self._send(hwnd, WM_NCACTIVATE, 1, 0)
            self._send(hwnd, WM_ACTIVATEAPP, 1, 0)
            self._send(hwnd, WM_ACTIVATE, WA_ACTIVE, root_hwnd)
            self._sent_messages += 3

    def _maybe_activate_for_click(self, hwnd: int) -> None:
        if self.activation not in {"click", "all"}:
            return
        if self.target is None:
            return
        root_hwnd = self.target.hwnd
        mouse_activate_lparam = (WM_LBUTTONDOWN << 16) | HTCLIENT
        self._send(hwnd, WM_MOUSEACTIVATE, root_hwnd, mouse_activate_lparam)
        self._send(hwnd, WM_NCACTIVATE, 1, 0)
        self._send(hwnd, WM_ACTIVATEAPP, 1, 0)
        self._send(hwnd, WM_ACTIVATE, WA_CLICKACTIVE, root_hwnd)
        self._sent_messages += 4

    def _send(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        if self.delivery == "post":
            ok = user32.PostMessageW(hwnd, message, wparam, lparam)
            if ok == 0:
                raise RuntimeError(f"PostMessage failed for message 0x{message:04x} to hwnd {hwnd}.")
            return
        result = ctypes.c_size_t()
        ok = user32.SendMessageTimeoutW(
            hwnd,
            message,
            wparam,
            lparam,
            SMTO_ABORTIFHUNG | SMTO_BLOCK,
            300,
            ctypes.byref(result),
        )
        if ok == 0:
            raise RuntimeError(f"SendMessageTimeout failed for message 0x{message:04x} to hwnd {hwnd}.")

    def _move_to(self, hwnd: int, local_point: tuple[int, int]) -> int:
        move_lparam = _make_lparam(*local_point)
        self._send(hwnd, WM_MOUSEMOVE, 0, move_lparam)
        return move_lparam


class LegacyForegroundInputBackend(InputBackend):
    name = "legacy"
    foreground_only = True

    def __init__(self, *, backend: str = "combined", dry_run: bool = False) -> None:
        super().__init__(dry_run=dry_run)
        self.legacy_backend = backend

    def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        if button != "left":
            raise ValueError("Legacy foreground backend only supports left click in this adapter.")
        if self.dry_run:
            return
        for _ in range(2 if double else 1):
            perform_click(
                backend=self.legacy_backend,
                window_rect=self.target.client_rect,
                point=(x, y),
                hwnd=self.target.hwnd,
                delay_ms=0,
            )

    def move(self, x: int, y: int) -> None:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        if self.dry_run:
            return
        target_x = self.target.client_rect.left + x
        target_y = self.target.client_rect.top + y
        user32.SetCursorPos(target_x, target_y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        if self.dry_run:
            return
        perform_drag(
            backend=self.legacy_backend,
            window_rect=self.target.client_rect,
            start=(x1, y1),
            end=(x2, y2),
            hwnd=self.target.hwnd,
            delay_ms=0,
            duration_ms=duration_ms,
        )

    def key_down(self, key: str) -> None:
        raise RuntimeError("Legacy foreground backend only supports key_press.")

    def key_up(self, key: str) -> None:
        raise RuntimeError("Legacy foreground backend only supports key_press.")

    def key_press(self, key: str, *, hold_ms: int = 40) -> None:
        if self.target is None:
            raise RuntimeError("Input backend is not open.")
        if self.dry_run:
            return
        perform_key(
            backend=self.legacy_backend,
            key=key,
            hwnd=self.target.hwnd,
            delay_ms=0,
            hold_ms=hold_ms,
        )

    def diagnostics(self) -> InputDiagnostics:
        return InputDiagnostics(
            backend=self.name,
            background_capable=False,
            foreground_only=True,
            dry_run=self.dry_run,
            detail=f"Legacy foreground input using backend={self.legacy_backend}. Normal operation should not use this backend.",
        )


def _make_lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


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


def _is_extended(vk: int) -> bool:
    return vk in {0x25, 0x26, 0x27, 0x28}


def _key_lparam(scan_code: int, *, extended: bool, released: bool) -> int:
    repeat_count = 1
    value = repeat_count | (scan_code << 16)
    if extended:
        value |= 1 << 24
    if released:
        value |= 1 << 30
        value |= 1 << 31
    return value
