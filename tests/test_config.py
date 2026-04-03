from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from sts_bot.config import CalibrationProfile, write_example_profile
from sts_bot.models import ScreenKind


class ConfigTest(unittest.TestCase):
    def test_example_profile_round_trip(self) -> None:
        base_tmp = Path(__file__).resolve().parents[1] / "tmp"
        base_tmp.mkdir(parents=True, exist_ok=True)
        profile_path = base_tmp / f"windows.example.{uuid4().hex}.json"
        try:
            write_example_profile(profile_path)

            payload = json.loads(profile_path.read_text(encoding="utf-8"))
            profile = CalibrationProfile.load(profile_path)

            self.assertEqual(payload["window_title"], "Slay the Spire 2")
            self.assertEqual(profile.window_title, "Slay the Spire 2")
            self.assertEqual(payload["window_message_activation"], "none")
            self.assertEqual(profile.window_message_activation, "none")
            self.assertEqual(payload["battle_cancel_key"], "down")
            self.assertEqual(profile.battle_cancel_key, "down")
            self.assertIn("Single Play", profile.startup_sequence_labels)
            self.assertEqual(profile.scene_input_backends["menu"], "gamepad")
            self.assertEqual(profile.scene_input_backends["battle"], "gamepad")
            self.assertEqual(profile.scene_input_backends["reward_menu"], "window_messages")
            self.assertEqual(profile.scene_input_backends["reward_cards"], "window_messages")
            self.assertEqual(profile.scene_input_backends["reward_gold_only"], "window_messages")
            self.assertEqual(profile.scene_input_backends["event"], "gamepad")
            self.assertEqual(profile.scene_input_backends["continue"], "gamepad")
            self.assertEqual(profile.scene_input_backends["game_over"], "window_messages")
            self.assertFalse(profile.memory_read.enabled)
            self.assertEqual(profile.memory_read.module, "sts2.dll")
            self.assertEqual(profile.memory_read.refresh_ms, 250)
            self.assertEqual(profile.memory_read.fields, [])
            self.assertTrue(profile.anchors)
            self.assertEqual(profile.anchors[0].screen, ScreenKind.MENU)
            self.assertTrue(any(action.label == "Skip reward" for action in profile.actions))
            self.assertTrue(any(action.label == "Take gold" for action in profile.actions))
            self.assertTrue(any(action.label == "Take gold and continue" for action in profile.actions))
            self.assertTrue(any(action.label == "Continue" for action in profile.actions))
            self.assertTrue(any(action.label == "Main menu" for action in profile.actions))
            self.assertTrue(any(region.name == "floor" for region in profile.text_regions))
        finally:
            profile_path.unlink(missing_ok=True)

    def test_memory_read_round_trip_preserves_fields(self) -> None:
        base_tmp = Path(__file__).resolve().parents[1] / "tmp"
        base_tmp.mkdir(parents=True, exist_ok=True)
        profile_path = base_tmp / f"windows.memory.{uuid4().hex}.json"
        try:
            payload = {
                "window_title": "Slay the Spire 2",
                "memory_read": {
                    "enabled": True,
                    "module": "sts2.dll",
                    "refresh_ms": 125,
                    "fields": [
                        {
                            "name": "gold",
                            "screens": ["battle", "map"],
                            "locator_kind": "module_offset",
                            "offset": "0x120",
                            "pointer_offsets": ["0x10", 32],
                            "value_type": "int32",
                        }
                    ],
                },
            }
            profile_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

            profile = CalibrationProfile.load(profile_path)

            self.assertTrue(profile.memory_read.enabled)
            self.assertEqual(profile.memory_read.refresh_ms, 125)
            self.assertEqual(profile.memory_read.fields[0].name, "gold")
            self.assertEqual(profile.memory_read.fields[0].screens, [ScreenKind.BATTLE, ScreenKind.MAP])
            self.assertEqual(profile.memory_read.fields[0].offset, 0x120)
            self.assertEqual(profile.memory_read.fields[0].pointer_offsets, [0x10, 32])

            round_trip = profile.to_dict(base_dir=profile_path.parent)
            self.assertTrue(round_trip["memory_read"]["enabled"])
            self.assertEqual(round_trip["memory_read"]["fields"][0]["offset"], 0x120)
            self.assertEqual(round_trip["memory_read"]["fields"][0]["pointer_offsets"], [0x10, 32])
        finally:
            profile_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
