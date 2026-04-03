from __future__ import annotations

import sys
import time
from pathlib import Path


DEFAULT_VENDOR_ROOT = Path(".tmp_vgamepad") / "vgamepad-0.1.0"
_VG_MODULE = None
_VG_GAMEPAD = None


BUTTON_ALIASES = {
    "a": "XUSB_GAMEPAD_A",
    "b": "XUSB_GAMEPAD_B",
    "x": "XUSB_GAMEPAD_X",
    "y": "XUSB_GAMEPAD_Y",
    "start": "XUSB_GAMEPAD_START",
    "back": "XUSB_GAMEPAD_BACK",
    "dpad_up": "XUSB_GAMEPAD_DPAD_UP",
    "dpad_down": "XUSB_GAMEPAD_DPAD_DOWN",
    "dpad_left": "XUSB_GAMEPAD_DPAD_LEFT",
    "dpad_right": "XUSB_GAMEPAD_DPAD_RIGHT",
    "lb": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "rb": "XUSB_GAMEPAD_RIGHT_SHOULDER",
}


def load_vgamepad(vendor_root: Path | None = None):
    global _VG_MODULE
    if _VG_MODULE is not None:
        return _VG_MODULE
    root = (vendor_root or DEFAULT_VENDOR_ROOT).resolve()
    if not root.exists():
        raise RuntimeError(
            f"vgamepad source tree is missing: {root}. "
            "Download/extract vgamepad-0.1.0 under .tmp_vgamepad first."
        )
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    import vgamepad as vg  # type: ignore

    _VG_MODULE = vg
    return vg


def get_xbox_gamepad(vendor_root: Path | None = None):
    global _VG_GAMEPAD
    vg = load_vgamepad(vendor_root)
    if _VG_GAMEPAD is None:
        _VG_GAMEPAD = vg.VX360Gamepad()
    return vg, _VG_GAMEPAD


def press_xbox_sequence(
    button_names: list[str],
    *,
    vendor_root: Path | None = None,
    settle_ms: int = 0,
    hold_ms: int = 500,
    gap_ms: int = 700,
) -> None:
    vg, gamepad = get_xbox_gamepad(vendor_root)
    time.sleep(max(0, settle_ms) / 1000)
    for button_name in button_names:
        normalized = button_name.strip().lower()
        enum_name = BUTTON_ALIASES.get(normalized)
        if enum_name is None:
            raise ValueError(f"Unsupported gamepad button: {button_name}")
        button = getattr(vg.XUSB_BUTTON, enum_name)
        gamepad.press_button(button=button)
        gamepad.update()
        time.sleep(max(0, hold_ms) / 1000)
        gamepad.release_button(button=button)
        gamepad.update()
        time.sleep(max(0, gap_ms) / 1000)
