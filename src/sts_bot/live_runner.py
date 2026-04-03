from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from sts_bot.adapters.windows_stub import WindowsStsAdapter
from sts_bot.decision_provider import DecisionProvider
from sts_bot.kb_learning import CodexKBLearner
from sts_bot.logging import RunLogger
from sts_bot.models import (
    ActionKind,
    ExecutionExpectation,
    ExecutionObservation,
    LiveLoopTick,
    ScreenKind,
)
from sts_bot.policy import attach_run_intent


@dataclass(slots=True)
class LiveLoopResult:
    run_id: str
    steps: int
    finished: bool
    status: str
    floor: int
    screen: str


class LiveLoopRunner:
    def __init__(
        self,
        *,
        adapter: WindowsStsAdapter,
        provider: DecisionProvider,
        logger: RunLogger,
        stream_path: Path | None = None,
        stable_attempts: int = 3,
        stable_delay_seconds: float = 0.18,
        actionable_retries: int = 8,
        actionable_delay_seconds: float = 0.30,
        stuck_repeat_limit: int = 3,
        harvest_dir: Path | None = None,
        harvest_confidence_threshold: float | None = 0.55,
        learner: CodexKBLearner | None = None,
    ) -> None:
        self.adapter = adapter
        self.provider = provider
        self.logger = logger
        self.stream_path = stream_path
        self.stable_attempts = stable_attempts
        self.stable_delay_seconds = stable_delay_seconds
        self.actionable_retries = actionable_retries
        self.actionable_delay_seconds = actionable_delay_seconds
        self.stuck_repeat_limit = max(2, stuck_repeat_limit)
        self.harvest_dir = harvest_dir
        self.harvest_confidence_threshold = harvest_confidence_threshold
        self.learner = learner

    def run(
        self,
        *,
        max_steps: int,
        max_seconds: float | None = None,
        emit: Callable[[LiveLoopTick], None] | None = None,
    ) -> LiveLoopResult:
        self.adapter.start_run(focus=False)
        self.logger.start_run()
        started_at = time.time()
        steps = 0
        status = "running"
        last_state = self.adapter.current_state()
        repeated_signature: tuple[str, str, str] | None = None
        repeated_count = 0

        while True:
            tick_started_at = time.perf_counter()
            if self.adapter.is_run_over():
                status = "run_over"
                break
            if steps >= max_steps:
                status = "max_steps"
                break
            if max_seconds is not None and (time.time() - started_at) >= max_seconds:
                status = "max_seconds"
                break

            observe_started_at = time.perf_counter()
            state, actions = self._await_actionable_state()
            observe_ms = round((time.perf_counter() - observe_started_at) * 1000)
            last_state = state
            if not actions:
                status = "no_actions"
                break

            decide_started_at = time.perf_counter()
            decision = self.provider.decide(state, actions)
            decide_ms = round((time.perf_counter() - decide_started_at) * 1000)
            before_image = self._current_screenshot()
            current_intent = self.provider.current_run_intent()
            state_for_log = attach_run_intent(state, current_intent)
            provider_reasoning = decision.reasoning
            if decision.fallback_note:
                provider_reasoning = (
                    f"{provider_reasoning} | {decision.fallback_note}"
                    if provider_reasoning
                    else decision.fallback_note
                )
            self.logger.log_decision(
                state_for_log,
                decision.action,
                evaluations=decision.evaluations,
                provider_name=decision.provider_name,
                expected_outcome=decision.expected_outcome,
                provider_reasoning=provider_reasoning,
            )
            act_started_at = time.perf_counter()
            self.adapter.apply_action(decision.action)
            act_ms = round((time.perf_counter() - act_started_at) * 1000)
            verify_started_at = time.perf_counter()
            observed_state = self._stable_state()
            after_image = self._current_screenshot()
            observed = self._observation_from_state(observed_state)
            if decision.fallback_note:
                observed.note = (
                    f"{observed.note} | {decision.fallback_note}".strip(" |")
                    if observed.note
                    else decision.fallback_note
                )
            verification = self._verification_status(state, decision.action.kind, decision.expected_outcome, observed)
            verify_ms = round((time.perf_counter() - verify_started_at) * 1000)
            self.logger.log_verification(observed_outcome=observed, verification_status=verification)
            harvest_trigger = self._harvest_trigger(verification_status=verification, confidence=decision.confidence)
            if harvest_trigger is not None and hasattr(self.logger, "log_harvest_case"):
                harvest_id = self.logger.log_harvest_case(
                    state=state_for_log,
                    trigger=harvest_trigger,
                    verification_status=verification,
                    confidence=decision.confidence,
                )
                if self.harvest_dir is not None:
                    asset_root = self._save_harvest_assets(
                        harvest_id=harvest_id,
                        state=state,
                        action=decision.action,
                        before_image=before_image,
                        after_image=after_image,
                    )
                    self.logger.update_harvest_case(harvest_id, asset_root=str(asset_root))
                if self.learner is not None:
                    try:
                        self.learner.maybe_learn(self.logger)
                    except Exception:
                        pass
            tick = LiveLoopTick(
                step_index=steps,
                screen=state.screen.value,
                floor=state.floor,
                hp=state.hp,
                max_hp=state.max_hp,
                energy=state.energy,
                gold=state.gold,
                action_label=decision.action.label,
                provider_name=decision.provider_name,
                max_energy=state.max_energy,
                block=state.block,
                reasoning=decision.reasoning,
                expected_outcome=decision.expected_outcome,
                observed_outcome=observed,
                verification_status=verification,
                state_source=state.state_source.value,
                state_metric_sources=dict(state.metric_sources),
                fallback_note=decision.fallback_note,
                phase_timings_ms={
                    "observe": observe_ms,
                    "decide": decide_ms,
                    "act": act_ms,
                    "verify": verify_ms,
                    "tick": round((time.perf_counter() - tick_started_at) * 1000),
                },
            )
            if self.stream_path is not None:
                append_live_tick_jsonl(self.stream_path, tick)
            if emit is not None:
                emit(tick)
            steps += 1
            last_state = observed_state
            signature = (tick.screen, tick.action_label, tick.observed_outcome.screen if tick.observed_outcome is not None else "")
            if (
                tick.verification_status in {"unknown", "partial", "mismatch"}
                and tick.observed_outcome is not None
                and tick.screen == tick.observed_outcome.screen
            ):
                if signature == repeated_signature:
                    repeated_count += 1
                else:
                    repeated_signature = signature
                    repeated_count = 1
                if repeated_count >= self.stuck_repeat_limit:
                    status = "stuck"
                    break
            else:
                repeated_signature = None
                repeated_count = 0

        summary = self.adapter.run_summary()
        run_id = self.logger.finish_run(summary)
        return LiveLoopResult(
            run_id=run_id,
            steps=steps,
            finished=status == "run_over",
            status=status,
            floor=max(summary.floor_reached, last_state.floor),
            screen=last_state.screen.value,
        )

    def _await_actionable_state(self):
        state = self.adapter.current_state()
        actions = self.adapter.available_actions()
        if actions:
            return state, actions
        for _ in range(max(0, self.actionable_retries)):
            time.sleep(self.actionable_delay_seconds)
            state = self.adapter.current_state()
            actions = self.adapter.available_actions()
            if actions:
                return state, actions
        return state, actions

    def _stable_state(self):
        state = self.adapter.current_state()
        best_state = state
        for _ in range(max(0, self.stable_attempts - 1)):
            if state.screen != ScreenKind.UNKNOWN and state.available_actions:
                return state
            time.sleep(self.stable_delay_seconds)
            state = self.adapter.current_state()
            if state.screen != ScreenKind.UNKNOWN:
                best_state = state
            if state.available_actions:
                return state
        return best_state

    def _observation_from_state(self, state) -> ExecutionObservation:
        note_parts: list[str] = []
        if state.state_source is not None:
            note_parts.append(f"source={state.state_source.value}")
        return ExecutionObservation(
            screen=state.screen.value,
            hp=state.hp,
            max_hp=state.max_hp,
            energy=state.energy,
            gold=state.gold,
            floor=state.floor,
            max_energy=state.max_energy,
            block=state.block,
            actions=[action.label for action in state.available_actions],
            state_source=state.state_source.value,
            metric_sources=dict(state.metric_sources),
            note=" | ".join(note_parts),
        )

    def _verification_status(
        self,
        before_state,
        action_kind: ActionKind,
        expected: ExecutionExpectation | None,
        observed: ExecutionObservation,
    ) -> str:
        if (
            expected is not None
            and expected.next_screen
            and observed.screen == expected.next_screen
            and expected.next_screen != before_state.screen.value
        ):
            return "matched"
        if before_state.screen.value != observed.screen:
            return "matched"
        if action_kind == ActionKind.END_TURN and observed.energy != before_state.energy:
            return "matched"
        if action_kind == ActionKind.PLAY_CARD:
            if observed.energy < before_state.energy:
                return "matched"
            if before_state.screen == ScreenKind.BATTLE and observed.screen == ScreenKind.BATTLE.value:
                if observed.energy > before_state.energy:
                    return "matched"
                if observed.block != before_state.block:
                    return "matched"
        if action_kind == ActionKind.PICK_CARD and before_state.screen.value in {
            ScreenKind.REWARD_MENU.value,
            ScreenKind.REWARD_CARDS.value,
            ScreenKind.REWARD_GOLD_ONLY.value,
        }:
            return "partial" if observed.screen == before_state.screen.value else "matched"
        if observed.floor != before_state.floor or observed.gold != before_state.gold or observed.hp != before_state.hp:
            return "partial"
        return "unknown"

    def _harvest_trigger(self, *, verification_status: str, confidence: float | None) -> str | None:
        triggers: list[str] = []
        if verification_status in {"unknown", "partial", "mismatch"}:
            triggers.append(f"verification:{verification_status}")
        if (
            confidence is not None
            and self.harvest_confidence_threshold is not None
            and confidence < self.harvest_confidence_threshold
        ):
            triggers.append(f"confidence:{confidence:.2f}")
        return ",".join(triggers) if triggers else None

    def _current_screenshot(self):
        getter = getattr(self.adapter, "last_screenshot", None)
        if callable(getter):
            try:
                image = getter()
            except Exception:
                image = None
            if image is not None:
                return image
        getter = getattr(self.adapter, "capture_image", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    def _save_harvest_assets(
        self,
        *,
        harvest_id: int,
        state,
        action,
        before_image,
        after_image,
    ) -> Path:
        if self.harvest_dir is None:
            raise RuntimeError("harvest_dir is not configured.")
        root = self.harvest_dir / f"run_{state.floor:03d}" / f"harvest_{harvest_id:05d}"
        root.mkdir(parents=True, exist_ok=True)
        if before_image is not None:
            before_path = root / "before.png"
            before_image.save(before_path)
            self.logger.log_harvest_asset(
                harvest_id=harvest_id,
                asset_kind="before",
                path=str(before_path),
                metadata={"screen": state.screen.value},
            )
        if after_image is not None:
            after_path = root / "after.png"
            after_image.save(after_path)
            self.logger.log_harvest_asset(
                harvest_id=harvest_id,
                asset_kind="after",
                path=str(after_path),
                metadata={},
            )
        click_point = action.payload.get("click_point")
        if (
            isinstance(click_point, (list, tuple))
            and len(click_point) == 2
            and before_image is not None
            and after_image is not None
        ):
            try:
                point = (int(click_point[0]), int(click_point[1]))
            except (TypeError, ValueError):
                point = None
            if point is not None:
                before_focus = before_image.crop(self._focus_box(point, before_image.size))
                after_focus = after_image.crop(self._focus_box(point, after_image.size))
                before_focus_path = root / "before_focus.png"
                after_focus_path = root / "after_focus.png"
                before_focus.save(before_focus_path)
                after_focus.save(after_focus_path)
                focus_metadata = {"point": [point[0], point[1]], "action_label": action.label}
                self.logger.log_harvest_asset(
                    harvest_id=harvest_id,
                    asset_kind="before_focus",
                    path=str(before_focus_path),
                    metadata=focus_metadata,
                )
                self.logger.log_harvest_asset(
                    harvest_id=harvest_id,
                    asset_kind="after_focus",
                    path=str(after_focus_path),
                    metadata=focus_metadata,
                )
        metadata_path = root / "case.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "screen": state.screen.value,
                    "floor": state.floor,
                    "action_label": action.label,
                    "action_payload": action.payload,
                    "action_tags": action.tags,
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        self.logger.log_harvest_asset(
            harvest_id=harvest_id,
            asset_kind="case_json",
            path=str(metadata_path),
            metadata={},
        )
        return root

    @staticmethod
    def _focus_box(point: tuple[int, int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
        width, height = image_size
        clamped_x = min(max(0, point[0]), max(0, width - 1))
        clamped_y = min(max(0, point[1]), max(0, height - 1))
        left = max(0, clamped_x - 220)
        top = max(0, clamped_y - 120)
        right = max(left + 1, min(width, clamped_x + 220))
        bottom = max(top + 1, min(height, clamped_y + 120))
        return (left, top, right, bottom)


def append_live_tick_jsonl(path: Path, tick: LiveLoopTick) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "step_index": tick.step_index,
        "screen": tick.screen,
        "floor": tick.floor,
        "hp": tick.hp,
        "max_hp": tick.max_hp,
        "energy": tick.energy,
        "gold": tick.gold,
        "action_label": tick.action_label,
        "provider_name": tick.provider_name,
        "reasoning": tick.reasoning,
        "state_source": tick.state_source,
        "state_metric_sources": dict(tick.state_metric_sources),
        "fallback_note": tick.fallback_note,
        "expected_outcome": asdict(tick.expected_outcome) if tick.expected_outcome is not None else None,
        "observed_outcome": asdict(tick.observed_outcome) if tick.observed_outcome is not None else None,
        "verification_status": tick.verification_status,
        "phase_timings_ms": dict(tick.phase_timings_ms),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True))
        handle.write("\n")
