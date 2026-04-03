from __future__ import annotations

import unittest
from pathlib import Path

from sts_bot.knowledge import (
    active_kb_overlay_path,
    apply_overlay_operations,
    canonicalize_card_name,
    lookup_boss_relic_knowledge,
    lookup_event_choice,
    set_active_kb_overlay_path,
)


class KnowledgeOverlayTest(unittest.TestCase):
    def test_overlay_updates_choice_alias_and_boss_relic_entries(self) -> None:
        Path("tmp").mkdir(exist_ok=True)
        overlay_path = Path("tmp") / "kb_overlay_test.json"
        if overlay_path.exists():
            overlay_path.unlink()
        original_path = active_kb_overlay_path()
        try:
            set_active_kb_overlay_path(overlay_path)

            result = apply_overlay_operations(
                [
                    {
                        "target_type": "choice",
                        "target_key": "event:heal",
                        "payload": {"name": "Heal", "base_score": 4.25, "tags": ["heal"]},
                        "reason": "Repeated low HP events favored healing.",
                    },
                    {
                        "target_type": "alias",
                        "target_key": "card:shrg it off",
                        "payload": {"canonical": "shrug it off"},
                        "reason": "OCR alias for Shrug It Off.",
                    },
                    {
                        "target_type": "boss_relic",
                        "target_key": "ectoplasm",
                        "payload": {"name": "Ectoplasm", "base_score": 1.25, "downside_tags": ["no_more_gold"]},
                        "reason": "New boss relic observed in live run.",
                    },
                ]
            )

            self.assertEqual(result["applied"], 3)
            self.assertAlmostEqual(lookup_event_choice("heal").base_score, 4.25)
            self.assertEqual(canonicalize_card_name("shrg it off"), "Shrug It Off")
            self.assertEqual(lookup_boss_relic_knowledge("ectoplasm").name, "Ectoplasm")
        finally:
            set_active_kb_overlay_path(original_path)
            if overlay_path.exists():
                overlay_path.unlink()


if __name__ == "__main__":
    unittest.main()
