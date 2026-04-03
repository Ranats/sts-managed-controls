from __future__ import annotations

import unittest

from sts_bot.config import Rect
from sts_bot.windowing import CoordinateTransform, TargetWindow


class CoordinateTransformTest(unittest.TestCase):
    def test_reference_to_client_scales_points(self) -> None:
        target = TargetWindow(
            hwnd=1,
            title="test",
            class_name="cls",
            pid=10,
            process_name="proc.exe",
            window_rect=Rect(0, 0, 800, 600),
            client_rect=Rect(100, 200, 800, 600),
            dpi=96,
        )
        transform = CoordinateTransform(reference_width=1600, reference_height=1200, target=target)

        self.assertEqual(transform.reference_to_client((400, 300)), (200, 150))
        self.assertEqual(transform.client_to_screen((200, 150)), (300, 350))


if __name__ == "__main__":
    unittest.main()
