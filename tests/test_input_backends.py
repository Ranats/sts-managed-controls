from __future__ import annotations

import unittest
from unittest.mock import patch

from sts_bot.input_backends import WindowMessageInputBackend


class WindowMessageBackendTest(unittest.TestCase):
    def test_key_press_emits_char_for_printable_keys(self) -> None:
        backend = WindowMessageInputBackend(dry_run=True)

        with patch.object(backend, "key_down") as mock_down:
            with patch.object(backend, "key_up") as mock_up:
                with patch.object(backend, "_char") as mock_char:
                    backend.key_press("1", hold_ms=0)

        mock_down.assert_called_once_with("1")
        mock_up.assert_called_once_with("1")
        mock_char.assert_called_once_with("1")

    def test_key_press_skips_char_for_named_keys(self) -> None:
        backend = WindowMessageInputBackend(dry_run=True)

        with patch.object(backend, "key_down") as mock_down:
            with patch.object(backend, "key_up") as mock_up:
                with patch.object(backend, "_char") as mock_char:
                    backend.key_press("enter", hold_ms=0)

        mock_down.assert_called_once_with("enter")
        mock_up.assert_called_once_with("enter")
        mock_char.assert_not_called()

    def test_move_emits_single_mousemove_message(self) -> None:
        backend = WindowMessageInputBackend(dry_run=False)
        backend.target = type("Target", (), {"refresh": lambda self: None, "hwnd": 100})()

        with patch.object(backend, "_resolve_mouse_target", return_value=(100, (12, 34))):
            with patch.object(backend, "_maybe_activate_for_click") as mock_activate:
                with patch.object(backend, "_send") as mock_send:
                    backend.move(200, 300)

        mock_activate.assert_called_once_with(100)
        mock_send.assert_called_once()
        args = mock_send.call_args.args
        self.assertEqual(args[0], 100)
        self.assertEqual(args[1], 0x0200)


if __name__ == "__main__":
    unittest.main()
