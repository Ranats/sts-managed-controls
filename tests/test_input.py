from __future__ import annotations

import unittest

from sts_bot.input import backend_candidates


class InputBackendTest(unittest.TestCase):
    def test_combined_backend_prefers_sendinput(self) -> None:
        self.assertEqual(
            backend_candidates("combined", include_key_only=True),
            ["sendinput_scan", "sendinput", "legacy_event", "directinput", "pyautogui", "hwnd"],
        )

    def test_all_backends_excludes_key_only_by_default(self) -> None:
        self.assertEqual(
            backend_candidates("all"),
            ["sendinput", "legacy_event", "directinput", "pyautogui", "hwnd"],
        )

    def test_drag_prefers_mouse_capable_backends(self) -> None:
        self.assertEqual(
            [candidate for candidate in backend_candidates("combined") if candidate in {"directinput", "pyautogui"}],
            ["directinput", "pyautogui"],
        )


if __name__ == "__main__":
    unittest.main()
