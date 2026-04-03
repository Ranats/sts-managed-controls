from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from sts_bot.game_catalog import (
    CatalogEntry,
    filter_catalog,
    load_card_catalog,
)
from sts_bot.type_probe import TypeListingResult


class GameCatalogTest(unittest.TestCase):
    def test_filter_catalog_matches_display_and_short_name_tokens(self) -> None:
        entries = (
            CatalogEntry(
                kind="card",
                type_name="MegaCrit.Sts2.Core.Models.Cards.AdaptiveStrike",
                short_name="AdaptiveStrike",
                display_name="Adaptive Strike",
            ),
            CatalogEntry(
                kind="card",
                type_name="MegaCrit.Sts2.Core.Models.Cards.Whirlwind",
                short_name="Whirlwind",
                display_name="Whirlwind",
            ),
        )

        filtered = filter_catalog(entries, "adaptive strike")

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].short_name, "AdaptiveStrike")

    @patch("sts_bot.game_catalog.list_game_types")
    def test_load_card_catalog_builds_sorted_entries(self, mock_list_game_types) -> None:
        mock_list_game_types.return_value = TypeListingResult.from_payload(
            {
                "assembly_path": "C:\\game\\sts2.dll",
                "namespace_prefix": "MegaCrit.Sts2.Core.Models.Cards",
                "assignable_to": "MegaCrit.Sts2.Core.Models.CardModel",
                "types": [
                    {
                        "type_name": "MegaCrit.Sts2.Core.Models.Cards.Whirlwind",
                        "base_type": "MegaCrit.Sts2.Core.Models.CardModel",
                        "is_abstract": False,
                        "has_parameterless_constructor": True,
                    },
                    {
                        "type_name": "MegaCrit.Sts2.Core.Models.Cards.AdaptiveStrike",
                        "base_type": "MegaCrit.Sts2.Core.Models.CardModel",
                        "is_abstract": False,
                        "has_parameterless_constructor": True,
                    },
                ],
            }
        )

        entries = load_card_catalog(assembly_path=Path(__file__), workspace_dir=Path.cwd())

        self.assertEqual([entry.short_name for entry in entries], ["AdaptiveStrike", "Whirlwind"])
        self.assertEqual(entries[0].display_name, "Adaptive Strike")
        self.assertTrue(entries[1].has_parameterless_constructor)


if __name__ == "__main__":
    unittest.main()
