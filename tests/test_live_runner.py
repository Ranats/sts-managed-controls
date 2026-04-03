from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from PIL import Image

from sts_bot.live_runner import LiveLoopRunner
from sts_bot.logging import RunLogger
from sts_bot.models import (
    ActionKind,
    DecisionProviderResult,
    ExecutionExpectation,
    GameAction,
    GameState,
    RunSummary,
    ScreenKind,
    StateSource,
)


class _AdapterStub:
    def __init__(self, *, stop_after_apply: bool = True) -> None:
        self._states = [
            GameState(
                screen=ScreenKind.BATTLE,
                act=1,
                floor=4,
                hp=40,
                max_hp=80,
                energy=3,
            gold=90,
            character="Ironclad",
            available_actions=[GameAction(ActionKind.END_TURN, "End turn")],
            state_source=StateSource.HYBRID,
            metric_sources={"energy": "memory", "gold": "ocr"},
        ),
        GameState(
            screen=ScreenKind.BATTLE,
                act=1,
                floor=4,
                hp=40,
                max_hp=80,
                energy=0,
            gold=90,
            character="Ironclad",
            available_actions=[GameAction(ActionKind.END_TURN, "End turn")],
            state_source=StateSource.MEMORY,
            metric_sources={"energy": "memory", "gold": "memory"},
        ),
        ]
        self._index = 0
        self._applied = 0
        self._stop_after_apply = stop_after_apply

    def start_run(self, focus: bool = False) -> None:
        del focus

    def current_state(self) -> GameState:
        return self._states[min(self._index, len(self._states) - 1)]

    def available_actions(self) -> list[GameAction]:
        return self.current_state().available_actions

    def apply_action(self, action: GameAction) -> None:
        self._applied += 1
        self._index = 1

    def is_run_over(self) -> bool:
        return self._stop_after_apply and self._applied >= 1

    def run_summary(self) -> RunSummary:
        return RunSummary(
            character="Ironclad",
            won=False,
            act_reached=1,
            floor_reached=4,
            score=0,
            deck=[],
            relics=[],
            picked_cards=[],
            skipped_cards=[],
            path=[],
            strategy_tags=[],
        )

    def close(self) -> None:
        return None


class _ProviderStub:
    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        return DecisionProviderResult(
            provider_name="heuristic",
            action=actions[0],
            reasoning="Energy is available, so end the turn in the stub.",
            expected_outcome=ExecutionExpectation(
                next_screen="battle",
                change_summary="energy should change",
                verification_hint="battle_turn_progress",
            ),
        )

    def current_run_intent(self):
        return None


class _LowConfidenceProviderStub:
    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        return DecisionProviderResult(
            provider_name="codex",
            action=actions[0],
            reasoning="Low confidence event choice.",
            expected_outcome=ExecutionExpectation(
                next_screen="event",
                change_summary="screen should advance",
                verification_hint="event_progress",
            ),
            confidence=0.32,
        )

    def current_run_intent(self):
        return None


class _NoProgressProviderStub:
    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        return DecisionProviderResult(
            provider_name="heuristic",
            action=GameAction(ActionKind.NAVIGATE, "Single Play"),
            reasoning="No progress stub.",
            expected_outcome=ExecutionExpectation(
                next_screen="mode_select",
                change_summary="menu should advance",
                verification_hint="menu_progress",
            ),
        )

    def current_run_intent(self):
        return None


class _LoggerStub:
    def __init__(self) -> None:
        self.logged = []
        self.verifications = []

    def start_run(self) -> str | None:
        return None

    def log_decision(self, state, action, **kwargs) -> None:
        self.logged.append((state.screen.value, action.label, kwargs))

    def log_verification(self, **kwargs) -> None:
        self.verifications.append(kwargs)

    def finish_run(self, summary: RunSummary) -> str:
        del summary
        return "run-1"


class _HarvestAdapterStub(_AdapterStub):
    def __init__(self) -> None:
        super().__init__(stop_after_apply=True)
        self._states = [
            GameState(
                screen=ScreenKind.EVENT,
                act=1,
                floor=7,
                hp=33,
                max_hp=80,
                energy=3,
                gold=99,
                character="Ironclad",
                available_actions=[
                    GameAction(
                        ActionKind.NAVIGATE,
                        "Heal option",
                        {"click_point": (320, 180), "option_text": "Heal 20 HP"},
                        ["heal"],
                    )
                ],
            ),
            GameState(
                screen=ScreenKind.EVENT,
                act=1,
                floor=7,
                hp=33,
                max_hp=80,
                energy=3,
                gold=99,
                character="Ironclad",
                available_actions=[
                    GameAction(
                        ActionKind.NAVIGATE,
                        "Heal option",
                        {"click_point": (320, 180), "option_text": "Heal 20 HP"},
                        ["heal"],
                    )
                ],
            ),
        ]
        self._last_image = Image.new("RGB", (640, 360), color=(25, 40, 60))

    def current_state(self) -> GameState:
        self._last_image = Image.new("RGB", (640, 360), color=(25 + (self._index * 15), 40, 60))
        return super().current_state()

    def last_screenshot(self):
        return self._last_image.copy()


