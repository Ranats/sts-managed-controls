from __future__ import annotations

import unittest

from sts_bot.gamepad import BUTTON_ALIASES


class GamepadTest(unittest.TestCase):
    def test_button_aliases_cover_basic_title_inputs(self) -> None:
        self.assertEqual(BUTTON_ALIASES["a"], "XUSB_GAMEPAD_A")
        self.assertEqual(BUTTON_ALIASES["start"], "XUSB_GAMEPAD_START")
        self.assertEqual(BUTTON_ALIASES["dpad_down"], "XUSB_GAMEPAD_DPAD_DOWN")


if __name__ == "__main__":
    unittest.main()
