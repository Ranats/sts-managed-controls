from __future__ import annotations

import importlib
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from sts_bot.config import Rect
from sts_bot.vision import capture_rect
from sts_bot.windowing import TargetWindow
from sts_bot.windows_api import capture_screen_client_region, capture_window_client, focus_window


@dataclass(slots=True)
class CaptureDiagnostics:
    backend: str
    background_capable: bool
    foreground_only: bool
    selected: bool = False
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def is_effectively_blank(image: Image.Image) -> bool:
    grayscale = image.convert("L")
    extrema = grayscale.getextrema()
    if extrema is None:
        return True
    low, high = extrema
    return high <= 5 or (high - low) <= 3


class CaptureBackend(ABC):
    name = "capture"
    foreground_only = False

    def __init__(self) -> None:
        self.target: TargetWindow | None = None

    @property
    def is_background_capable(self) -> bool:
        return not self.foreground_only

    def open(self, target_window: TargetWindow) -> None:
        self.target = target_window

    @abstractmethod
    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        raise NotImplementedError

    def close(self) -> None:
        self.target = None

    @abstractmethod
    def diagnostics(self) -> CaptureDiagnostics:
        raise NotImplementedError


class UnsupportedCaptureBackend(CaptureBackend):
    def __init__(self, name: str, reason: str, *, foreground_only: bool = False) -> None:
        super().__init__()
        self.name = name
        self.reason = reason
        self.foreground_only = foreground_only

    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        del timeout_ms
        raise RuntimeError(f"{self.name} capture backend is unsupported: {self.reason}")

    def diagnostics(self) -> CaptureDiagnostics:
        return CaptureDiagnostics(
            backend=self.name,
            background_capable=self.is_background_capable,
            foreground_only=self.foreground_only,
            detail=self.reason,
        )


class WgcCaptureBackend(UnsupportedCaptureBackend):
    def __init__(self, helper_path: Path | None = None) -> None:
        super().__init__("wgc", "Windows.Graphics.Capture backend is unavailable.", foreground_only=False)
        self.helper_path = helper_path
        self._module: Any | None = None
        self._capture: Any | None = None
        self._control: Any | None = None
        self._last_frame: Image.Image | None = None
        self._last_frame_at = 0.0
        self._lock = threading.Lock()
        self._frame_event = threading.Event()
        self._closed_event = threading.Event()

    def open(self, target_window: TargetWindow) -> None:
        super().open(target_window)
        self._start_capture()

    def _start_capture(self) -> None:
        if self.target is None:
            raise RuntimeError("Capture backend is not open.")
        try:
            self._module = importlib.import_module("windows_capture")
        except Exception as exc:
            self.reason = f"windows-capture package is not installed or failed to import: {exc}"
            raise RuntimeError(self.reason) from exc
        self.reason = "Uses Windows.Graphics.Capture via the optional windows-capture package. Non-foreground capture is supported while the target window remains capturable by WGC."
        self.target.refresh()
        window_name = self.target.title
        self._frame_event.clear()
        self._closed_event.clear()
        self._capture = self._module.WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=16,
            window_name=window_name,
        )

        @self._capture.event
        def on_frame_arrived(frame, capture_control) -> None:  # type: ignore[no-untyped-def]
            rgb = frame.frame_buffer[:, :, [2, 1, 0]].copy()
            image = Image.fromarray(rgb, mode="RGB")
            with self._lock:
                self._last_frame = image
                self._last_frame_at = time.time()
            self._frame_event.set()

        @self._capture.event
        def on_closed() -> None:  # type: ignore[no-untyped-def]
            self._closed_event.set()
            self._frame_event.set()

        self._control = self._capture.start_free_threaded()

    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        if self.target is None:
            raise RuntimeError("Capture backend is not open.")
        deadline = time.time() + max(0.05, timeout_ms / 1000)
        while time.time() < deadline:
            with self._lock:
                if self._last_frame is not None:
                    return self._normalize_frame(self._last_frame)
            remaining = max(0.01, deadline - time.time())
            self._frame_event.wait(min(0.05, remaining))
            self._frame_event.clear()
            if self._closed_event.is_set():
                break
        raise RuntimeError("WGC capture timed out without receiving a frame.")

    def _normalize_frame(self, image: Image.Image) -> Image.Image:
        if self.target is None:
            return image.copy()
        refresh = getattr(self.target, "refresh", None)
        if callable(refresh):
            refresh()
        client_size = getattr(self.target, "client_size", None)
        window_rect = getattr(self.target, "window_rect", None)
        client_rect = getattr(self.target, "client_rect", None)
        if client_size is None or window_rect is None or client_rect is None:
            return image.copy()
        client_width, client_height = client_size
        if image.size == (client_width, client_height):
            return image.copy()
        window_width = max(1, window_rect.width)
        window_height = max(1, window_rect.height)
        client_left = max(0, client_rect.left - window_rect.left)
        client_top = max(0, client_rect.top - window_rect.top)
        crop_left = round(client_left * image.width / window_width)
        crop_top = round(client_top * image.height / window_height)
        crop_width = round(client_width * image.width / window_width)
        crop_height = round(client_height * image.height / window_height)
        crop_right = min(image.width, crop_left + max(1, crop_width))
        crop_bottom = min(image.height, crop_top + max(1, crop_height))
        crop_left = max(0, min(crop_left, image.width - 1))
        crop_top = max(0, min(crop_top, image.height - 1))
        if crop_right <= crop_left or crop_bottom <= crop_top:
            return image.copy()
        cropped = image.crop((crop_left, crop_top, crop_right, crop_bottom))
        if cropped.size != (client_width, client_height):
            return cropped.resize((client_width, client_height))
        return cropped

    def close(self) -> None:
        if self._control is not None:
            try:
                self._control.stop()
                self._control.wait()
            except Exception:
                pass
        self._control = None
        self._capture = None
        self._module = None
        with self._lock:
            self._last_frame = None
            self._last_frame_at = 0.0
        self._frame_event.clear()
        self._closed_event.clear()
        super().close()

    def diagnostics(self) -> CaptureDiagnostics:
        installed = self._module is not None
        return CaptureDiagnostics(
            backend=self.name,
            background_capable=True,
            foreground_only=False,
            detail=self.reason,
            extra={
                "last_capture_at": self._last_frame_at,
                "package_loaded": installed,
            },
        )


