from __future__ import annotations

import json
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from sts_bot.models import (
    ActionKind,
    DecisionProviderResult,
    ExecutionExpectation,
    GameAction,
    GameState,
    ScreenKind,
)
from sts_bot.policy import (
    HeuristicPolicy,
    attach_run_intent,
    build_choice_context_snapshot,
    build_state_snapshot,
    summarize_choice,
)


class DecisionProvider(ABC):
    @abstractmethod
    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        """Choose an action and explain why."""

    def current_run_intent(self):
        return None


class HeuristicDecisionProvider(DecisionProvider):
    def __init__(self, policy: HeuristicPolicy | None = None) -> None:
        self.policy = policy or HeuristicPolicy()

    def current_run_intent(self):
        return self.policy.current_run_intent()

    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        evaluations = self.policy.evaluate_actions(state, actions)
        best = max(evaluations, key=lambda item: (item.score, -len(item.action_label)))
        action = next(item for item in actions if item.label == best.action_label)
        enriched_state = attach_run_intent(state, self.policy.current_run_intent())
        reasoning = summarize_choice(enriched_state, action, evaluations)
        expected = _default_expectation(state, action)
        return DecisionProviderResult(
            provider_name="heuristic",
            action=action,
            evaluations=evaluations,
            reasoning=reasoning,
            expected_outcome=expected,
        )


class CodexDecisionProvider(DecisionProvider):
    def __init__(
        self,
        *,
        fallback: HeuristicDecisionProvider | None = None,
        model: str = "gpt-5.4",
        timeout_seconds: float = 45.0,
        workspace_dir: Path | None = None,
        strategic_screens: set[str] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.fallback = fallback or HeuristicDecisionProvider()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.workspace_dir = workspace_dir
        self.progress_callback = progress_callback
        self.strategic_screens = strategic_screens or {
            ScreenKind.MAP.value,
            ScreenKind.EVENT.value,
            ScreenKind.REWARD_MENU.value,
            ScreenKind.REWARD_CARDS.value,
            ScreenKind.REWARD_RELIC.value,
            ScreenKind.REWARD_POTION.value,
            ScreenKind.REWARD_GOLD_ONLY.value,
            ScreenKind.SHOP.value,
            ScreenKind.BOSS_RELIC.value,
            ScreenKind.CONTINUE.value,
            ScreenKind.GAME_OVER.value,
            ScreenKind.NEOW_CHOICE.value,
            ScreenKind.CARD_GRID.value,
        }

    def current_run_intent(self):
        return self.fallback.current_run_intent()

    def decide(self, state: GameState, actions: list[GameAction]) -> DecisionProviderResult:
        fallback_result = self.fallback.decide(state, actions)
        if state.screen.value not in self.strategic_screens:
            return fallback_result

        self._emit_progress(
            f"codex_decide_start screen={state.screen.value} actions={len(actions)} timeout={self.timeout_seconds:.1f}s"
        )
        try:
            response = self._invoke_codex(state, actions, fallback_result)
        except Exception as exc:
            fallback_note = f"codex_fallback={type(exc).__name__}:{exc}"
            self._emit_progress(
                f"codex_decide_error screen={state.screen.value} error={type(exc).__name__}:{exc}"
            )
            fallback_result.reasoning = (
                f"{fallback_result.reasoning} | {fallback_note}"
            )
            fallback_result.fallback_note = fallback_note
            return fallback_result

        selected_label = str(response.get("action_label") or "").strip()
        action = next((item for item in actions if item.label == selected_label), None)
        if action is None:
            fallback_note = f"codex_invalid_action={selected_label or 'missing'}"
            self._emit_progress(
                f"codex_decide_invalid screen={state.screen.value} action_label={selected_label or 'missing'}"
            )
            fallback_result.reasoning = (
                f"{fallback_result.reasoning} | {fallback_note}"
            )
            fallback_result.fallback_note = fallback_note
            return fallback_result

        expected = ExecutionExpectation(
            next_screen=_coerce_optional_text(response.get("expected_next_screen")),
            change_summary=_coerce_optional_text(response.get("expected_change_summary")) or "",
            verification_hint=_coerce_optional_text(response.get("verification_hint")) or "",
        )
        reasoning = _coerce_optional_text(response.get("reasoning")) or fallback_result.reasoning
        confidence = _coerce_optional_float(response.get("confidence"))
        self._emit_progress(
            f"codex_decide_done screen={state.screen.value} action={action.label} confidence={confidence if confidence is not None else 'n/a'}"
        )
        return DecisionProviderResult(
            provider_name="codex",
            action=action,
            evaluations=fallback_result.evaluations,
            reasoning=reasoning,
            expected_outcome=expected,
            confidence=confidence,
        )

    def _invoke_codex(
        self,
        state: GameState,
        actions: list[GameAction],
        fallback_result: DecisionProviderResult,
    ) -> dict[str, object]:
        payload = {
            "state": build_state_snapshot(attach_run_intent(state, self.fallback.current_run_intent())),
            "choice_context": build_choice_context_snapshot(
                attach_run_intent(state, self.fallback.current_run_intent()),
                actions,
            ),
            "actions": [
                {
                    "label": action.label,
                    "kind": action.kind.value,
                    "tags": action.tags,
                    "payload": action.payload,
                }
                for action in actions
            ],
            "fallback_choice": {
                "action_label": fallback_result.action.label,
                "reasoning": fallback_result.reasoning,
            },
        }
        schema = {
            "type": "object",
            "properties": {
                "action_label": {"type": "string"},
                "reasoning": {"type": "string"},
                "confidence": {"type": "number"},
                "expected_next_screen": {"type": ["string", "null"]},
                "expected_change_summary": {"type": "string"},
                "verification_hint": {"type": "string"},
            },
            "required": ["action_label", "reasoning", "confidence", "expected_next_screen", "expected_change_summary", "verification_hint"],
            "additionalProperties": False,
        }
        prompt = (
            "You are selecting the next Slay the Spire action.\n"
            "Choose exactly one action_label from the provided actions.\n"
            "Prefer long-term deck direction and immediate survival.\n"
            "Return strict JSON only.\n\n"
            f"{json.dumps(payload, ensure_ascii=True, indent=2)}"
        )
        temp_root = self.workspace_dir if self.workspace_dir is not None and self.workspace_dir.exists() else None
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "decision_schema.json"
            output_path = temp_path / "decision_output.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=True), encoding="utf-8")
            command = [
                "cmd",
                "/c",
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--model",
                self.model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.workspace_dir is not None:
                command.extend(["--cd", str(self.workspace_dir)])
            command.append(prompt)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, self.timeout_seconds),
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(stderr or f"codex exec failed with exit code {completed.returncode}")
            raw = output_path.read_text(encoding="utf-8").strip()
            return json.loads(raw)

    def _emit_progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)