class LiveLoopRunnerTest(unittest.TestCase):
    def test_runner_streams_tick_and_verifies_observed_progress(self) -> None:
        adapter = _AdapterStub()
        logger = _LoggerStub()
        provider = _ProviderStub()
        emitted = []
        Path("tmp").mkdir(exist_ok=True)
        temp_dir = Path("tmp") / "live_runner_test"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            stream_path = temp_dir / "live_loop.jsonl"
            if stream_path.exists():
                stream_path.unlink()
            runner = LiveLoopRunner(adapter=adapter, provider=provider, logger=logger, stream_path=stream_path)

            result = runner.run(max_steps=3, emit=emitted.append)

            self.assertEqual(result.run_id, "run-1")
            self.assertEqual(result.status, "run_over")
            self.assertEqual(result.steps, 1)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0].verification_status, "matched")
            self.assertEqual(emitted[0].state_source, "hybrid")
            self.assertEqual(emitted[0].observed_outcome.state_source, "memory")
            rows = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["action_label"], "End turn")
            self.assertEqual(rows[0]["verification_status"], "matched")
            self.assertEqual(rows[0]["state_source"], "hybrid")
            self.assertEqual(rows[0]["observed_outcome"]["state_source"], "memory")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_runner_stops_when_same_unknown_tick_repeats(self) -> None:
        adapter = _AdapterStub(stop_after_apply=False)
        logger = _LoggerStub()
        provider = _NoProgressProviderStub()
        runner = LiveLoopRunner(adapter=adapter, provider=provider, logger=logger, stream_path=None, stuck_repeat_limit=3)

        result = runner.run(max_steps=10)

        self.assertEqual(result.status, "stuck")
        self.assertEqual(result.steps, 3)

    def test_runner_harvests_low_confidence_case_and_saves_assets(self) -> None:
        adapter = _HarvestAdapterStub()
        provider = _LowConfidenceProviderStub()
        Path("tmp").mkdir(exist_ok=True)
        db_path = Path("tmp") / "live_runner_harvest.sqlite3"
        harvest_dir = Path("tmp") / "live_runner_harvest_assets"
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass
        shutil.rmtree(harvest_dir, ignore_errors=True)
        logger = RunLogger(db_path)
        logger.init_db()
        runner = LiveLoopRunner(
            adapter=adapter,
            provider=provider,
            logger=logger,
            harvest_dir=harvest_dir,
            harvest_confidence_threshold=0.55,
        )

        result = runner.run(max_steps=2)

        self.assertEqual(result.steps, 1)
        cases = logger.pending_harvest_cases(limit=10)
        self.assertEqual(len(cases), 1)
        self.assertIn("confidence:0.32", cases[0]["trigger"])
        asset_paths = [Path(asset["path"]) for asset in cases[0]["assets"]]
        self.assertTrue(any(path.name == "before.png" for path in asset_paths))
        self.assertTrue(any(path.name == "after_focus.png" for path in asset_paths))
        for path in asset_paths:
            self.assertTrue(path.exists())

    def test_battle_same_screen_does_not_match_without_energy_or_screen_change(self) -> None:
        runner = LiveLoopRunner(adapter=_AdapterStub(), provider=_ProviderStub(), logger=_LoggerStub())
        before_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            gold=432,
            character="Ironclad",
            available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
        )
        expected = ExecutionExpectation(
            next_screen="battle",
            change_summary="card should resolve or targeting state should change",
            verification_hint="battle_card_progress",
        )
        observed = runner._observation_from_state(
            GameState(
                screen=ScreenKind.BATTLE,
                act=1,
                floor=4,
                hp=43,
                max_hp=80,
                energy=2,
                gold=432,
                character="Ironclad",
                available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
            )
        )

        verification = runner._verification_status(before_state, ActionKind.PLAY_CARD, expected, observed)

        self.assertEqual(verification, "unknown")

    def test_battle_play_card_matches_when_turn_advances_on_same_screen(self) -> None:
        runner = LiveLoopRunner(adapter=_AdapterStub(), provider=_ProviderStub(), logger=_LoggerStub())
        before_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            gold=432,
            character="Ironclad",
            block=5,
            available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
        )
        expected = ExecutionExpectation(
            next_screen="battle",
            change_summary="card should resolve or targeting state should change",
            verification_hint="battle_card_progress",
        )
        observed = runner._observation_from_state(
            GameState(
                screen=ScreenKind.BATTLE,
                act=1,
                floor=4,
                hp=43,
                max_hp=80,
                energy=3,
                max_energy=3,
                gold=432,
                block=0,
                character="Ironclad",
                available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
            )
        )

        verification = runner._verification_status(before_state, ActionKind.PLAY_CARD, expected, observed)

        self.assertEqual(verification, "matched")


if __name__ == "__main__":
    unittest.main()
