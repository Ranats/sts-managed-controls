from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from sts_bot.capture_backends import VisibleRegionCaptureBackend, WgcCaptureBackend


class FakeCaptureControl:
    def __init__(self) -> None:
        self.stopped = False
        self.waited = False

    def stop(self) -> None:
        self.stopped = True

    def wait(self) -> None:
        self.waited = True


class FakeWindowsCapture:
    last_instance: "FakeWindowsCapture | None" = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.handlers: dict[str, object] = {}
        self.control = FakeCaptureControl()
        FakeWindowsCapture.last_instance = self

    def event(self, handler):
        self.handlers[handler.__name__] = handler
        return handler

    def start_free_threaded(self) -> FakeCaptureControl:
        return self.control


class WgcCaptureBackendTest(unittest.TestCase):
    def test_wgc_backend_reads_latest_frame_from_event_stream(self) -> None:
        fake_module = SimpleNamespace(WindowsCapture=FakeWindowsCapture)
        backend = WgcCaptureBackend()
        target = SimpleNamespace(title="Slay the Spire 2", refresh=lambda: None)

        with patch("sts_bot.capture_backends.importlib.import_module", return_value=fake_module):
            backend.open(target)
            assert FakeWindowsCapture.last_instance is not None
            frame_handler = FakeWindowsCapture.last_instance.handlers["on_frame_arrived"]
            bgra = np.zeros((4, 5, 4), dtype=np.uint8)
            bgra[:, :, 0] = 10
            bgra[:, :, 1] = 20
            bgra[:, :, 2] = 30
            frame = SimpleNamespace(frame_buffer=bgra)
            frame_handler(frame, SimpleNamespace(stop=lambda: None))
            image = backend.read_latest_frame(timeout_ms=50)
            backend.close()

        self.assertEqual(image.size, (5, 4))
        self.assertEqual(image.getpixel((0, 0)), (30, 20, 10))
        self.assertTrue(FakeWindowsCapture.last_instance.control.stopped)
        self.assertTrue(FakeWindowsCapture.last_instance.control.waited)

    def test_wgc_backend_crops_window_frame_to_client_region(self) -> None:
        backend = WgcCaptureBackend()
        backend.target = SimpleNamespace(
            window_rect=SimpleNamespace(left=10, top=20, width=120, height=80),
            client_rect=SimpleNamespace(left=20, top=40, width=100, height=50),
            client_size=(100, 50),
            refresh=lambda: None,
        )
        image = Image.new("RGB", (120, 80), "black")

        normalized = backend._normalize_frame(image)

        self.assertEqual(normalized.size, (100, 50))

    def test_visible_region_backend_reports_not_background_reliable(self) -> None:
        backend = VisibleRegionCaptureBackend()

        diagnostics = backend.diagnostics()

        self.assertFalse(diagnostics.background_capable)
        self.assertTrue(diagnostics.foreground_only)

    def test_visible_region_backend_does_not_focus_target_by_default(self) -> None:
        backend = VisibleRegionCaptureBackend()
        target = SimpleNamespace(hwnd=101, refresh=lambda: None)

        with patch("sts_bot.capture_backends.focus_window") as focus_window:
            with patch("sts_bot.capture_backends.capture_screen_client_region", return_value=Image.new("RGB", (8, 8), "white")):
                backend.open(target)
                backend.read_latest_frame(timeout_ms=50)

        focus_window.assert_not_called()

    def test_visible_region_backend_can_opt_in_to_focus_target(self) -> None:
        backend = VisibleRegionCaptureBackend(focus_target=True)
        target = SimpleNamespace(hwnd=101, refresh=lambda: None)

        with patch("sts_bot.capture_backends.focus_window") as focus_window:
            with patch("sts_bot.capture_backends.capture_screen_client_region", return_value=Image.new("RGB", (8, 8), "white")):
                backend.open(target)
                backend.read_latest_frame(timeout_ms=50)

        self.assertGreaterEqual(focus_window.call_count, 1)


if __name__ == "__main__":
    unittest.main()
