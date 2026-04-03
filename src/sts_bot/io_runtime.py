from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from sts_bot.capture_backends import (
    CaptureBackend,
    DxgiDuplicationCaptureBackend,
    LegacyForegroundCaptureBackend,
    UnsupportedCaptureBackend,
    WgcCaptureBackend,
    VisibleRegionCaptureBackend,
    Win32WindowCaptureBackend,
    is_effectively_blank,
)
from sts_bot.config import CalibrationProfile
from sts_bot.input_backends import (
    InputBackend,
    LegacyForegroundInputBackend,
    UnsupportedInputBackend,
    WindowMessageInputBackend,
)
from sts_bot.windowing import CoordinateTransform, TargetWindow, WindowLocator, WindowSelector, cursor_position, foreground_window_title


@dataclass(slots=True)
class CapabilityReport:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str
    client_size: tuple[int, int]
    dpi: int
    scale: float
    selected_capture_backend: str
    selected_input_backend: str
    background_capture_supported: bool
    background_input_supported: bool
    foreground_only_fallback_available: bool
    dry_run: bool
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IoRuntime:
    profile: CalibrationProfile
    target: TargetWindow
    transform: CoordinateTransform
    capture_backend: CaptureBackend
    input_backend: InputBackend

    def open(self) -> None:
        self.target.refresh()
        self.capture_backend.open(self.target)
        self.input_backend.open(self.target)

    def close(self) -> None:
        self.capture_backend.close()
        self.input_backend.close()

    def capability_report(self) -> CapabilityReport:
        self.target.refresh()
        capture_info = self.capture_backend.diagnostics()
        input_info = self.input_backend.diagnostics()
        return CapabilityReport(
            hwnd=self.target.hwnd,
            title=self.target.title,
            class_name=self.target.class_name,
            pid=self.target.pid,
            process_name=self.target.process_name,
            client_size=self.target.client_size,
            dpi=self.target.dpi,
            scale=self.target.scale,
            selected_capture_backend=capture_info.backend,
            selected_input_backend=input_info.backend,
            background_capture_supported=capture_info.background_capable,
            background_input_supported=input_info.background_capable,
            foreground_only_fallback_available=_legacy_fallback_available(),
            dry_run=input_info.dry_run,
            extra={
                "capture_detail": capture_info.detail,
                "input_detail": input_info.detail,
                "candidate_count": self.target.metadata.get("candidate_count", 1),
                "candidate_titles": self.target.metadata.get("candidate_titles", [self.target.title]),
            },
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "foreground_title": foreground_window_title(),
            "cursor_position": cursor_position(),
            "capture": self.capture_backend.diagnostics().__dict__,
            "input": self.input_backend.diagnostics().__dict__,
        }


def create_runtime(profile: CalibrationProfile) -> IoRuntime:
    selector = WindowSelector(
        process_name=_env_or_profile("STS_BOT_TARGET_PROCESS", profile.target_process_name),
        title_regex=_env_or_profile("STS_BOT_TARGET_TITLE_REGEX", profile.target_title_regex) or profile.window_title,
        class_name=_env_or_profile("STS_BOT_TARGET_CLASS", profile.target_class_name),
    )
    locator = WindowLocator(selector)
    target = locator.locate()
    transform = CoordinateTransform(profile.reference_width, profile.reference_height, target)
    capture_backend = _select_capture_backend(profile)
    input_backend = _select_input_backend(profile)
    runtime = IoRuntime(profile=profile, target=target, transform=transform, capture_backend=capture_backend, input_backend=input_backend)
    runtime.open()
    return runtime


def _select_capture_backend(profile: CalibrationProfile) -> CaptureBackend:
    requested = (_env_or_profile("STS_BOT_CAPTURE_BACKEND", profile.capture_backend_name) or "auto").lower()
    allow_legacy = profile.allow_foreground_fallback or _truthy_env("STS_BOT_ALLOW_FOREGROUND_FALLBACK")

    if requested == "wgc":
        return WgcCaptureBackend()
    if requested == "dxgi":
        return DxgiDuplicationCaptureBackend()
    if requested == "legacy":
        if not allow_legacy:
            return UnsupportedCaptureBackend("legacy", "foreground_only fallback is disabled")
        return LegacyForegroundCaptureBackend()
    if requested == "win32":
        return Win32WindowCaptureBackend()
    if requested == "visible_region":
        return VisibleRegionCaptureBackend()
    if requested != "auto":
        return UnsupportedCaptureBackend(requested, "unknown capture backend")

    attempts: list[CaptureBackend] = [
        WgcCaptureBackend(),
        DxgiDuplicationCaptureBackend(),
        Win32WindowCaptureBackend(),
        VisibleRegionCaptureBackend(),
    ]
    try:
        selector = WindowSelector(
            process_name=_env_or_profile("STS_BOT_TARGET_PROCESS", profile.target_process_name),
            title_regex=_env_or_profile("STS_BOT_TARGET_TITLE_REGEX", profile.target_title_regex) or profile.window_title,
            class_name=_env_or_profile("STS_BOT_TARGET_CLASS", profile.target_class_name),
        )
        target = WindowLocator(selector).locate()
    except Exception:
        target = None
    if target is not None:
        for backend in attempts:
            try:
                backend.open(target)
                frame = backend.read_latest_frame(timeout_ms=200)
                if not is_effectively_blank(frame):
                    return backend
            except Exception:
                continue
            finally:
                if backend.target is not None:
                    backend.close()
    for backend in attempts:
        if backend.name in {"win32_window", "visible_region"}:
            return backend
    if allow_legacy:
        return LegacyForegroundCaptureBackend()
    return UnsupportedCaptureBackend("auto", "no background-capable capture backend is available; legacy fallback is disabled")


def _select_input_backend(profile: CalibrationProfile) -> InputBackend:
    requested = (_env_or_profile("STS_BOT_INPUT_BACKEND", profile.input_backend_name) or "auto").lower()
    dry_run = profile.dry_run or _truthy_env("STS_BOT_DRY_RUN")
    allow_legacy = profile.allow_foreground_fallback or _truthy_env("STS_BOT_ALLOW_FOREGROUND_FALLBACK")

    if requested == "window_messages":
        return WindowMessageInputBackend(
            dry_run=dry_run,
            delivery=profile.window_message_delivery,
            activation=profile.window_message_activation,
        )
    if requested == "legacy":
        if not allow_legacy:
            return UnsupportedInputBackend("legacy", "foreground_only fallback is disabled")
        return LegacyForegroundInputBackend(backend=profile.legacy_input_backend, dry_run=dry_run)
    if requested != "auto":
        return UnsupportedInputBackend(requested, "unknown input backend")

    return WindowMessageInputBackend(
        dry_run=dry_run,
        delivery=profile.window_message_delivery,
        activation=profile.window_message_activation,
    )


def _legacy_fallback_available() -> bool:
    try:
        import pyautogui  # noqa: F401
        import pydirectinput  # noqa: F401
    except Exception:
        return False
    return True


def _env_or_profile(name: str, value: str | None) -> str | None:
    env_value = os.getenv(name)
    if env_value:
        return env_value
    return value


def _truthy_env(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and value.lower() in {"1", "true", "yes", "on"}
