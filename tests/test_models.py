from __future__ import annotations

import unittest

from sts_bot.models import (
    BuildIntentPreset,
    ChoiceContext,
    ChoiceDomain,
    ChoiceOption,
    RunIntent,
    ScreenKind,
    StateSource,
)


class ChoiceSchemaModelTest(unittest.TestCase):
    def test_choice_context_carries_options_and_build_preset(self) -> None:
        preset = BuildIntentPreset(
            name="Ironclad block",
            character="Ironclad",
            desired_axes=["block"],
            avoid_axes=["glass_cannon"],
        )
        context = ChoiceContext(
            domain=ChoiceDomain.NEOW,
            screen=ScreenKind.NEOW_CHOICE,
            character="Ironclad",
            act=1,
            floor=0,
            ascension=10,
            hp=80,
            max_hp=80,
            run_intent=RunIntent(deck_axes=["block"], long_term_direction="block"),
            build_preset=preset,
            option_source=StateSource.HYBRID,
            options=[
                ChoiceOption(
                    option_id="neow_1",
                    label="Neow option 1",
                    text="Transform a card.",
                    source=StateSource.OCR,
                )
            ],
        )

        self.assertEqual(context.domain, ChoiceDomain.NEOW)
        self.assertEqual(context.build_preset.name, "Ironclad block")
        self.assertEqual(context.options[0].text, "Transform a card.")
        self.assertEqual(context.option_source, StateSource.HYBRID)


if __name__ == "__main__":
    unittest.main()
