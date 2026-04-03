from __future__ import annotations

import json
from types import SimpleNamespace
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sts_bot.decision_provider import CodexDecisionProvider, HeuristicDecisionProvider
from sts_bot.models import ActionKind, GameAction, GameState, ScreenKind


def _state(screen: ScreenKind = ScreenKind.REWARD_CARDS) -> GameState:
    return GameState(
        screen=screen,
        act=1,
        floor=3,
        hp=45,
        max_hp=80,
        energy=3,
        gold=88,
        character="Ironclad",
    )


class HeuristicDecisionProviderTest(unittest.TestCase):
    def test_decide_returns_reasoning_and_expected_outcome(self) -> None:
        provider = HeuristicDecisionProvider()
        state = _state(ScreenKind.REWARD_CARDS)
        actions = [
            GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block", "draw"]),
            GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
        ]

        result = provider.decide(state, actions)

        self.assertEqual(result.provider_name, "heuristic")
        self.assertEqual(result.action.label, "Pick Shrug It Off")
        self.assertIn("Pick Shrug It Off", result.reasoning)
        self.assertIsNotNone(result.expected_outcome)
        self.assertEqual(result.expected_outcome.verification_hint, "reward_progress")


class CodexDecisionProviderTest(unittest.TestCase):
    @patch.object(CodexDecisionProvider, "_invoke_codex")
    def test_codex_result_overrides_fallback_when_action_is_valid(self, mock_invoke) -> None:
        Path("tmp").mkdir(exist_ok=True)
        temp_dir = tempfile.mkdtemp(dir=Path("tmp"))
        try:
            mock_invoke.return_value = {
                "action_label": "Pick Shrug It Off",
                "reasoning": "Block plan is strongest here.",
                "confidence": 0.81,
                "expected_next_screen": "continue",
                "expected_change_summary": "reward should advance",
                "verification_hint": "reward_progress",
            }
            provider = CodexDecisionProvider(workspace_dir=Path(temp_dir))
            state = _state(ScreenKind.REWARD_CARDS)
            actions = [
                GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block"]),
                GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
            ]

            result = provider.decide(state, actions)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(result.provider_name, "codex")
        self.assertEqual(result.action.label, "Pick Shrug It Off")
        self.assertEqual(result.expected_outcome.next_screen, "continue")
        self.assertIn("Block plan", result.reasoning)

    @patch.object(CodexDecisionProvider, "_invoke_codex")
    def test_codex_failure_falls_back_to_heuristic(self, mock_invoke) -> None:
        mock_invoke.side_effect = RuntimeError("login required")
        provider = CodexDecisionProvider(workspace_dir=Path.cwd())
        state = _state(ScreenKind.REWARD_CARDS)
        actions = [
            GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block"]),
            GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
        ]

        result = provider.decide(state, actions)

        self.assertEqual(result.provider_name, "heuristic")
        self.assertIn("codex_fallback", result.reasoning)
        self.assertIn("codex_fallback=RuntimeError:login required", result.fallback_note)

    @patch("sts_bot.decision_provider.subprocess.run")
    def test_invoke_codex_uses_utf8_decode_settings(self, mock_run) -> None:
        Path("tmp").mkdir(exist_ok=True)
        fixed_temp = Path("tmp") / "decision_provider_invoke"
        fixed_temp.mkdir(parents=True, exist_ok=True)

        class _FixedTempDir:
            def __init__(self, path: Path) -> None:
                self.path = path

            def __enter__(self) -> str:
                return str(self.path)

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        def _fake_run(command, **kwargs):
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps(
                    {
                        "action_label": "Pick Shrug It Off",
                        "reasoning": "utf8-safe",
                        "confidence": 0.75,
                        "expected_next_screen": "continue",
                        "expected_change_summary": "reward should advance",
                        "verification_hint": "reward_progress",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _fake_run
        provider = CodexDecisionProvider(workspace_dir=Path("tmp"))
        state = _state(ScreenKind.REWARD_CARDS)
        actions = [
            GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block"]),
            GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
        ]
        fallback_result = provider.fallback.decide(state, actions)

        with patch("sts_bot.decision_provider.tempfile.TemporaryDirectory", return_value=_FixedTempDir(fixed_temp)):
            payload = provider._invoke_codex(state, actions, fallback_result)

        self.assertEqual(payload["action_label"], "Pick Shrug It Off")
        self.assertEqual(mock_run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(mock_run.call_args.kwargs["errors"], "replace")

    @patch("sts_bot.decision_provider.subprocess.run")
    def test_invoke_codex_prompt_includes_choice_context_snapshot(self, mock_run) -> None:
        Path("tmp").mkdir(exist_ok=True)
        fixed_temp = Path("tmp") / "decision_provider_choice_context"
        fixed_temp.mkdir(parents=True, exist_ok=True)
        captured = {}

        class _FixedTempDir:
            def __init__(self, path: Path) -> None:
                self.path = path

            def __enter__(self) -> str:
                return str(self.path)

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        def _fake_run(command, **kwargs):
            captured["prompt"] = command[-1]
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps(
                    {
                        "action_label": "Heal option",
                        "reasoning": "Use the healing line.",
                        "confidence": 0.77,
                        "expected_next_screen": "continue",
                        "expected_change_summary": "event should advance",
                        "verification_hint": "event_progress",
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _fake_run
        provider = CodexDecisionProvider(workspace_dir=Path("tmp"))
        state = GameState(
            screen=ScreenKind.EVENT,
            act=1,
            floor=5,
            hp=33,
            max_hp=80,
            energy=0,
            gold=99,
            character="Ironclad",
        )
        actions = [
            GameAction(ActionKind.NAVIGATE, "Heal option", {"option_text": "Heal 20 HP", "source": "memory"}, ["heal"]),
            GameAction(ActionKind.NAVIGATE, "Gold option", {"option_text": "Lose 6 HP. Gain 100 Gold.", "source": "ocr"}, ["gold", "hp_cost"]),
        ]
        fallback_result = provider.fallback.decide(state, actions)

        with patch("sts_bot.decision_provider.tempfile.TemporaryDirectory", return_value=_FixedTempDir(fixed_temp)):
            provider._invoke_codex(state, actions, fallback_result)

        prompt = str(captured["prompt"])
        self.assertIn("\"choice_context\"", prompt)
        self.assertIn("\"domain\": \"event\"", prompt)
        self.assertIn("\"option_source\": \"hybrid\"", prompt)
        self.assertIn("\"source\": \"memory\"", prompt)


if __name__ == "__main__":
    unittest.main()
