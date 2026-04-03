from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sts_bot.analysis import build_fix_request_markdown, latest_run_id, load_run_trace, render_run_trace
from sts_bot.logging import RunLogger
from sts_bot.models import (
    ActionEvaluation,
    ActionKind,
    DeckCard,
    ExecutionExpectation,
    ExecutionObservation,
    GameAction,
    GameState,
    RunIntent,
    RunSummary,
    ScreenKind,
    StateSource,
)


class AnalysisTraceTest(unittest.TestCase):
    def test_render_run_trace_includes_reasoning_and_state(self) -> None:
        fd, raw_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        db_path = Path(raw_path)
        try:
            logger = RunLogger(db_path)
            logger.init_db()
            logger.start_run()
            state = GameState(
                screen=ScreenKind.REWARD_CARDS,
                act=1,
                floor=2,
                hp=62,
                max_hp=80,
                energy=0,
                gold=99,
                character="Ironclad",
                deck=[DeckCard("Bash", tags=["attack"]), DeckCard("Defend", tags=["block"])],
                relics=["Burning Blood"],
                run_intent=RunIntent(
                    deck_axes=["block"],
                    short_term_survival_need="stabilize",
                    long_term_direction="block",
                    elite_boss_risk_posture="cautious",
                ),
                state_source=StateSource.HYBRID,
                metric_sources={"hp": "memory", "gold": "ocr"},
            )
            action = GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block", "draw"])
            evaluations = [
                ActionEvaluation("Pick Shrug It Off", 8.5, ["kb_card:Shrug It Off", "deck_needs_block"]),
                ActionEvaluation("Skip", 1.0, ["deck_already_balanced"]),
            ]
            logger.log_decision(
                state,
                action,
                evaluations=evaluations,
                provider_name="codex",
                expected_outcome=ExecutionExpectation(
                    next_screen="continue",
                    change_summary="reward should close",
                    verification_hint="reward_progress",
                ),
                provider_reasoning="Current deck leans block, so Shrug It Off improves immediate survival.",
            )
            logger.log_verification(
                observed_outcome=ExecutionObservation(
                    screen="continue",
                    hp=62,
                    max_hp=80,
                    energy=0,
                    gold=99,
                    floor=2,
                    actions=["Continue"],
                    state_source="memory",
                    metric_sources={"hp": "memory", "gold": "memory"},
                    note="source=memory | codex_fallback=TimeoutExpired:demo",
                ),
                verification_status="matched",
            )
            run_id = logger.finish_run(
                RunSummary(
                    character="Ironclad",
                    won=False,
                    act_reached=1,
                    floor_reached=2,
                    score=0,
                    deck=state.deck,
                    relics=state.relics,
                    picked_cards=["Shrug It Off"],
                    skipped_cards=[],
                    path=["combat", "reward"],
                    strategy_tags=["block"],
                )
            )

            selected_run_id = latest_run_id(db_path)
            loaded_run_id, trace = load_run_trace(db_path, run_id=run_id)
            rendered = render_run_trace(loaded_run_id, trace)
        finally:
            pass
        self.assertEqual(selected_run_id, run_id)
        self.assertIn("Pick Shrug It Off", rendered)
        self.assertIn("why:", rendered)
        self.assertIn("provider: codex", rendered)
        self.assertIn("model: Current deck leans block", rendered)
        self.assertIn("intent:", rendered)
        self.assertIn("state: hp=62/80", rendered)
        self.assertIn("source=hybrid", rendered)
        self.assertIn("metrics: hp:memory", rendered)
        self.assertIn("reasons: kb_card:Shrug It Off", rendered)
        self.assertIn("expected: next_screen=continue", rendered)
        self.assertIn("observed: screen=continue", rendered)
        self.assertIn("observed_metrics: hp:memory", rendered)
        self.assertIn("observed_note: source=memory | codex_fallback=TimeoutExpired:demo", rendered)
        self.assertIn("verify: matched", rendered)

    def test_build_fix_request_markdown_summarizes_mismatches(self) -> None:
        trace = [
            {
                "step_index": 4,
                "screen": "reward_cards",
                "floor": 5,
                "action_label": "Card option 2",
                "provider_name": "codex",
                "provider_reasoning_text": "Current deck still needs block.",
                "expected_outcome": {"next_screen": "continue", "change_summary": "reward should close"},
                "observed_outcome": {"screen": "reward_cards", "hp": 55, "max_hp": 80, "energy": 0},
                "verification_status": "partial",
            }
        ]

        markdown = build_fix_request_markdown("run-123", trace)

        self.assertIn("# Fix Request", markdown)
        self.assertIn("run_id: `run-123`", markdown)
        self.assertIn("mismatched_steps: 1", markdown)
        self.assertIn("screen=reward_cards floor=5 action=Card option 2", markdown)
        self.assertIn("Current deck still needs block.", markdown)


if __name__ == "__main__":
    unittest.main()
