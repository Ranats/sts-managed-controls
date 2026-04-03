from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from sts_bot.capture_backends import CaptureBackend, CaptureDiagnostics, LegacyForegroundCaptureBackend, UnsupportedCaptureBackend
from sts_bot.config import example_profile
from sts_bot.io_runtime import _select_capture_backend, _select_input_backend
from sts_bot.input_backends import LegacyForegroundInputBackend, UnsupportedInputBackend, WindowMessageInputBackend


class IoRuntimeSelectionTest(unittest.TestCase):
    def test_auto_prefers_background_backends(self) -> None:
        class FakeBackgroundCaptureBackend(CaptureBackend):
            name = "wgc"

            def read_latest_frame(self, timeout_ms: int = 250) -> Image.Image:
                del timeout_ms
                return Image.new("RGB", (8, 8), "white")

            def diagnostics(self) -> CaptureDiagnostics:
                return CaptureDiagnostics(
                    backend=self.name,
                    background_capable=True,
                    foreground_only=False,
                )

        profile = example_profile()
        target = SimpleNamespace(
            hwnd=101,
            title="Slay the Spire 2",
            class_name="Engine",
            pid=1,
            process_name="SlayTheSpire2.exe",
            client_size=(1920, 1009),
            dpi=96,
            scale=1.0,
            refresh=lambda: None,
        )
        fake_capture = FakeBackgroundCaptureBackend()
        with patch("sts_bot.io_runtime.WindowLocator.locate", return_value=target):
            with patch("sts_bot.io_runtime.WgcCaptureBackend", return_value=fake_capture):
                with patch("sts_bot.io_runtime.DxgiDuplicationCaptureBackend", return_value=UnsupportedCaptureBackend("dxgi", "disabled")):
                    with patch("sts_bot.io_runtime.Win32WindowCaptureBackend", return_value=UnsupportedCaptureBackend("win32_window", "disabled")):
                        with patch("sts_bot.io_runtime.VisibleRegionCaptureBackend", return_value=UnsupportedCaptureBackend("visible_region", "disabled", foreground_only=True)):
                            capture_backend = _select_capture_backend(profile)
                            input_backend = _select_input_backend(profile)

        self.assertIsInstance(capture_backend, CaptureBackend)
        self.assertFalse(capture_backend.foreground_only)
        self.assertTrue(capture_backend.is_background_capable)
        self.assertIsInstance(input_backend, WindowMessageInputBackend)

    def test_legacy_requires_opt_in(self) -> None:
        profile = example_profile()
        profile.capture_backend_name = "legacy"
        profile.input_backend_name = "legacy"

        self.assertIsInstance(_select_capture_backend(profile), UnsupportedCaptureBackend)
        self.assertIsInstance(_select_input_backend(profile), UnsupportedInputBackend)

        profile.allow_foreground_fallback = True
        self.assertIsInstance(_select_capture_backend(profile), LegacyForegroundCaptureBackend)
        self.assertIsInstance(_select_input_backend(profile), LegacyForegroundInputBackend)


if __name__ == "__main__":
    unittest.main()