class DxgiDuplicationCaptureBackend(UnsupportedCaptureBackend):
    def __init__(self, helper_path: Path | None = None) -> None:
        reason = "DXGI Desktop Duplication helper is not bundled in this repo build."
        if helper_path is not None:
            reason = f"DXGI helper path configured but unsupported here: {helper_path}"
        super().__init__("dxgi", reason)


class Win32WindowCaptureBackend(CaptureBackend):
    name = "win32_window"

    def __init__(self) -> None:
        super().__init__()
        self._last_capture_at = 0.0

    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        if self.target is None:
            raise RuntimeError("Capture backend is not open.")
        self.target.refresh()
        deadline = time.time() + max(0.05, timeout_ms / 1000)
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                image = capture_window_client(self.target.hwnd)
                self._last_capture_at = time.time()
                return image.convert("RGB")
            except Exception as exc:
                last_error = exc
                time.sleep(0.02)
        raise RuntimeError(f"Win32 window capture failed: {last_error}")

    def diagnostics(self) -> CaptureDiagnostics:
        return CaptureDiagnostics(
            backend=self.name,
            background_capable=True,
            foreground_only=False,
            detail="Uses Win32 PrintWindow client capture. Background-capable for visible windows; minimized/invisible behavior is not guaranteed.",
            extra={"last_capture_at": self._last_capture_at},
        )


class VisibleRegionCaptureBackend(CaptureBackend):
    name = "visible_region"
    foreground_only = True

    def __init__(self, *, focus_target: bool = False) -> None:
        super().__init__()
        self._last_capture_at = 0.0
        self._last_focus_at = 0.0
        self._focus_target = focus_target

    def open(self, target_window: TargetWindow) -> None:
        super().open(target_window)
        self._ensure_target_visible(force=True)

    def _ensure_target_visible(self, *, force: bool = False) -> None:
        if self.target is None or not self._focus_target:
            return
        if not force and (time.time() - self._last_focus_at) < 0.5:
            return
        focus_window(hwnd=self.target.hwnd)
        self._last_focus_at = time.time()

    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        if self.target is None:
            raise RuntimeError("Capture backend is not open.")
        self._ensure_target_visible()
        self.target.refresh()
        deadline = time.time() + max(0.05, timeout_ms / 1000)
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                image = capture_screen_client_region(self.target.hwnd)
                self._last_capture_at = time.time()
                return image.convert("RGB")
            except Exception as exc:
                last_error = exc
                time.sleep(0.02)
        raise RuntimeError(f"Visible region capture failed: {last_error}")

    def diagnostics(self) -> CaptureDiagnostics:
        return CaptureDiagnostics(
            backend=self.name,
            background_capable=False,
            foreground_only=True,
            detail="Captures the target client rectangle from the visible screen via BitBlt without auto-focusing by default. It is not reliable when the target is covered or otherwise not actually visible on the desktop.",
            extra={"last_capture_at": self._last_capture_at, "focus_target": self._focus_target},
        )


class LegacyForegroundCaptureBackend(CaptureBackend):
    name = "legacy"
    foreground_only = True

    def open(self, target_window: TargetWindow) -> None:
        super().open(target_window)
        focus_window(hwnd=target_window.hwnd)

    def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
        del timeout_ms
        if self.target is None:
            raise RuntimeError("Capture backend is not open.")
        focus_window(hwnd=self.target.hwnd)
        self.target.refresh()
        return capture_rect(Rect(
            self.target.client_rect.left,
            self.target.client_rect.top,
            self.target.client_rect.width,
            self.target.client_rect.height,
        )).convert("RGB")

    def diagnostics(self) -> CaptureDiagnostics:
        return CaptureDiagnostics(
            backend=self.name,
            background_capable=False,
            foreground_only=True,
            detail="Legacy foreground capture using ImageGrab over the desktop. Normal operation should not use this backend.",
        )