def _default_expectation(state: GameState, action: GameAction) -> ExecutionExpectation:
    if action.kind == ActionKind.END_TURN:
        return ExecutionExpectation(
            next_screen=ScreenKind.BATTLE.value,
            change_summary="turn should advance or enemy turn should begin",
            verification_hint="battle_turn_progress",
        )
    if action.kind == ActionKind.PLAY_CARD:
        return ExecutionExpectation(
            next_screen=ScreenKind.BATTLE.value,
            change_summary="card should resolve or targeting state should change",
            verification_hint="battle_card_progress",
        )
    if action.kind == ActionKind.PICK_CARD:
        if state.screen == ScreenKind.CARD_GRID:
            return ExecutionExpectation(
                next_screen=None,
                change_summary="card transform selection should resolve or confirm should appear",
                verification_hint="card_grid_progress",
            )
        return ExecutionExpectation(
            next_screen=None if state.screen == ScreenKind.REWARD_CARDS else ScreenKind.CONTINUE.value,
            change_summary="reward screen should advance",
            verification_hint="reward_progress",
        )
    if action.kind == ActionKind.TAKE_RELIC:
        return ExecutionExpectation(
            next_screen=None,
            change_summary="relic reward choice should resolve",
            verification_hint="relic_reward_progress",
        )
    if action.kind == ActionKind.TAKE_POTION:
        return ExecutionExpectation(
            next_screen=None,
            change_summary="potion reward choice should resolve",
            verification_hint="potion_reward_progress",
        )
    if action.kind == ActionKind.BUY:
        return ExecutionExpectation(
            next_screen=None,
            change_summary="shop purchase or shop service should resolve",
            verification_hint="shop_progress",
        )
    if action.kind == ActionKind.CHOOSE_PATH:
        return ExecutionExpectation(
            next_screen=None,
            change_summary="map route should advance to the next node",
            verification_hint="map_progress",
        )
    if state.screen == ScreenKind.GAME_OVER:
        return ExecutionExpectation(
            next_screen=ScreenKind.MENU.value,
            change_summary="should return to the title or main menu",
            verification_hint="restart_to_menu",
        )
    return ExecutionExpectation(
        next_screen=None,
        change_summary=f"{action.label} should make forward progress",
        verification_hint="generic_progress",
    )


def _coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
