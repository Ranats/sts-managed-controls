from __future__ import annotations

import unittest

from sts_bot.adapters.mock import MockAdapter
from sts_bot.models import (
    ActionEvaluation,
    ActionKind,
    BattleCardObservation,
    BattleTargetKind,
    DeckCard,
    EnemyState,
    GameAction,
    GameState,
    ScreenKind,
    StateSource,
)
from sts_bot.policy import (
    HeuristicPolicy,
    build_choice_context_snapshot,
    build_state_snapshot,
    evaluate_battle_card,
    infer_run_intent,
    summarize_choice,
)


class PolicyKnowledgeTest(unittest.TestCase):
    def test_build_state_snapshot_includes_axes_and_enemy_summary(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=32,
            max_hp=80,
            energy=2,
            max_energy=3,
            block=7,
            gold=91,
            character="Ironclad",
            enemies=[
                EnemyState(
                    x=800,
                    y=300,
                    width=80,
                    height=20,
                    hp=18,
                    max_hp=42,
                    intent_damage=12,
                    status_icon_count=1,
                    block=5,
                    powers={"Weak": 1},
                )
            ],
            deck=[],
            relics=["Burning Blood"],
            player_powers={"Strength": 2},
            tags=["strength"],
            state_source=StateSource.HYBRID,
            metric_sources={"hp": "memory", "gold": "ocr"},
        )

        snapshot = build_state_snapshot(state)

        self.assertEqual(snapshot["screen"], "battle")
        self.assertEqual(snapshot["enemy_count"], 1)
        self.assertEqual(snapshot["incoming_intent"], 12)
        self.assertEqual(snapshot["max_energy"], 3)
        self.assertEqual(snapshot["block"], 7)
        self.assertEqual(snapshot["enemy_block_total"], 5)
        self.assertEqual(snapshot["player_block"], 7)
        self.assertEqual(snapshot["player_powers"], {"Strength": 2})
        self.assertEqual(snapshot["enemies"][0]["powers"], {"Weak": 1})
        self.assertEqual(snapshot["relics"], ["Burning Blood"])
        self.assertEqual(snapshot["state_source"], "hybrid")
        self.assertEqual(snapshot["metric_sources"]["hp"], "memory")

    def test_policy_prefers_shrug_it_off_over_skip_in_mock_reward(self) -> None:
        adapter = MockAdapter()
        adapter.start_run()
        adapter.apply_action(adapter.available_actions()[0])

        state = adapter.current_state()
        actions = adapter.available_actions()
        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(
            evaluations["Pick Shrug It Off"].score,
            evaluations["Skip"].score,
        )
        self.assertGreater(
            evaluations["Pick Shrug It Off"].score,
            evaluations["Pick Pommel Strike"].score,
        )
        self.assertIn("kb_card:Shrug It Off", evaluations["Pick Shrug It Off"].reasons)

    def test_build_choice_context_snapshot_carries_domain_and_sources(self) -> None:
        state = GameState(
            screen=ScreenKind.EVENT,
            act=1,
            floor=6,
            hp=44,
            max_hp=80,
            energy=0,
            gold=120,
            character="Ironclad",
            state_source=StateSource.HYBRID,
            metric_sources={"hp": "memory", "gold": "ocr"},
        )
        actions = [
            GameAction(
                ActionKind.NAVIGATE,
                "Heal option",
                {"option_id": "event_1", "option_text": "Heal 20 HP", "source": "memory"},
                ["heal"],
            ),
            GameAction(
                ActionKind.NAVIGATE,
                "Greed option",
                {"option_id": "event_2", "option_text": "Lose 6 HP. Gain 100 Gold.", "source": "ocr"},
                ["gold", "hp_cost"],
            ),
        ]

        snapshot = build_choice_context_snapshot(state, actions)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["domain"], "event")
        self.assertEqual(snapshot["option_source"], "hybrid")
        self.assertEqual(snapshot["notes"], ["state_source:hybrid"])
        self.assertEqual(snapshot["options"][0]["source"], "memory")
        self.assertEqual(snapshot["options"][1]["source"], "ocr")

    def test_policy_penalizes_limit_break_without_strength_support(self) -> None:
        adapter = MockAdapter()
        adapter.start_run()
        adapter.apply_action(adapter.available_actions()[0])
        adapter.apply_action(next(action for action in adapter.available_actions() if action.label == "Pick Shrug It Off"))
        adapter.apply_action(next(action for action in adapter.available_actions() if action.label == "Take Anchor"))

        state = adapter.current_state()
        actions = adapter.available_actions()
        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(
            evaluations["Pick True Grit"].score,
            evaluations["Pick Limit Break"].score,
        )
        self.assertIn("awkward_without:strength", evaluations["Pick Limit Break"].reasons)

    def test_policy_prefers_battle_macro_over_end_turn(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=1,
            hp=70,
            max_hp=80,
            energy=3,
            gold=99,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.PLAY_CARD, "Play basic turn", {"battle_macro": "basic_turn"}, ["combat", "progress"]),
            GameAction(ActionKind.END_TURN, "End turn", {}, ["pass"]),
        ]
        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Play basic turn"].score, evaluations["End turn"].score)
        self.assertIn("prefer_playing_cards_before_end_turn", evaluations["Play basic turn"].reasons)
        summary = summarize_choice(state, actions[0], list(evaluations.values()))
        self.assertIn("screen=battle", summary)
        self.assertIn("Play basic turn", summary)

    def test_policy_prefers_heal_over_hp_payment_event_when_low(self) -> None:
        state = GameState(
            screen=ScreenKind.EVENT,
            act=1,
            floor=3,
            hp=27,
            max_hp=80,
            energy=0,
            gold=111,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.NAVIGATE, "Jungle: Push through", {"event": "jungle"}, ["gold", "progress", "hp_cost"]),
            GameAction(ActionKind.NAVIGATE, "Jungle: Rest and fight", {"event": "jungle"}, ["heal", "combat", "progress"]),
        ]
        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Jungle: Rest and fight"].score, evaluations["Jungle: Push through"].score)
        self.assertIn("low_hp_prioritize_healing", evaluations["Jungle: Rest and fight"].reasons)
        self.assertIn("low_hp_avoid_hp_payment", evaluations["Jungle: Push through"].reasons)

    def test_policy_marks_battle_pressure_and_lethal_context(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=3,
            hp=19,
            max_hp=80,
            energy=3,
            gold=99,
            character="Ironclad",
            enemies=[EnemyState(x=800, y=300, width=80, height=20, hp=9, max_hp=42, intent_damage=11, status_icon_count=1)],
        )
        actions = [
            GameAction(ActionKind.PLAY_CARD, "Play basic turn", {"battle_macro": "basic_turn"}, ["combat", "progress"]),
            GameAction(ActionKind.END_TURN, "End turn", {}, ["pass"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertIn("incoming_damage_pressure", evaluations["Play basic turn"].reasons)
        self.assertIn("enemy_near_lethal", evaluations["Play basic turn"].reasons)
        self.assertIn("unsafe_to_pass_under_pressure", evaluations["End turn"].reasons)

    def test_summarize_choice_mentions_runner_up_delta(self) -> None:
        state = GameState(
            screen=ScreenKind.REWARD_CARDS,
            act=1,
            floor=2,
            hp=70,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        action = GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block", "draw"])
        evaluations = [
            ActionEvaluation("Pick Shrug It Off", 8.5, ["kb_card:Shrug It Off", "deck_needs_block"]),
            ActionEvaluation("Skip", 2.0, ["deck_already_balanced"]),
        ]

        summary = summarize_choice(state, action, evaluations)

        self.assertIn("over Skip by 6.5", summary)
        self.assertIn("カード知識が Shrug It Off を高く評価している", summary)

    def test_policy_does_not_apply_low_hp_bias_when_hp_is_unknown(self) -> None:
        state = GameState(
            screen=ScreenKind.CARD_GRID,
            act=1,
            floor=0,
            hp=0,
            max_hp=0,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.PICK_CARD, "Card slot 1", {"card": "slot_1"}, ["attack"]),
            GameAction(ActionKind.PICK_CARD, "Card slot 6", {"card": "slot_6"}, ["block"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Card slot 1"].score, evaluations["Card slot 6"].score)
        self.assertNotIn("low_hp_values_block", evaluations["Card slot 6"].reasons)
        self.assertNotIn("critical_hp_extra_block_value", evaluations["Card slot 6"].reasons)

    def test_summarize_choice_explains_neow_default_as_calibrated_progress(self) -> None:
        state = GameState(
            screen=ScreenKind.NEOW_CHOICE,
            act=1,
            floor=0,
            hp=0,
            max_hp=0,
            energy=0,
            gold=0,
            character="Ironclad",
        )
        action = GameAction(ActionKind.NAVIGATE, "Take default whale option", {"target": "neow_default"}, ["start", "neow"])
        evaluations = [ActionEvaluation("Take default whale option", 2.0, ["tag:start", "tag:neow"])]

        summary = summarize_choice(state, action, evaluations)

        self.assertIn("Neow 3択の比較ロジックが未実装", summary)

    def test_policy_prefers_neow_cleanup_over_hp_cost(self) -> None:
        state = GameState(
            screen=ScreenKind.NEOW_CHOICE,
            act=1,
            floor=0,
            hp=64,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.NAVIGATE, "Neow option 1", {"target": "generic_neow_option", "option_index": 0, "option_text": "Transform a card."}, ["start", "neow", "progress", "transform"]),
            GameAction(ActionKind.NAVIGATE, "Neow option 2", {"target": "generic_neow_option", "option_index": 1, "option_text": "Remove 2 cards. Lose 16 HP."}, ["start", "neow", "progress", "remove", "hp_cost"]),
            GameAction(ActionKind.NAVIGATE, "Neow option 3", {"target": "generic_neow_option", "option_index": 2, "option_text": "Gain 150 Gold."}, ["start", "neow", "progress", "gold"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Neow option 1"].score, evaluations["Neow option 2"].score)
        self.assertIn("neow_prefers_cleanup", evaluations["Neow option 1"].reasons)
        self.assertIn("neow_hp_cost_penalty", evaluations["Neow option 2"].reasons)
        self.assertIn("neow_values_gold", evaluations["Neow option 3"].reasons)

    def test_summarize_choice_shows_neow_option_text(self) -> None:
        state = GameState(
            screen=ScreenKind.NEOW_CHOICE,
            act=1,
            floor=0,
            hp=64,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        action = GameAction(
            ActionKind.NAVIGATE,
            "Neow option 1",
            {"target": "generic_neow_option", "option_index": 0, "option_text": "Transform a card."},
            ["start", "neow", "progress", "transform"],
        )
        evaluations = [ActionEvaluation("Neow option 1", 5.0, ["neow_prefers_cleanup", "neow_transform_upgrade"])]

        summary = summarize_choice(state, action, evaluations)

        self.assertIn("Transform a card.", summary)
        self.assertIn("deck 圧縮や starter 改善", summary)

    def test_summarize_choice_names_starter_defend_on_card_grid(self) -> None:
        state = GameState(
            screen=ScreenKind.CARD_GRID,
            act=1,
            floor=0,
            hp=0,
            max_hp=0,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        action = GameAction(ActionKind.PICK_CARD, "Card slot 6", {"card": "slot_6"}, ["block"])
        evaluations = [ActionEvaluation("Card slot 6", 3.0, ["deck_needs_block"])]

        summary = summarize_choice(state, action, evaluations)

        self.assertIn("Card slot 6 (starter Defend)", summary)
        self.assertIn("防御が薄い", summary)

    def test_infer_run_intent_tracks_strength_direction(self) -> None:
        state = GameState(
            screen=ScreenKind.REWARD_CARDS,
            act=1,
            floor=9,
            hp=55,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
            deck=[
                DeckCard("Strike", tags=["attack"]),
                DeckCard("Inflame", tags=["strength", "scaling"]),
                DeckCard("Pommel Strike", tags=["attack", "draw"]),
            ],
        )

        intent = infer_run_intent(state)

        self.assertEqual(intent.long_term_direction, "strength")
        self.assertIn("strength", intent.deck_axes)

    def test_evaluate_battle_card_prefers_lethal_attack(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=3,
            hp=42,
            max_hp=80,
            energy=2,
            gold=99,
            character="Ironclad",
            enemies=[EnemyState(x=900, y=250, width=80, height=20, hp=6, max_hp=39, intent_damage=6)],
            deck=[DeckCard("Strike", tags=["attack"])],
        )
        observation = BattleCardObservation(
            slot=1,
            playable=True,
            energy_cost=1,
            target_kind=BattleTargetKind.ENEMY,
            card_name="Strike",
        )

        evaluated = evaluate_battle_card(state, observation)

        self.assertGreater(evaluated.score, 10.0)
        self.assertIn("lethal_or_near_lethal", evaluated.reasons)

    def test_evaluate_battle_card_prefers_block_under_pressure(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=6,
            hp=16,
            max_hp=80,
            energy=1,
            gold=99,
            character="Ironclad",
            enemies=[EnemyState(x=900, y=250, width=80, height=20, hp=24, max_hp=39, intent_damage=11)],
            deck=[DeckCard("Defend", tags=["block", "skill"])],
        )
        observation = BattleCardObservation(
            slot=2,
            playable=True,
            energy_cost=1,
            target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
            card_name="Defend",
        )

        evaluated = evaluate_battle_card(state, observation)

        self.assertGreater(evaluated.score, 10.0)
        self.assertIn("survival_bias_block", evaluated.reasons)

    def test_evaluate_battle_card_avoids_overblocking_small_hit(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=2,
            hp=64,
            max_hp=80,
            energy=1,
            gold=99,
            character="Ironclad",
            enemies=[EnemyState(x=900, y=250, width=80, height=20, hp=50, max_hp=56, intent_damage=4)],
        )
        strike = evaluate_battle_card(
            state,
            BattleCardObservation(
                slot=1,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.ENEMY,
                card_name="Strike",
            ),
        )
        defend = evaluate_battle_card(
            state,
            BattleCardObservation(
                slot=2,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                card_name="Defend",
            ),
        )

        self.assertGreater(strike.score, defend.score)
        self.assertIn("avoid_overblock", defend.reasons)

    def test_evaluate_battle_card_uses_strength_and_enemy_block_from_memory_state(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=8,
            hp=42,
            max_hp=80,
            energy=2,
            gold=99,
            character="Ironclad",
            player_powers={"Strength": 3},
            enemies=[
                EnemyState(
                    x=900,
                    y=250,
                    width=80,
                    height=20,
                    hp=7,
                    max_hp=39,
                    block=2,
                    intent_damage=6,
                    powers={"Vulnerable": 1},
                )
            ],
        )
        observation = BattleCardObservation(
            slot=1,
            playable=True,
            energy_cost=1,
            target_kind=BattleTargetKind.ENEMY,
            card_name="Strike",
        )

        evaluated = evaluate_battle_card(state, observation)

        self.assertGreaterEqual(evaluated.damage or 0, 14)
        self.assertIn("strength:3", evaluated.reasons)
        self.assertIn("enemy_vulnerable_bonus", evaluated.reasons)
        self.assertIn("enemy_block:2", evaluated.reasons)
        self.assertIn("lethal_or_near_lethal", evaluated.reasons)

    def test_evaluate_battle_card_penalizes_extra_block_when_player_block_already_covers_hit(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=5,
            hp=55,
            max_hp=80,
            energy=1,
            gold=99,
            character="Ironclad",
            block=10,
            enemies=[EnemyState(x=900, y=250, width=80, height=20, hp=24, max_hp=39, intent_damage=6)],
        )
        defend = evaluate_battle_card(
            state,
            BattleCardObservation(
                slot=1,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                card_name="Defend",
            ),
        )

        self.assertIn("existing_block_covers_hit", defend.reasons)

    def test_evaluate_battle_card_penalizes_dead_status_card(self) -> None:
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=2,
            hp=69,
            max_hp=80,
            energy=3,
            gold=99,
            character="Ironclad",
            enemies=[EnemyState(x=900, y=250, width=80, height=20, hp=26, max_hp=32, intent_damage=0)],
        )
        defend = evaluate_battle_card(
            state,
            BattleCardObservation(
                slot=1,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                card_name="Defend",
            ),
        )
        slimed = evaluate_battle_card(
            state,
            BattleCardObservation(
                slot=2,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                card_name="Slimed",
            ),
        )

        self.assertGreater(defend.score, slimed.score)
        self.assertIn("dead_card_penalty", slimed.reasons)

    def test_policy_prefers_reward_relic_over_skip(self) -> None:
        state = GameState(
            screen=ScreenKind.REWARD_RELIC,
            act=1,
            floor=7,
            hp=42,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.TAKE_RELIC, "Take Anchor", {"relic": "Anchor"}, ["block"]),
            GameAction(ActionKind.SKIP_REWARD, "Skip reward", {"target": "skip"}, ["skip"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Take Anchor"].score, evaluations["Skip reward"].score)
        self.assertIn("kb_relic:Anchor", evaluations["Take Anchor"].reasons)

    def test_policy_prefers_defensive_reward_potion_when_low_hp(self) -> None:
        state = GameState(
            screen=ScreenKind.REWARD_POTION,
            act=1,
            floor=10,
            hp=18,
            max_hp=80,
            energy=0,
            gold=120,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.TAKE_POTION, "Take Skill Potion", {"potion": "Skill Potion"}, ["block"]),
            GameAction(ActionKind.SKIP_REWARD, "Skip reward", {"target": "skip"}, ["skip"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Take Skill Potion"].score, evaluations["Skip reward"].score)
        self.assertIn("low_hp_values_potion", evaluations["Take Skill Potion"].reasons)

    def test_policy_prefers_shop_card_purchase_over_leaving(self) -> None:
        state = GameState(
            screen=ScreenKind.SHOP,
            act=1,
            floor=8,
            hp=54,
            max_hp=80,
            energy=0,
            gold=130,
            character="Ironclad",
            deck=[
                DeckCard("Strike", tags=["attack"]),
                DeckCard("Strike", tags=["attack"]),
                DeckCard("Defend", tags=["block"]),
            ],
        )
        actions = [
            GameAction(ActionKind.BUY, "Buy Shrug It Off", {"shop_item_type": "card", "card": "Shrug It Off", "price": 49}, ["shop", "card"]),
            GameAction(ActionKind.NAVIGATE, "Leave shop", {}, ["progress", "shop_exit"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Buy Shrug It Off"].score, evaluations["Leave shop"].score)
        self.assertIn("kb_card:Shrug It Off", evaluations["Buy Shrug It Off"].reasons)
        self.assertIn("shop_price_penalty", evaluations["Buy Shrug It Off"].reasons)

    def test_policy_prefers_shop_remove_service_over_leaving(self) -> None:
        state = GameState(
            screen=ScreenKind.SHOP,
            act=1,
            floor=9,
            hp=61,
            max_hp=80,
            energy=0,
            gold=150,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.BUY, "Buy remove service", {"shop_item_type": "remove", "price": 75}, ["shop", "remove"]),
            GameAction(ActionKind.NAVIGATE, "Leave shop", {}, ["progress", "shop_exit"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Buy remove service"].score, evaluations["Leave shop"].score)
        self.assertIn("event_cleanup", evaluations["Buy remove service"].reasons)

    def test_policy_prefers_rest_over_smith_when_hp_is_low(self) -> None:
        state = GameState(
            screen=ScreenKind.REST,
            act=2,
            floor=23,
            hp=20,
            max_hp=80,
            energy=0,
            gold=210,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.REST, "Rest", {"rest_action": "rest"}, ["heal"]),
            GameAction(ActionKind.SMITH, "Smith", {"rest_action": "smith"}, ["upgrade"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Rest"].score, evaluations["Smith"].score)
        self.assertIn("rest_to_stabilize", evaluations["Rest"].reasons)

    def test_policy_avoids_boss_relic_with_sustain_downside_when_low_hp(self) -> None:
        state = GameState(
            screen=ScreenKind.BOSS_RELIC,
            act=1,
            floor=17,
            hp=18,
            max_hp=80,
            energy=0,
            gold=160,
            character="Ironclad",
            relics=["Burning Blood"],
        )
        actions = [
            GameAction(ActionKind.TAKE_RELIC, "Take Black Blood", {"boss_relic": "Black Blood", "relic": "Black Blood"}, ["boss", "relic"]),
            GameAction(ActionKind.TAKE_RELIC, "Take Coffee Dripper", {"boss_relic": "Coffee Dripper", "relic": "Coffee Dripper"}, ["boss", "relic", "energy"]),
        ]

        policy = HeuristicPolicy()
        evaluations = {item.action_label: item for item in policy.evaluate_actions(state, actions)}

        self.assertGreater(evaluations["Take Black Blood"].score, evaluations["Take Coffee Dripper"].score)
        self.assertIn("boss_relic_sustain_risk", evaluations["Take Coffee Dripper"].reasons)


if __name__ == "__main__":
    unittest.main()
