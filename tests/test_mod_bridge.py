from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sts_bot.managed_probe import ManagedProbeError
from sts_bot.mod_bridge import (
    BRIDGE_MANIFEST_NAME,
    _decode_process_output,
    install_bridge_mod,
    send_bridge_add_card,
    send_bridge_apply_power,
    send_bridge_clear_auto_power_on_combat_start,
    send_bridge_jump_to_map_coord,
    send_bridge_replace_master_deck,
    send_bridge_obtain_relic,
    send_bridge_set_auto_power_on_combat_start,
    send_bridge_tune_card_var,
    send_bridge_tune_relic_var,
)


class ModBridgeTest(unittest.TestCase):
    def test_decode_process_output_uses_locale_and_replaces_invalid_bytes(self) -> None:
        self.assertEqual(_decode_process_output(b"\x82\xa0"), "あ")
        self.assertEqual(_decode_process_output(b"\x92"), "�")

    def test_install_bridge_mod_writes_manifest_and_dll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            game_dir = root / "game"
            sts2_dll = game_dir / "data_sts2_windows_x86_64" / "sts2.dll"
            sts2_dll.parent.mkdir(parents=True, exist_ok=True)
            sts2_dll.write_text("stub", encoding="utf-8")
            workspace = root / "workspace"
            source = workspace / "tmp" / "codex_bridge_mod" / "CodexBridge.cs"
            output = workspace / "tmp" / "codex_bridge_mod" / "CodexBridge.dll"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("// stub", encoding="utf-8")
            output.write_text("dll", encoding="utf-8")

            with patch("sts_bot.mod_bridge._build_bridge_mod") as mock_build:
                result = install_bridge_mod(game_dir=game_dir, workspace_dir=workspace)

        self.assertTrue(result.dll_path.endswith("CodexBridge.dll"))
        self.assertTrue(result.manifest_path.endswith(BRIDGE_MANIFEST_NAME))
        self.assertEqual(result.manifest["has_dll"], True)
        self.assertEqual(result.manifest["has_pck"], False)
        self.assertEqual(result.manifest["affects_gameplay"], True)
        mock_build.assert_called_once()

    def test_install_bridge_mod_reports_locked_dll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            game_dir = root / "game"
            sts2_dll = game_dir / "data_sts2_windows_x86_64" / "sts2.dll"
            sts2_dll.parent.mkdir(parents=True, exist_ok=True)
            sts2_dll.write_text("stub", encoding="utf-8")
            workspace = root / "workspace"
            source = workspace / "tmp" / "codex_bridge_mod" / "CodexBridge.cs"
            output = workspace / "tmp" / "codex_bridge_mod" / "CodexBridge.dll"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("// stub", encoding="utf-8")
            output.write_text("dll", encoding="utf-8")

            with (
                patch("sts_bot.mod_bridge._build_bridge_mod"),
                patch("sts_bot.mod_bridge.shutil.copy2", side_effect=PermissionError("locked")),
            ):
                with self.assertRaises(ManagedProbeError) as cm:
                    install_bridge_mod(game_dir=game_dir, workspace_dir=workspace)

        self.assertIn("bridge dll is locked", str(cm.exception))

    def test_send_bridge_apply_power_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_apply_power(power_type="StrengthPower", amount=100, target="player")

        self.assertIn("action=apply_power", pipe.written)
        self.assertIn("power_type=StrengthPower", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_add_card_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_add_card(card_type="Whirlwind", destination="hand", count=2, upgrade_count=1)

        self.assertIn("action=add_card_to_hand", pipe.written)
        self.assertIn("card_type=Whirlwind", pipe.written)
        self.assertIn("count=2", pipe.written)
        self.assertIn("upgrade_count=1", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_replace_master_deck_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_replace_master_deck(card_type="Whirlwind", count=10, upgrade_count=2)

        self.assertIn("action=replace_master_deck", pipe.written)
        self.assertIn("card_type=Whirlwind", pipe.written)
        self.assertIn("count=10", pipe.written)
        self.assertIn("upgrade_count=2", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_obtain_relic_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_obtain_relic(relic_type="Anchor", count=2)

        self.assertIn("action=obtain_relic", pipe.written)
        self.assertIn("relic_type=Anchor", pipe.written)
        self.assertIn("count=2", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_set_auto_power_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=configured\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_set_auto_power_on_combat_start(power_type="StrengthPower", amount=100, target="player")

        self.assertIn("action=set_auto_power_on_combat_start", pipe.written)
        self.assertIn("power_type=StrengthPower", pipe.written)
        self.assertEqual(result.response["status"], "configured")

    def test_send_bridge_clear_auto_power_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=cleared;count=1\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_clear_auto_power_on_combat_start(power_type="StrengthPower", target="player")

        self.assertIn("action=clear_auto_power_on_combat_start", pipe.written)
        self.assertEqual(result.response["status"], "cleared")

    def test_send_bridge_jump_to_map_coord_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_jump_to_map_coord(col=2, row=8)

        self.assertIn("action=jump_to_map_coord", pipe.written)
        self.assertIn("col=2", pipe.written)
        self.assertIn("row=8", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_tune_card_var_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_tune_card_var(card_type="Whirlwind", var_name="Damage", amount=99, scope="hand", mode="set")

        self.assertIn("action=tune_card_var", pipe.written)
        self.assertIn("var_name=Damage", pipe.written)
        self.assertIn("scope=hand", pipe.written)
        self.assertEqual(result.response["status"], "queued")

    def test_send_bridge_tune_relic_var_serializes_request(self) -> None:
        class _FakePipe:
            def __init__(self) -> None:
                self.written = ""

            def write(self, text: str) -> None:
                self.written += text

            def flush(self) -> None:
                return None

            def readline(self) -> str:
                return 'ok=true;status=queued\n'

        pipe = _FakePipe()

        class _PipeContext:
            def __enter__(self):
                return pipe

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("sts_bot.mod_bridge._open_pipe", return_value=_PipeContext()):
            result = send_bridge_tune_relic_var(relic_type="FestivePopper", var_name="Damage", amount=99, mode="set")

        self.assertIn("action=tune_relic_var", pipe.written)
        self.assertIn("relic_type=FestivePopper", pipe.written)
        self.assertIn("var_name=Damage", pipe.written)
        self.assertEqual(result.response["status"], "queued")


if __name__ == "__main__":
    unittest.main()
