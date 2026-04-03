from __future__ import annotations

import unittest
from unittest.mock import patch

from sts_bot.engine import AutoplayEngine
from sts_bot.models import ActionKind, GameAction, GameState, RunSummary, ScreenKind


class _AdapterStub:
    def __init__(self) -> None:
        self._state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=1,
            hp=80,
            max_hp=80,
            energy=3,
            gold=99,
            character="Ironclad",
            available_actions=[],
        )
        self._available_actions_calls = 0
        self.applied_labels: list[str] = []
        self.started = False

    def start_run(self) -> None:
        self.started = True

    def is_run_over(self) -> bool:
        return len(self.applied_labels) >= 1

    def current_state(self) -> GameState:
        return self._state

    def available_actions(self) -> list[GameAction]:
        self._available_actions_calls += 1
        if self._available_actions_calls <= 2:
            return []
        return [GameAction(kind=ActionKind.END_TURN, label="End turn")]

    def apply_action(self, action: GameAction) -> None:
        self.applied_labels.append(action.label)

    def run_summary(self) -> RunSummary:
        return RunSummary(
            character="Ironclad",
            won=False,
            act_reached=1,
            floor_reached=1,
            score=0,
            deck=[],
            relics=[],
            picked_cards=[],
            skipped_cards=[],
            path=[],
            strategy_tags=[],
        )


class _PolicyStub:
    def choose_action(self, _state: GameState, actions: list[GameAction]) -> GameAction:
        return actions[0]


class _LoggerStub:
    def __init__(self) -> None:
        self.logged_labels: list[str] = []

    def start_run(self) -> None:
        return None

    def log_decision(self, _state: GameState, action: GameAction, evaluations=None) -> None:
        del evaluations
        self.logged_labels.append(action.label)

    def finish_run(self, _summary: RunSummary) -> str:
        return "run-1"


class AutoplayEngineTest(unittest.TestCase):
    @patch("sts_bot.engine.time.sleep", return_value=None)
    def test_run_episode_retries_briefly_when_no_actions_are_available(self, _mock_sleep) -> None:
        adapter = _AdapterStub()
        policy = _PolicyStub()
        logger = _LoggerStub()
        engine = AutoplayEngine(adapter=adapter, policy=policy, logger=logger)

        result = engine.run_episode(max_steps=5)

        self.assertTrue(adapter.started)
        self.assertEqual(adapter.applied_labels, ["End turn"])
        self.assertEqual(logger.logged_labels, ["End turn"])
        self.assertEqual(result.run_id, "run-1")


if __name__ == "__main__":
    unittest.main()
