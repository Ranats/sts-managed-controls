from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sts_bot.dev_console import enable_full_console, find_settings_save_paths, run_dev_console_command


class DevConsoleSettingsTest(unittest.TestCase):
    def test_find_settings_save_paths_discovers_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = root / "steam" / "123" / "settings.save"
            second = root / "default" / "1" / "settings.save"
            first.parent.mkdir(parents=True, exist_ok=True)
            second.parent.mkdir(parents=True, exist_ok=True)
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")

            discovered = find_settings_save_paths(settings_root=root)

        self.assertEqual(discovered, [second.resolve(), first.resolve()])

    def test_enable_full_console_updates_missing_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            settings = root / "steam" / "123" / "settings.save"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"fps_limit": 60}, indent=2), encoding="utf-8")

            result = enable_full_console(settings_root=root)
            payload = json.loads(settings.read_text(encoding="utf-8"))

        self.assertEqual(result.updated_paths, [str(settings.resolve())])
        self.assertEqual(result.unchanged_paths, [])
        self.assertTrue(payload["full_console"])

    def test_enable_full_console_leaves_existing_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            settings = root / "settings.save"
            settings.write_text(json.dumps({"full_console": True}, indent=2), encoding="utf-8")

            result = enable_full_console(settings_root=settings)

        self.assertEqual(result.updated_paths, [])
        self.assertEqual(result.unchanged_paths, [str(settings.resolve())])


class DevConsoleCommandTest(unittest.TestCase):
    @patch("sts_bot.dev_console._type_text")
    @patch("sts_bot.dev_console.perform_key")
    @patch("sts_bot.dev_console.focus_window")
    @patch("sts_bot.dev_console.create_runtime")
    @patch("sts_bot.dev_console.enable_full_console")
    def test_run_dev_console_command_focuses_and_types(
        self,
        mock_enable_console,
        mock_create_runtime,
        mock_focus_window,
        mock_perform_key,
        mock_type_text,
    ) -> None:
        runtime = SimpleNamespace(target=SimpleNamespace(pid=4321, hwnd=0x1234), close=lambda: None)
        mock_create_runtime.return_value = runtime
        mock_enable_console.return_value = SimpleNamespace(
            updated_paths=["C:\\settings.save"],
            unchanged_paths=[],
            searched_root="C:\\root",
        )

        result = run_dev_console_command(SimpleNamespace(), "help power", close_console=False)

        self.assertEqual(result.pid, 4321)
        self.assertEqual(result.hwnd, 0x1234)
        self.assertFalse(result.close_console)
        mock_focus_window.assert_called_once_with(hwnd=0x1234)
        self.assertEqual(mock_perform_key.call_count, 2)
        mock_type_text.assert_called_once_with("help power", interval=0.01)


if __name__ == "__main__":
    unittest.main()
