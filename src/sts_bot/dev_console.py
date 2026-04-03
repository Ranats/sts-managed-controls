from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from sts_bot.config import CalibrationProfile
from sts_bot.input import perform_key
from sts_bot.io_runtime import create_runtime
from sts_bot.windows_api import focus_window


@dataclass(frozen=True)
class DevConsoleSettingsResult:
    searched_root: str
    updated_paths: list[str] = field(default_factory=list)
    unchanged_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "searched_root": self.searched_root,
            "updated_paths": list(self.updated_paths),
            "unchanged_paths": list(self.unchanged_paths),
        }


@dataclass(frozen=True)
class DevConsoleCommandResult:
    command: str
    pid: int
    hwnd: int
    backend: str
    open_key: str
    close_console: bool
    typing_interval: float
    settings: DevConsoleSettingsResult

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "backend": self.backend,
            "open_key": self.open_key,
            "close_console": self.close_console,
            "typing_interval": self.typing_interval,
            "settings": self.settings.to_dict(),
        }


def find_settings_save_paths(*, settings_root: Path | None = None) -> list[Path]:
    root = settings_root.resolve() if settings_root is not None else _default_settings_root()
    if root.is_file():
        return [root] if root.name.lower() == "settings.save" else []
    if not root.exists():
        return []
    return sorted(path.resolve() for path in root.rglob("settings.save"))


def enable_full_console(*, settings_root: Path | None = None) -> DevConsoleSettingsResult:
    root = settings_root.resolve() if settings_root is not None else _default_settings_root()
    updated_paths: list[str] = []
    unchanged_paths: list[str] = []

    for path in find_settings_save_paths(settings_root=root):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if bool(payload.get("full_console", False)):
            unchanged_paths.append(str(path))
            continue
        payload["full_console"] = True
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        updated_paths.append(str(path))

    return DevConsoleSettingsResult(
        searched_root=str(root),
        updated_paths=updated_paths,
        unchanged_paths=unchanged_paths,
    )


def run_dev_console_command(
    profile: CalibrationProfile,
    command: str,
    *,
    backend: str = "sendinput_scan",
    open_key: str = "backtick",
    typing_interval: float = 0.01,
    settle_seconds: float = 0.12,
    close_console: bool = True,
    ensure_full_console_enabled: bool = True,
    settings_root: Path | None = None,
) -> DevConsoleCommandResult:
    settings_result = (
        enable_full_console(settings_root=settings_root)
        if ensure_full_console_enabled
        else DevConsoleSettingsResult(
            searched_root=str(settings_root.resolve()) if settings_root is not None else str(_default_settings_root()),
        )
    )

    runtime = create_runtime(profile)
    try:
        focus_window(hwnd=runtime.target.hwnd)
        time.sleep(max(0.0, settle_seconds))
        perform_key(backend=backend, key=open_key, hwnd=runtime.target.hwnd, hold_ms=40)
        time.sleep(max(0.0, settle_seconds))
        _type_text(command, interval=max(0.0, typing_interval))
        time.sleep(max(0.0, settle_seconds))
        perform_key(backend=backend, key="enter", hwnd=runtime.target.hwnd, hold_ms=40)
        if close_console:
            time.sleep(max(0.0, settle_seconds))
            perform_key(backend=backend, key=open_key, hwnd=runtime.target.hwnd, hold_ms=40)
        return DevConsoleCommandResult(
            command=command,
            pid=runtime.target.pid,
            hwnd=runtime.target.hwnd,
            backend=backend,
            open_key=open_key,
            close_console=close_console,
            typing_interval=typing_interval,
            settings=settings_result,
        )
    finally:
        runtime.close()


def _type_text(text: str, *, interval: float) -> None:
    import pyautogui

    pyautogui.write(text, interval=interval)


def _default_settings_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
    return Path(appdata) / "SlayTheSpire2"
