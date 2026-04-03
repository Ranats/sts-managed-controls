from __future__ import annotations

from difflib import get_close_matches
from dataclasses import dataclass, replace
from pathlib import Path
import time

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageStat

from sts_bot.adapters.base import GameAdapter
from sts_bot.capture_backends import CaptureDiagnostics
from sts_bot.config import ActionDefinition, CalibrationProfile, Rect, TextRegionDefinition
from sts_bot.gamepad import press_xbox_sequence
from sts_bot.input_backends import InputBackend, LegacyForegroundInputBackend, WindowMessageInputBackend
from sts_bot.io_runtime import CapabilityReport, IoRuntime, create_runtime
from sts_bot.knowledge import (
    canonicalize_card_name,
    canonicalize_potion_name,
    canonicalize_relic_name,
    known_potion_names,
    known_relic_names,
    lookup_boss_relic_knowledge,
    lookup_card_knowledge,
    lookup_potion_knowledge,
)
from sts_bot.memory_reader import MemoryFieldResult, MemoryReadSnapshot, ProcessMemoryReader
from sts_bot.managed_probe import ManagedProbeError, ManagedProbeSnapshot, ManagedSnapshotProbe
from sts_bot.models import (
    ActionKind,
    BattleCardObservation,
    BattleTargetKind,
    ChoiceDomain,
    DeckCard,
    EnemyState,
    GameAction,
    GameState,
    RunSummary,
    ScreenKind,
    StateSource,
)
from sts_bot.policy import evaluate_battle_card, infer_run_intent
from sts_bot.vision import TemplateMatch, extract_text, extract_text_fast, match_template, parse_text_value


@dataclass(slots=True)
class CardPlayAttempt:
    backend: str
    played: bool
    reason: str
    visual_score: float = 0.0


class WindowsStsAdapter(GameAdapter):
    """
    Config-driven Windows adapter.

    It relies on a calibration profile that defines:
    - window title
    - anchor templates for screen detection
    - OCR regions for hp / floor / gold / energy
    - clickable actions per screen
    """

    def __init__(self, profile_path: Path, strict_ocr: bool = False) -> None:
        self.profile_path = profile_path
        self.profile = CalibrationProfile.load(profile_path)
        self.strict_ocr = strict_ocr
        self._runtime: IoRuntime | None = None
        self._last_state: GameState | None = None
        self._last_matches: dict[str, TemplateMatch] = {}
        self._started = False
        self._bootstrap_on_start = False
        self._picked_cards: list[str] = []
        self._deck_cards: list[DeckCard] = []
        self._skipped_cards: list[str] = []
        self._path: list[str] = []
        self._relics: list[str] = []
        self._strategy_tags: set[str] = set()
        self._floor_reached = 0
        self._act_reached = 0
        self._won = False
        self._scale_x = 1.0
        self._scale_y = 1.0
        self._last_metrics: dict[str, int | tuple[int, int] | str | None] = {}
        self._last_metric_sources: dict[str, str] = {}
        self._last_ocr_metric_sources: dict[str, str] = {}
        self._last_screenshot: Image.Image | None = None
        self._last_metric_read_at = 0.0
        self._memory_reader: ProcessMemoryReader | None = None
        self._managed_probe: ManagedSnapshotProbe | None = None
        self._last_managed_snapshot: ManagedProbeSnapshot | None = None
        self._last_managed_read_at = 0.0
        self._last_memory_probe_data: dict[str, object] = {"enabled": self.profile.memory_read.enabled, "fields": {}, "values": {}}

    def start_run(self, focus: bool = True) -> None:
        del focus
        if self._runtime is not None:
            self._runtime.close()
        self._runtime = create_runtime(self.profile)
        if self.profile.verbose_diagnostics:
            report = self._runtime.capability_report()
            print(
                f"[sts_bot] hwnd={report.hwnd} title={report.title!r} capture={report.selected_capture_backend} "
                f"input={report.selected_input_backend} bg_capture={report.background_capture_supported} "
                f"bg_input={report.background_input_supported} dry_run={report.dry_run}"
            )
        self._started = True
        self._picked_cards = []
        self._deck_cards = []
        self._skipped_cards = []
        self._path = []
        self._relics = []
        self._strategy_tags = set()
        self._floor_reached = 0
        self._act_reached = 0
        self._won = False
        if self._bootstrap_on_start:
            for action in self.profile.start_actions:
                self._execute_action(action)
            self._bootstrap_on_start = False
        self._last_state = None
        self._last_metric_sources = {}
        self._last_ocr_metric_sources = {}
        self._last_memory_probe_data = {"enabled": self.profile.memory_read.enabled, "fields": {}, "values": {}}
        if self._memory_reader is not None:
            self._memory_reader.close()
            self._memory_reader = None
        self._managed_probe = None
        self._last_managed_snapshot = None
        self._last_managed_read_at = 0.0
        screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
        initial_state = self.inspect_image(screenshot, read_metrics=False)
        if initial_state.screen == ScreenKind.GAME_OVER:
            main_menu_action = next((action for action in initial_state.available_actions if action.label == "Main menu"), None)
            if main_menu_action is not None:
                self.apply_action(main_menu_action, backend=self.profile.scene_input_backends.get(ScreenKind.GAME_OVER.value))
                self._last_state = None

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        if self._memory_reader is not None:
            self._memory_reader.close()
            self._memory_reader = None
        self._managed_probe = None
        self._last_managed_snapshot = None
        self._last_managed_read_at = 0.0
        self._started = False

    def enable_bootstrap_on_start(self) -> None:
        self._bootstrap_on_start = True

    def current_state(self) -> GameState:
        if not self._started:
            raise RuntimeError("Call start_run before querying state.")
        return self._observe_live_state(read_metrics=True)

    def inspect_image_path(self, image_path: Path) -> GameState:
        screenshot = Image.open(image_path)
        return self.inspect_image(screenshot)

    def inspect_image(self, screenshot, *, read_metrics: bool = True) -> GameState:
        self._update_scale(screenshot.size)
        self._last_screenshot = screenshot.copy()
        screen = self._detect_screen(screenshot)
        metrics = self._metrics_for_screen(screenshot, screen, read_metrics=read_metrics)
        if read_metrics:
            self._last_metrics = dict(metrics)
        actions = self._actions_for_screen(screen, screenshot)
        metric_sources = dict(self._last_metric_sources)
        hp_value = metrics.get("hp")
        if isinstance(hp_value, tuple):
            hp, max_hp = hp_value
        else:
            hp, max_hp = int(hp_value or 0), int(hp_value or 0)
        floor = int(metrics.get("floor") or 0)
        act = int(metrics.get("act") or 0) or self._infer_act_from_floor(floor)
        enemies = self._extract_battle_enemies(screenshot, include_hp_text=False) if screen == ScreenKind.BATTLE else []
        if screen == ScreenKind.BATTLE:
            enemies = self._merge_managed_enemy_metrics(enemies)
        managed_snapshot = self._last_managed_snapshot if screen in self._metric_screens() else None
        state = GameState(
            screen=screen,
            act=act,
            floor=floor,
            hp=hp,
            max_hp=max_hp,
            energy=int(metrics.get("energy") or 0),
            max_energy=self._managed_max_energy(managed_snapshot) or int(metrics.get("energy") or 0),
            block=self._managed_player_block(managed_snapshot),
            gold=int(metrics.get("gold") or 0),
            character=self.profile.static_character,
            enemies=enemies,
            deck=[DeckCard(card.name, upgraded=card.upgraded, tags=card.tags[:]) for card in self._deck_cards],
            relics=self._relics[:],
            player_powers=self._managed_power_map(getattr(managed_snapshot, "player_powers", [])),
            hand=[],
            available_actions=actions,
            tags=sorted(self._strategy_tags),
            run_intent=None,
            state_source=self._state_source_from_metric_sources(metric_sources),
            metric_sources=metric_sources,
        )
        self._floor_reached = max(self._floor_reached, state.floor)
        self._act_reached = max(self._act_reached, state.act)
        self._won = screen == ScreenKind.GAME_OVER and floor >= 50
        self._last_state = state
        return replace(state)

    def _merge_managed_enemy_metrics(self, enemies: list[EnemyState]) -> list[EnemyState]:
        if not enemies or self._last_managed_snapshot is None or not self._last_managed_snapshot.enemies:
            return enemies
        merged = [replace(enemy) for enemy in enemies]
        ordered_enemies = sorted(merged, key=lambda enemy: enemy.x)
        managed_enemies = list(self._last_managed_snapshot.enemies)
        if len(managed_enemies) != len(ordered_enemies):
            return merged
        for enemy, managed_enemy in zip(ordered_enemies, managed_enemies):
            enemy.hp = managed_enemy.current_hp
            enemy.max_hp = managed_enemy.max_hp
            enemy.block = managed_enemy.block
            enemy.powers = self._managed_power_map(managed_enemy.powers)
        return merged

    @staticmethod
    def _managed_power_name(type_name: str) -> str:
        name = str(type_name or "").rsplit(".", 1)[-1]
        return name[:-5] if name.endswith("Power") else name

    def _managed_power_map(self, powers: list[ManagedPowerSnapshot] | None) -> dict[str, int]:
        if not powers:
            return {}
        return {
            self._managed_power_name(power.type_name): power.amount
            for power in powers
            if getattr(power, "type_name", "") and isinstance(getattr(power, "amount", None), int)
        }

    @staticmethod
    def _managed_max_energy(snapshot: ManagedProbeSnapshot | None) -> int | None:
        if snapshot is None:
            return None
        value = getattr(snapshot, "max_energy", None)
        return value if isinstance(value, int) and 0 <= value <= 100 else None

    @staticmethod
    def _managed_player_block(snapshot: ManagedProbeSnapshot | None) -> int:
        if snapshot is None:
            return 0
        value = getattr(snapshot, "block", None)
        return value if isinstance(value, int) and 0 <= value <= 999 else 0

    def available_actions(self) -> list[GameAction]:
        if self._last_state is None:
            return self.current_state().available_actions
        return self._last_state.available_actions[:]

    def inject_input(
        self,
        *,
        backend: str | None = None,
        key: str | None = None,
        point: tuple[int, int] | None = None,
        delay_ms: int | None = None,
        hold_ms: int = 40,
        repeat: int = 1,
    ) -> str:
        self._require_runtime()
        selected_delay = self.profile.action_delay_ms if delay_ms is None else delay_ms
        input_backend = self._resolve_input_backend(backend)
        last_backend = input_backend.diagnostics().backend
        for _ in range(max(1, repeat)):
            if key is not None:
                input_backend.key_press(key, hold_ms=hold_ms)
                last_backend = input_backend.diagnostics().backend
                self._action_sleep(selected_delay)
                continue
            if point is None:
                raise ValueError("Either key or point must be provided.")
            if self._last_screenshot is not None:
                self._update_scale(self._last_screenshot.size)
            scaled_point = self._scale_reference_point(point)
            input_backend.click(scaled_point[0], scaled_point[1])
            last_backend = input_backend.diagnostics().backend
            self._action_sleep(selected_delay)
        self._close_temporary_backend(input_backend, backend)
        return last_backend

    def inject_drag(
        self,
        *,
        backend: str | None = None,
        start_point: tuple[int, int],
        end_point: tuple[int, int],
        delay_ms: int | None = None,
        duration_ms: int = 220,
    ) -> str:
        self._require_runtime()
        if self._last_screenshot is not None:
            self._update_scale(self._last_screenshot.size)
        selected_delay = self.profile.action_delay_ms if delay_ms is None else delay_ms
        input_backend = self._resolve_input_backend(backend)
        scaled_start = self._scale_reference_point(start_point)
        scaled_end = self._scale_reference_point(end_point)
        input_backend.drag(scaled_start[0], scaled_start[1], scaled_end[0], scaled_end[1], duration_ms=duration_ms)
        self._action_sleep(selected_delay)
        selected_backend = input_backend.diagnostics().backend
        self._close_temporary_backend(input_backend, backend)
        return selected_backend

    def play_card_slot(self, slot: int, *, backend: str | None = None) -> str:
        attempt = self._attempt_play_card_slot(slot, backend=backend)
        if not attempt.played:
            raise RuntimeError(attempt.reason)
        return attempt.backend

    def _attempt_play_card_slot(
        self,
        slot: int,
        *,
        backend: str | None = None,
        prefer_non_target_only: bool = False,
        reset_gamepad_focus: bool = True,
    ) -> CardPlayAttempt:
        if slot < 1 or slot > 10:
            raise ValueError(f"Unsupported slot index: {slot}")
        self._require_runtime()
        if self._backend_name_for_actions(backend) == "gamepad":
            return self._attempt_play_card_slot_with_gamepad(
                slot,
                prefer_non_target_only=prefer_non_target_only,
                reset_focus=reset_gamepad_focus,
            )
        input_backend = self._resolve_input_backend(backend)
        input_backend.key_press(str(slot), hold_ms=80)
        self._action_sleep(120)
        screenshot = self._capture_window_image()
        selected_screenshot = screenshot.copy()
        drag_start = self._selected_card_drag_origin(screenshot)
        for _ in range(6):
            if self._selection_requires_target(screenshot):
                break
            if drag_start is not None:
                break
            time.sleep(0.08)
            screenshot = self._capture_window_image()
            drag_start = self._selected_card_drag_origin(screenshot)
        self._last_screenshot = screenshot.copy()
        if self._selection_requires_target(screenshot):
            resolution = self._resolve_targeted_card(input_backend, screenshot)
            backend_name = input_backend.diagnostics().backend
            self._close_temporary_backend(input_backend, backend)
            if not resolution.played:
                settled = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                self._last_screenshot = settled.copy()
                delayed_progress = self._battle_progress_made(selected_screenshot, settled)
                if delayed_progress and not self._selection_requires_target(settled) and self._selected_card_drag_origin(settled) is None:
                    return CardPlayAttempt(
                        backend=backend_name,
                        played=True,
                        reason="targeted_card_played_delayed_confirmation",
                        visual_score=max(
                            self._frame_diff_score(selected_screenshot, settled),
                            self._hand_diff_score(selected_screenshot, settled),
                            self._enemy_progress_score(selected_screenshot, settled),
                        ),
                    )
                return CardPlayAttempt(
                    backend=backend_name,
                    played=False,
                    reason=f"{resolution.reason}:slot_{slot}",
                    visual_score=resolution.visual_score,
                )
            return CardPlayAttempt(
                backend=backend_name,
                played=True,
                reason=resolution.reason,
                visual_score=resolution.visual_score,
            )
        if drag_start is None:
            drag_start = self._fallback_selected_card_drag_origin(screenshot)
        if drag_start is None:
            backend_name = input_backend.diagnostics().backend
            self._close_temporary_backend(input_backend, backend)
            return CardPlayAttempt(backend=backend_name, played=False, reason=f"card_not_selectable:slot_{slot}")
        drag_start = drag_start or self._scale_reference_point((985, 377))
        drag_end = self._scale_reference_point((942, 305))
        input_backend.drag(drag_start[0], drag_start[1], drag_end[0], drag_end[1], duration_ms=220)
        self._action_sleep(self.profile.action_delay_ms)
        resolved_screenshot = self._capture_window_image()
        self._last_screenshot = resolved_screenshot.copy()
        backend_name = input_backend.diagnostics().backend
        if self._selection_requires_target(resolved_screenshot) or self._selected_card_drag_origin(resolved_screenshot) is not None:
            self._cancel_card_selection(input_backend)
            self._close_temporary_backend(input_backend, backend)
            return CardPlayAttempt(backend=backend_name, played=False, reason=f"card_selection_stuck:slot_{slot}")
        diff_score = self._frame_diff_score(selected_screenshot, resolved_screenshot)
        hand_diff_score = self._hand_diff_score(selected_screenshot, resolved_screenshot)
        if diff_score < 4.0 and hand_diff_score < 8.0:
            self._close_temporary_backend(input_backend, backend)
            return CardPlayAttempt(
                backend=backend_name,
                played=False,
                reason=f"no_visual_progress:slot_{slot}",
                visual_score=max(diff_score, hand_diff_score),
            )
        self._close_temporary_backend(input_backend, backend)
        return CardPlayAttempt(
            backend=backend_name,
            played=True,
            reason="drag_card_played",
            visual_score=max(diff_score, hand_diff_score),
        )

    def _attempt_play_card_slot_with_gamepad(
        self,
        slot: int,
        *,
        prefer_non_target_only: bool = False,
        reset_focus: bool = True,
    ) -> CardPlayAttempt:
        before = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.06)
        self._last_screenshot = before.copy()
        buttons = (["dpad_left"] * 6 if reset_focus else []) + (["dpad_right"] * max(0, slot - 1)) + ["a"]
        self._press_gamepad_sequence(buttons, hold_ms=80, gap_ms=80)
        after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.06)
        for _ in range(6):
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                break
            time.sleep(0.08)
            refreshed = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
            refreshed_diff = max(
                self._frame_diff_score(after, refreshed),
                self._hand_diff_score(after, refreshed),
            )
            after = refreshed
            if refreshed_diff < 1.0:
                break
        self._last_screenshot = after.copy()
        if self._selection_requires_target(after):
            if prefer_non_target_only:
                self._cancel_card_selection_gamepad()
                after_cancel = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                self._last_screenshot = after_cancel.copy()
                return CardPlayAttempt(
                    backend="gamepad",
                    played=False,
                    reason=f"defer_targeted:slot_{slot}",
                )
            if self._prefer_background_target_resolution():
                background_attempt = self._resolve_targeted_card_with_background_messages(after.copy())
                if background_attempt.played:
                    return background_attempt
                targeted_attempt = self._resolve_targeted_card_with_gamepad(after.copy())
                if targeted_attempt.played:
                    return targeted_attempt
            else:
                targeted_attempt = self._resolve_targeted_card_with_gamepad(after.copy())
                if targeted_attempt.played:
                    return targeted_attempt
                background_attempt = self._resolve_targeted_card_with_background_messages(after.copy())
                if background_attempt.played:
                    return background_attempt
            settled = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = settled.copy()
            if self._battle_progress_made(before, settled) and not self._selection_requires_target(settled) and self._selected_card_drag_origin(settled) is None:
                return CardPlayAttempt(
                    backend=background_attempt.backend,
                    played=True,
                    reason="targeted_card_played_delayed_confirmation",
                    visual_score=max(
                        self._frame_diff_score(before, settled),
                        self._hand_diff_score(before, settled),
                        self._enemy_progress_score(before, settled),
                    ),
                )
            return background_attempt
        if self._selected_card_drag_origin(after) is not None:
            selected_cost = self._selected_card_cost(after)
            if selected_cost == 0:
                self._cancel_card_selection_gamepad()
                after_cancel = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                self._last_screenshot = after_cancel.copy()
                return CardPlayAttempt(
                    backend="gamepad",
                    played=False,
                    reason=f"skip_zero_cost_non_target:slot_{slot}",
                )
            gamepad_attempt = self._resolve_non_target_card_with_gamepad(after.copy())
            if gamepad_attempt.played:
                return gamepad_attempt
            return self._resolve_non_target_card_with_background_drag(after.copy())
        diff_score = self._frame_diff_score(before, after)
        hand_diff_score = self._hand_diff_score(before, after)
        if diff_score >= 4.0 or hand_diff_score >= 8.0:
            return CardPlayAttempt(
                backend="gamepad",
                played=True,
                reason="gamepad_card_played",
                visual_score=max(diff_score, hand_diff_score),
            )
        return CardPlayAttempt(
            backend="gamepad",
            played=False,
            reason=f"card_not_selectable:slot_{slot}",
            visual_score=max(diff_score, hand_diff_score),
        )

    def _attempt_resolve_selected_gamepad_card(self, screenshot: Image.Image) -> CardPlayAttempt | None:
        if self._selection_requires_target(screenshot):
            targeted_attempt = self._resolve_targeted_card_with_gamepad(screenshot.copy())
            if targeted_attempt.played:
                return targeted_attempt
            return self._resolve_targeted_card_with_background_messages(screenshot.copy())
        if self._selected_card_drag_origin(screenshot) is None:
            return None
        selected_cost = self._selected_card_cost(screenshot)
        if selected_cost == 0:
            self._cancel_card_selection_gamepad()
            after_cancel = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
            self._last_screenshot = after_cancel.copy()
            return CardPlayAttempt(
                backend="gamepad",
                played=False,
                reason="skip_zero_cost_selected_non_target",
            )
        return self._resolve_non_target_card_with_gamepad(screenshot.copy())

    @staticmethod
    def _gamepad_battle_slot_buttons(slot: int) -> list[str]:
        return ["dpad_left"] * 6 + (["dpad_right"] * max(0, slot - 1)) + ["a"]

    def _selected_card_name(self, screenshot: Image.Image, state: GameState | None = None) -> str | None:
        width, height = screenshot.size
        crop = screenshot.crop(
            (
                round(width * 0.54),
                round(height * 0.57),
                round(width * 0.79),
                round(height * 0.71),
            )
        ).convert("RGB")
        enlarged = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)), Image.Resampling.LANCZOS)
        region = TextRegionDefinition(
            "selected_card_name",
            Rect(0, 0, enlarged.width, enlarged.height),
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz -+'",
            parser="text",
        )
        try:
            raw_text = extract_text(enlarged, region)
        except RuntimeError:
            return None
        normalized = " ".join(str(raw_text).replace("\n", " ").split()).strip(" -")
        if not normalized:
            return None
        candidates = [card.name for card in (state.deck if state is not None else [])]
        candidates.extend(
            [
                "Strike",
                "Defend",
                "Bash",
                "Pommel Strike",
                "Shrug It Off",
                "Limit Break",
                "True Grit",
                "Second Wind",
                "Inflame",
                "Battle Trance",
                "Offering",
                "Slimed",
                "Injury",
                "Wound",
                "Dazed",
                "Burn",
            ]
        )
        match = get_close_matches(normalized, sorted(set(candidates)), n=1, cutoff=0.55)
        return match[0] if match else normalized

    def _observe_battle_card_slot(self, state: GameState, slot: int, *, backend: str | None = None) -> BattleCardObservation:
        backend_name = self._backend_name_for_actions(backend)
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        input_backend = None if backend_name == "gamepad" else self._resolve_input_backend(backend)
        try:
            if backend_name == "gamepad":
                self._press_gamepad_sequence(self._gamepad_battle_slot_buttons(slot), hold_ms=80, gap_ms=80)
            else:
                assert input_backend is not None
                input_backend.key_press(str(slot), hold_ms=80)
                self._action_sleep(120)
            selected = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = selected.copy()
            requires_target = self._selection_requires_target(selected)
            drag_origin = self._selected_card_drag_origin(selected)
            fallback_origin = self._fallback_selected_card_drag_origin(selected)
            playable = requires_target or drag_origin is not None or fallback_origin is not None
            if not playable:
                diff_score = max(self._frame_diff_score(before, selected), self._hand_diff_score(before, selected))
                if diff_score >= 6.0:
                    playable = True
            target_kind = BattleTargetKind.UNKNOWN
            if requires_target:
                target_kind = BattleTargetKind.ENEMY
            elif drag_origin is not None or fallback_origin is not None:
                target_kind = BattleTargetKind.SELF_OR_NON_TARGET
            energy_cost = self._selected_card_cost(selected)
            card_name = self._selected_card_name(selected, state)
            if playable and target_kind != BattleTargetKind.UNKNOWN:
                if backend_name == "gamepad":
                    self._cancel_card_selection_gamepad()
                elif input_backend is not None:
                    self._cancel_card_selection(input_backend)
                after_cancel = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                self._last_screenshot = after_cancel.copy()
            knowledge = lookup_card_knowledge(state.character, card_name)
            return BattleCardObservation(
                slot=slot,
                playable=playable,
                energy_cost=energy_cost,
                target_kind=target_kind if playable else BattleTargetKind.NONE,
                card_name=card_name,
                damage=knowledge.damage if knowledge is not None else None,
                block=knowledge.block if knowledge is not None else None,
            )
        finally:
            if input_backend is not None:
                self._close_temporary_backend(input_backend, backend)

    def _planned_battle_cards(
        self,
        state: GameState,
        *,
        backend: str | None = None,
        max_slots: int = 10,
    ) -> list[BattleCardObservation]:
        intent = infer_run_intent(state)
        observations: list[BattleCardObservation] = []
        for slot in range(1, max_slots + 1):
            observation = self._observe_battle_card_slot(state, slot, backend=backend)
            observations.append(evaluate_battle_card(state, observation, intent))
        observations.sort(key=lambda item: (item.score, -item.slot), reverse=True)
        return observations

    def play_basic_battle_turn(
        self,
        *,
        backend: str | None = None,
        max_slots: int = 10,
        time_budget_seconds: float | None = None,
    ) -> list[str]:
        self._require_runtime()
        played: list[str] = []
        backend_name = self._backend_name_for_actions(backend)
        prefer_gamepad = backend_name == "gamepad"
        prefer_probe_drag = backend_name == "window_messages"
        max_actions = max(1, max_slots + 4)
        if prefer_gamepad:
            max_actions = min(max_actions, 3)
        if time_budget_seconds is None:
            time_budget_seconds = 2.8 if prefer_gamepad else 6.0
        deadline = time.time() + max(0.8, time_budget_seconds)
        turn_reference = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04) if prefer_gamepad else None
        no_playable_cards_observed = False
        if turn_reference is not None:
            self._last_screenshot = turn_reference.copy()
        gamepad_focus_seeded = False
        tracked_energy: int | None = None
        tracked_block: int | None = None
        while len(played) < max_actions:
            if time.time() >= deadline:
                break
            advanced = False
            failed_slots: set[int] = set()
            slot_plan: list[BattleCardObservation] = []
            slot_plan_by_slot: dict[int, BattleCardObservation] = {}
            available_turn_energy: int | None = None
            visible_slot_limit = max_slots
            if prefer_gamepad:
                current = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                self._last_screenshot = current.copy()
                visible_slots = self._visible_hand_drag_starts(current)
                if visible_slots:
                    visible_slot_limit = min(max_slots, len(visible_slots))
                zero_energy_visible = self._looks_like_zero_energy(current)
                battle_state = self._last_state
                trusted_zero_energy = (
                    tracked_energy is not None
                    and tracked_energy <= 0
                ) or (
                    battle_state is not None
                    and battle_state.energy <= 0
                    and battle_state.metric_sources.get("energy") == "memory"
                )
                if (zero_energy_visible or trusted_zero_energy) and not advanced:
                    break
                if (
                    battle_state is not None
                    and battle_state.screen == ScreenKind.BATTLE
                    and visible_slot_limit > 0
                    and time.time() + 0.8 < deadline
                ):
                    planning_state = replace(battle_state)
                    if tracked_energy is not None:
                        planning_state.energy = tracked_energy
                    elif planning_state.energy <= 0 and not zero_energy_visible:
                        planning_state.energy = 1
                    if tracked_block is not None:
                        planning_state.block = tracked_block
                    available_turn_energy = planning_state.energy
                    slot_plan = self._planned_battle_cards(
                        planning_state,
                        backend=backend,
                        max_slots=visible_slot_limit,
                    )
                    slot_plan_by_slot = {item.slot: item for item in slot_plan}
                selection_requires_target = self._selection_requires_target(current)
                selection_origin = self._selected_card_drag_origin(current)
                if (
                    slot_plan
                    and visible_slot_limit > 0
                    and not selection_requires_target
                    and selection_origin is None
                    and not any(item.playable for item in slot_plan)
                ):
                    no_playable_cards_observed = True
                    break
                if selection_requires_target:
                    selected_attempt = self._attempt_resolve_selected_gamepad_card(current)
                    if selected_attempt is not None:
                        resolved = selected_attempt.played
                        if "assumed_after_clear" in str(selected_attempt.reason):
                            resolved = False
                        selection_cleared = True
                        if self._last_screenshot is not None:
                            selection_cleared = (
                                not self._selection_requires_target(self._last_screenshot)
                                and self._selected_card_drag_origin(self._last_screenshot) is None
                            )
                        if not resolved and self._last_screenshot is not None and selection_cleared:
                            resolved = self._battle_progress_made(current, self._last_screenshot)
                        if resolved:
                            played.append(f"selected:{selected_attempt.backend}")
                            advanced = True
                            time.sleep(0.06)
                        elif str(selected_attempt.reason).startswith("skip_zero_cost_selected_non_target"):
                            time.sleep(0.04)
                elif selection_origin is not None and selection_origin[1] < round(current.height * 0.58):
                    selected_attempt = self._attempt_resolve_selected_gamepad_card(current)
                    if selected_attempt is not None:
                        resolved = selected_attempt.played
                        if "assumed_after_clear" in str(selected_attempt.reason):
                            resolved = False
                        selection_cleared = True
                        if self._last_screenshot is not None:
                            selection_cleared = (
                                self._selected_card_drag_origin(self._last_screenshot) is None
                                and not self._selection_requires_target(self._last_screenshot)
                            )
                        if not resolved and self._last_screenshot is not None and selection_cleared:
                            resolved = self._battle_progress_made(current, self._last_screenshot)
                        if resolved:
                            played.append(f"selected:{selected_attempt.backend}")
                            advanced = True
                            time.sleep(0.06)
                        elif str(selected_attempt.reason).startswith("skip_zero_cost_selected_non_target"):
                            time.sleep(0.04)
                elif selection_origin is not None:
                    # Gamepad focus often enlarges the current card without meaning it has been played.
                    time.sleep(0.04)
            if prefer_probe_drag and not prefer_gamepad:
                probe_attempt = self._attempt_probe_drag_play(backend=backend)
                if probe_attempt is not None and probe_attempt.played:
                    played.append(f"probe:{probe_attempt.backend}")
                    advanced = True
                    time.sleep(0.08 if prefer_gamepad else 0.12)
            if self._started and not prefer_gamepad and time.time() + 0.45 < deadline:
                battle_state = self.current_state()
                slot_plan = self._planned_battle_cards(battle_state, backend=backend, max_slots=max_slots)
            if not advanced:
                pass_order = [True, False] if prefer_gamepad else [False]
                for prefer_non_target_only in pass_order:
                    if time.time() >= deadline:
                        break
                    planned_order = [item.slot for item in slot_plan if item.playable]
                    if prefer_gamepad and prefer_non_target_only:
                        planned_order = [
                            item.slot
                            for item in slot_plan
                            if item.playable and item.target_kind != BattleTargetKind.ENEMY
                        ] + [
                            item.slot
                            for item in slot_plan
                            if item.playable and item.target_kind == BattleTargetKind.ENEMY
                        ]
                    slot_order = planned_order or (
                        list(range(max(1, visible_slot_limit), 0, -1))
                        if prefer_gamepad and prefer_non_target_only
                        else list(range(1, max(1, visible_slot_limit if prefer_gamepad else max_slots) + 1))
                    )
                    for slot in slot_order:
                        if time.time() >= deadline:
                            break
                        if slot in failed_slots:
                            continue
                        before = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                        self._last_screenshot = before.copy()
                        try:
                            attempt = self._attempt_play_card_slot(
                                slot,
                                backend=backend,
                                prefer_non_target_only=prefer_non_target_only,
                                reset_gamepad_focus=not gamepad_focus_seeded,
                            )
                            if prefer_gamepad:
                                gamepad_focus_seeded = True
                        except Exception:
                            failed_slots.add(slot)
                            continue
                        after = self._last_screenshot.copy() if self._last_screenshot is not None else self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                        selection_still_active = (
                            self._selection_requires_target(after)
                            or self._selected_card_drag_origin(after) is not None
                        )
                        progressed = attempt.played or (
                            self._battle_progress_made(before, after)
                            and not selection_still_active
                        )
                        if progressed:
                            tracked_energy = self._consume_tracked_battle_energy(
                                available_turn_energy,
                                slot_plan_by_slot.get(slot),
                            )
                            tracked_block = self._gain_tracked_battle_block(
                                tracked_block if tracked_block is not None else battle_state.block if battle_state is not None else None,
                                slot_plan_by_slot.get(slot),
                            )
                            played.append(f"slot={slot}:{attempt.backend}")
                            advanced = True
                            if prefer_gamepad:
                                gamepad_focus_seeded = False
                            time.sleep(0.08 if prefer_gamepad else 0.12)
                            break
                        if not (
                            str(attempt.reason).startswith("defer_targeted:")
                            or str(attempt.reason).startswith("skip_zero_cost_non_target:")
                        ):
                            failed_slots.add(slot)
                    if advanced:
                        break
            if not advanced and not prefer_probe_drag and not prefer_gamepad:
                probe_attempt = self._attempt_probe_drag_play(backend=backend)
                if probe_attempt is not None and probe_attempt.played:
                    played.append(f"probe:{probe_attempt.backend}")
                    advanced = True
                    time.sleep(0.12)
            if (
                not advanced
                and prefer_gamepad
                and slot_plan
                and any(item.playable for item in slot_plan)
                and time.time() + 0.35 < deadline
            ):
                fallback_slots = [item.slot for item in slot_plan if item.playable]
                probe_attempt = self._attempt_probe_drag_play(
                    backend="window_messages",
                    preferred_slots=fallback_slots,
                )
                if probe_attempt is not None and probe_attempt.played:
                    tracked_energy = self._consume_tracked_battle_energy(
                        available_turn_energy,
                        slot_plan_by_slot.get(fallback_slots[0]) if fallback_slots else None,
                    )
                    played.append(f"probe:{probe_attempt.backend}")
                    advanced = True
                    gamepad_focus_seeded = False
                    time.sleep(0.12)
            if not advanced:
                break
        if prefer_gamepad:
            settled = self._last_screenshot.copy() if self._last_screenshot is not None else self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
            if (
                not played
                and turn_reference is not None
                and not self._selection_requires_target(settled)
                and self._selected_card_drag_origin(settled) is None
                and self._battle_progress_made(turn_reference, settled)
            ):
                played.append("progress:inferred")
            should_end_turn = bool(played) or self._looks_like_zero_energy(settled) or no_playable_cards_observed
            if should_end_turn:
                self._end_battle_turn(backend=backend)
            elif self._selection_requires_target(settled) or self._selected_card_drag_origin(settled) is not None:
                self._cancel_card_selection_gamepad()
        else:
            input_backend = self._resolve_input_backend(backend)
            input_backend.key_press("e", hold_ms=80)
            self._action_sleep(self.profile.action_delay_ms)
            self._close_temporary_backend(input_backend, backend)
        return played

    def _resolve_non_target_card_with_gamepad(self, selected_reference: Image.Image) -> CardPlayAttempt:
        sequences = [
            ["a"],
            ["dpad_up", "a"],
            ["a", "a"],
            ["dpad_up", "a", "a"],
        ]
        last_failure: CardPlayAttempt | None = None
        for buttons in sequences:
            self._press_gamepad_sequence(buttons, hold_ms=80, gap_ms=80)
            after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after):
                targeted_attempt = self._resolve_targeted_card_with_gamepad(after.copy())
                if targeted_attempt.played:
                    return targeted_attempt
                background_attempt = self._resolve_targeted_card_with_background_messages(after.copy())
                if background_attempt.played:
                    return background_attempt
                last_failure = background_attempt
                continue
            if self._selected_card_drag_origin(after) is not None:
                continue
            resolved_attempt = self._assess_non_target_resolution(
                selected_reference,
                after,
                backend="gamepad",
                success_reason="gamepad_selected_card_played",
                failure_reason="gamepad_selection_cleared_without_resolution",
            )
            if resolved_attempt.played:
                return resolved_attempt
            if resolved_attempt.reason == "gamepad_selection_cleared_without_resolution":
                return CardPlayAttempt(
                    backend="gamepad",
                    played=True,
                    reason="gamepad_selected_card_assumed_after_clear",
                    visual_score=resolved_attempt.visual_score,
                )
            last_failure = resolved_attempt
        self._cancel_card_selection_gamepad()
        if last_failure is not None:
            return last_failure
        return CardPlayAttempt(
            backend="gamepad",
            played=False,
            reason="gamepad_selected_card_failed",
        )

    def _assess_non_target_resolution(
        self,
        selected_reference: Image.Image,
        after: Image.Image,
        *,
        backend: str,
        success_reason: str,
        failure_reason: str,
    ) -> CardPlayAttempt:
        visual_score = 0.0
        for attempt_index in range(3):
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                return CardPlayAttempt(
                    backend=backend,
                    played=False,
                    reason=failure_reason,
                    visual_score=visual_score,
                )
            diff_score = self._frame_diff_score(selected_reference, after)
            hand_diff_score = self._hand_diff_score(selected_reference, after)
            enemy_diff_score = self._enemy_progress_score(selected_reference, after)
            visual_score = max(diff_score, hand_diff_score, enemy_diff_score)
            if (
                diff_score >= 4.0
                or hand_diff_score >= 8.0
                or enemy_diff_score >= 4.0
                or self._battle_progress_made(selected_reference, after)
            ):
                return CardPlayAttempt(
                    backend=backend,
                    played=True,
                    reason=success_reason,
                    visual_score=visual_score,
                )
            if attempt_index == 2:
                break
            time.sleep(0.08)
            after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        return CardPlayAttempt(
            backend=backend,
            played=False,
            reason=failure_reason,
            visual_score=visual_score,
        )

    def _resolve_non_target_card_with_background_drag(self, selected_reference: Image.Image) -> CardPlayAttempt:
        drag_origin = self._selected_card_drag_origin(selected_reference)
        if drag_origin is None:
            drag_origin = self._fallback_selected_card_drag_origin(selected_reference)
        if drag_origin is None:
            return self._resolve_non_target_card_with_gamepad(selected_reference)
        width, height = selected_reference.size
        neutral_point = (round(width * 0.52), round(height * 0.44))
        input_backend = self._resolve_input_backend("window_messages")
        try:
            input_backend.drag(drag_origin[0], drag_origin[1], neutral_point[0], neutral_point[1], duration_ms=220)
            self._action_sleep(140)
            after = self._capture_window_image()
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                self._cancel_card_selection(input_backend)
                return CardPlayAttempt(
                    backend=input_backend.diagnostics().backend,
                    played=False,
                    reason="background_drag_selection_stuck",
                )
            return self._assess_non_target_resolution(
                selected_reference,
                after,
                backend=input_backend.diagnostics().backend,
                success_reason="background_drag_card_played",
                failure_reason="background_drag_no_progress",
            )
        finally:
            self._close_temporary_backend(input_backend, "window_messages")

    def _attempt_probe_drag_play(
        self,
        *,
        backend: str | None = None,
        preferred_slots: list[int] | None = None,
    ) -> CardPlayAttempt | None:
        screenshot = self._capture_window_image()
        width, height = screenshot.size
        input_backend = self._resolve_input_backend(backend)
        try:
            probe_points: list[tuple[int, int]] = [self._scale_reference_point((861, 469))]
            neutral_point = (round(width * 0.52), round(height * 0.44))
            starts = self._visible_hand_drag_starts(screenshot)
            if not starts:
                fractions = [0.24, 0.33, 0.42, 0.52, 0.61, 0.70]
                starts = [(round(width * fraction), round(height * 0.80)) for fraction in fractions]
            if starts and preferred_slots:
                ordered_starts: list[tuple[int, int]] = []
                seen_indexes: set[int] = set()
                for slot in preferred_slots:
                    slot_index = slot - 1
                    if 0 <= slot_index < len(starts) and slot_index not in seen_indexes:
                        ordered_starts.append(starts[slot_index])
                        seen_indexes.add(slot_index)
                ordered_starts.extend(
                    start
                    for index, start in enumerate(starts)
                    if index not in seen_indexes
                )
                starts = ordered_starts
            for start in starts:
                for end in probe_points:
                    before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
                    input_backend.drag(start[0], start[1], end[0], end[1], duration_ms=280)
                    self._action_sleep(180)
                    after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
                    self._last_screenshot = after.copy()
                    if self._selection_requires_target(after):
                        resolution = self._resolve_targeted_card(input_backend, after)
                        if resolution.played:
                            return CardPlayAttempt(
                                backend=resolution.backend,
                                played=True,
                                reason=f"probe_drag_then_{resolution.reason}",
                                visual_score=resolution.visual_score,
                            )
                        self._cancel_card_selection(input_backend)
                        continue
                    origin = self._selected_card_drag_origin(after)
                    if origin is not None:
                        input_backend.drag(origin[0], origin[1], neutral_point[0], neutral_point[1], duration_ms=220)
                        self._action_sleep(140)
                        resolved = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
                        self._last_screenshot = resolved.copy()
                        if self._selection_requires_target(resolved) or self._selected_card_drag_origin(resolved) is not None:
                            self._cancel_card_selection(input_backend)
                            continue
                        diff_score = self._frame_diff_score(before, resolved)
                        hand_diff_score = self._hand_diff_score(before, resolved)
                        if diff_score >= 4.0 or hand_diff_score >= 8.0:
                            return CardPlayAttempt(
                                backend=input_backend.diagnostics().backend,
                                played=True,
                                reason="probe_drag_then_neutral_played",
                                visual_score=max(diff_score, hand_diff_score),
                            )
                        continue
                    diff_score = self._frame_diff_score(before, after)
                    hand_diff_score = self._hand_diff_score(before, after)
                    if diff_score >= 4.0 or hand_diff_score >= 8.0:
                        return CardPlayAttempt(
                            backend=input_backend.diagnostics().backend,
                            played=True,
                            reason="probe_drag_card_played",
                            visual_score=max(diff_score, hand_diff_score),
                        )
            return None
        finally:
            self._close_temporary_backend(input_backend, backend)

    def _backend_name_for_actions(self, backend: str | None) -> str:
        if backend in {None, "", "auto"}:
            runtime = self._require_runtime()
            return runtime.input_backend.diagnostics().backend
        if backend == "window_messages":
            return "window_messages"
        if backend.startswith("window_messages_"):
            return "window_messages"
        return backend

    def _resolve_targeted_card(self, input_backend: InputBackend, screenshot: Image.Image) -> CardPlayAttempt:
        selected_reference = screenshot.copy()
        backend_name = input_backend.diagnostics().backend
        prefer_pointer_resolution = self._last_state is not None and len(self._last_state.enemies) > 1
        if not prefer_pointer_resolution:
            keyboard_attempt = self._resolve_targeted_card_with_navigation(input_backend, selected_reference)
            if keyboard_attempt is not None:
                return keyboard_attempt
        last_failure: CardPlayAttempt | None = None
        for target_point in self._target_candidate_points(screenshot, selected_reference=selected_reference):
            input_backend.move(target_point[0], target_point[1])
            self._action_sleep(80)
            hovered = self._capture_window_image()
            self._last_screenshot = hovered.copy()
            if not self._selection_requires_target(hovered) and self._selected_card_drag_origin(hovered) is None:
                resolved_attempt = self._assess_target_resolution(
                    selected_reference,
                    hovered,
                    backend=backend_name,
                    success_reason="targeted_card_played_hover",
                    failure_reason="target_selection_cleared_without_resolution",
                )
                if resolved_attempt is not None and resolved_attempt.played:
                    return resolved_attempt
                if resolved_attempt is not None:
                    last_failure = resolved_attempt
            input_backend.key_press("enter", hold_ms=70)
            self._action_sleep(110)
            after_enter = self._capture_window_image()
            self._last_screenshot = after_enter.copy()
            if not self._selection_requires_target(after_enter) and self._selected_card_drag_origin(after_enter) is None:
                resolved_attempt = self._assess_target_resolution(
                    selected_reference,
                    after_enter,
                    backend=backend_name,
                    success_reason="targeted_card_played_hover_enter",
                    failure_reason="target_selection_cleared_without_resolution",
                )
                if resolved_attempt is not None and resolved_attempt.played:
                    return resolved_attempt
                if resolved_attempt is not None:
                    last_failure = resolved_attempt
            input_backend.click(target_point[0], target_point[1])
            self._action_sleep(140)
            after = self._capture_window_image()
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after):
                continue
            if self._selected_card_drag_origin(after) is not None:
                continue
            resolved_attempt = self._assess_target_resolution(
                selected_reference,
                after,
                backend=backend_name,
                success_reason="targeted_card_played",
                failure_reason="target_selection_cleared_without_resolution",
            )
            if resolved_attempt is not None and resolved_attempt.played:
                return resolved_attempt
            if resolved_attempt is not None:
                last_failure = resolved_attempt
        if prefer_pointer_resolution:
            keyboard_attempt = self._resolve_targeted_card_with_navigation(input_backend, selected_reference)
            if keyboard_attempt is not None:
                return keyboard_attempt
        self._cancel_card_selection(input_backend)
        if last_failure is not None:
            return last_failure
        return CardPlayAttempt(
            backend=backend_name,
            played=False,
            reason="target_selection_failed",
        )

    def _resolve_targeted_card_with_gamepad(self, selected_reference: Image.Image) -> CardPlayAttempt:
        sequences = self._target_navigation_sequences(selected_reference, use_gamepad=True)
        last_failure: CardPlayAttempt | None = None
        for buttons in sequences:
            self._press_gamepad_sequence(buttons, hold_ms=80, gap_ms=80)
            after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                continue
            resolved_attempt = self._assess_target_resolution(
                selected_reference,
                after,
                backend="gamepad",
                success_reason="targeted_card_played_gamepad",
                failure_reason="target_selection_gamepad_cleared_without_resolution",
            )
            if resolved_attempt is not None and resolved_attempt.played:
                return resolved_attempt
            if resolved_attempt is not None:
                last_failure = resolved_attempt
        self._cancel_card_selection_gamepad()
        if last_failure is not None:
            return last_failure
        return CardPlayAttempt(
            backend="gamepad",
            played=False,
            reason="target_selection_gamepad_failed",
        )

    def _prefer_background_target_resolution(self) -> bool:
        if self._last_state is None:
            return False
        return len(self._last_state.enemies) > 1

    def _assess_target_resolution(
        self,
        selected_reference: Image.Image,
        after: Image.Image,
        *,
        backend: str,
        success_reason: str,
        failure_reason: str,
    ) -> CardPlayAttempt | None:
        for attempt_index in range(3):
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                return None
            diff_score = self._frame_diff_score(selected_reference, after)
            hand_diff_score = self._hand_diff_score(selected_reference, after)
            enemy_diff_score = self._enemy_progress_score(selected_reference, after)
            visual_score = max(diff_score, hand_diff_score, enemy_diff_score)
            if (
                diff_score >= 4.0
                or hand_diff_score >= 8.0
                or enemy_diff_score >= 4.0
                or self._battle_progress_made(selected_reference, after)
            ):
                return CardPlayAttempt(
                    backend=backend,
                    played=True,
                    reason=success_reason,
                    visual_score=visual_score,
                )
            if attempt_index == 2:
                break
            time.sleep(0.08)
            after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        return CardPlayAttempt(
            backend=backend,
            played=False,
            reason=failure_reason,
            visual_score=visual_score,
        )

    def _resolve_targeted_card_with_background_messages(self, selected_reference: Image.Image) -> CardPlayAttempt:
        input_backend = self._resolve_input_backend("window_messages")
        try:
            return self._resolve_targeted_card(input_backend, selected_reference)
        finally:
            self._close_temporary_backend(input_backend, "window_messages")

    def _resolve_targeted_card_with_navigation(
        self,
        input_backend: InputBackend,
        selected_reference: Image.Image,
    ) -> CardPlayAttempt | None:
        backend_name = input_backend.diagnostics().backend
        last_failure: CardPlayAttempt | None = None
        sequences = self._target_navigation_sequences(selected_reference, use_gamepad=False)
        for sequence in sequences:
            for key_name in sequence:
                input_backend.key_press(key_name, hold_ms=70)
                self._action_sleep(85)
            after = self._capture_window_image()
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after) or self._selected_card_drag_origin(after) is not None:
                continue
            resolved_attempt = self._assess_target_resolution(
                selected_reference,
                after,
                backend=backend_name,
                success_reason="targeted_card_played_navigation",
                failure_reason="target_selection_navigation_cleared_without_resolution",
            )
            if resolved_attempt is not None and resolved_attempt.played:
                return resolved_attempt
            if resolved_attempt is not None:
                last_failure = resolved_attempt
        return last_failure

    def _cancel_card_selection(self, input_backend: InputBackend) -> None:
        input_backend.key_press(self.profile.battle_cancel_key, hold_ms=70)
        self._action_sleep(90)

    def _cancel_card_selection_gamepad(self) -> None:
        self._press_gamepad_sequence(["dpad_down"], hold_ms=70, gap_ms=60)

    def _press_gamepad_sequence(
        self,
        buttons: list[str],
        *,
        settle_ms: int = 0,
        hold_ms: int = 120,
        gap_ms: int = 110,
    ) -> None:
        press_xbox_sequence(
            buttons,
            settle_ms=settle_ms,
            hold_ms=hold_ms,
            gap_ms=gap_ms,
        )
        time.sleep(min(0.08, max(0, self.profile.action_delay_ms) / 1000 / 6))

    def _try_gamepad_progress_sequences(
        self,
        reference: Image.Image,
        sequences: list[list[str]],
        *,
        success_threshold: float = 4.0,
    ) -> tuple[bool, Image.Image]:
        current_reference = reference.copy()
        current_after = reference.copy()
        for buttons in sequences:
            self._press_gamepad_sequence(buttons, hold_ms=80, gap_ms=80)
            current_after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = current_after.copy()
            if (
                self._frame_diff_score(current_reference, current_after) >= success_threshold
                or self._battle_progress_made(current_reference, current_after)
            ):
                return True, current_after
            if self._frame_diff_score(current_reference, current_after) >= 0.5:
                current_reference = current_after.copy()
        return False, current_after

    @staticmethod
    def _event_option_points(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if float((gray < 40).mean()) < 0.52:
            return []
        top = max(0, round(height * 0.42))
        bottom = min(height, round(height * 0.82))
        left = max(0, round(width * 0.42))
        right = min(width, round(width * 0.96))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = (
            (hsv[:, :, 0] > 70)
            & (hsv[:, :, 0] < 110)
            & (hsv[:, :, 1] > 25)
            & (hsv[:, :, 2] > 55)
        ).astype(np.uint8)
        row_activity = mask.sum(axis=1).astype(np.float32)
        if row_activity.size == 0 or float(row_activity.max()) < 120.0:
            return []
        threshold = max(120.0, float(row_activity.max()) * 0.45)
        bands: list[tuple[int, int]] = []
        start: int | None = None
        for index, activity in enumerate(row_activity):
            if activity >= threshold:
                if start is None:
                    start = index
                continue
            if start is None:
                continue
            if index - start >= 14:
                bands.append((start, index - 1))
            start = None
        if start is not None and len(row_activity) - start >= 14:
            bands.append((start, len(row_activity) - 1))
        points: list[tuple[int, int]] = []
        for band_top, band_bottom in bands:
            band = mask[band_top : band_bottom + 1, :]
            ys, xs = np.nonzero(band)
            if xs.size == 0:
                continue
            span_width = int(xs.max() - xs.min() + 1)
            if span_width < max(140, round(region.shape[1] * 0.30)):
                continue
            if span_width > round(region.shape[1] * 0.97):
                continue
            span_height = band_bottom - band_top + 1
            if span_height < 18 or span_height > max(90, round(region.shape[0] * 0.24)):
                continue
            center_x = left + int(round(float(xs.mean())))
            center_y = top + int(round((band_top + band_bottom) / 2.0))
            points.append((center_x, center_y))
        points.sort(key=lambda point: point[1])
        deduped: list[tuple[int, int]] = []
        for point in points:
            if deduped and abs(point[1] - deduped[-1][1]) <= 18:
                continue
            deduped.append(point)
        return deduped

    def _neow_choice_actions(self, screenshot: Image.Image) -> list[GameAction]:
        actions: list[GameAction] = []
        for option_index in range(3):
            option_text = self._neow_option_text(screenshot, option_index)
            payload = {
                "target": "generic_neow_option",
                "option_index": option_index,
                "option_text": option_text,
            }
            tags = ["start", "neow", "progress"]
            for tag in self._classify_neow_option_tags(option_text):
                if tag not in tags:
                    tags.append(tag)
            actions.append(
                GameAction(
                    kind=ActionKind.NAVIGATE,
                    label=f"Neow option {option_index + 1}",
                    payload=payload,
                    tags=tags,
                )
            )
        return actions

    def _neow_option_text(self, screenshot: Image.Image, option_index: int) -> str:
        title_region, body_region = self._neow_option_regions(screenshot.size, option_index)
        snippets: list[str] = []
        for name, region in (
            (f"neow_option_{option_index + 1}_title", title_region),
            (f"neow_option_{option_index + 1}_body", body_region),
        ):
            normalized = self._extract_text_safe(screenshot, name, region, fast=True)
            if normalized:
                snippets.append(normalized)
        return " | ".join(snippets)

    def _neow_option_regions(self, image_size: tuple[int, int], option_index: int) -> tuple[Rect, Rect]:
        width, height = image_size
        scale_x = width / self.profile.reference_width
        scale_y = height / self.profile.reference_height
        clamped_index = min(max(option_index, 0), 2)
        # Anchor option regions to the actual three choice rows, not the speech bubble above them.
        base_top = 628 + clamped_index * 90
        title = Rect(455, base_top, 520, 52).scaled(scale_x, scale_y)
        body = Rect(455, base_top + 36, 560, 54).scaled(scale_x, scale_y)
        return title, body

    @staticmethod
    def _normalize_neow_option_text(text: str) -> str:
        collapsed = " ".join(str(text or "").split()).strip()
        if not collapsed:
            return ""
        cleaned = collapsed.replace("’", "'").replace("“", '"').replace("”", '"')
        return cleaned[:120]

    @staticmethod
    def _classify_neow_option_tags(text: str) -> list[str]:
        normalized = text.lower()
        tags: list[str] = []
        keyword_groups = {
            "remove": ("remove",),
            "transform": ("transform",),
            "gold": ("gold",),
            "rare": ("rare",),
            "random": ("random",),
            "hp_cost": ("lose", "hp"),
            "add_card": ("add", "deck"),
            "curse": ("curse", "clumsy", "wound", "regret", "decay"),
        }
        for tag, keywords in keyword_groups.items():
            if all(keyword in normalized for keyword in keywords):
                tags.append(tag)
        return tags

    def _extract_text_safe(
        self,
        screenshot: Image.Image,
        name: str,
        region: Rect,
        *,
        whitelist: str | None = None,
        fast: bool = False,
    ) -> str:
        definition = TextRegionDefinition(name, region, whitelist=whitelist, parser="str")
        try:
            text = extract_text_fast(screenshot, definition) if fast else extract_text(screenshot, definition)
        except RuntimeError:
            if self.strict_ocr:
                raise
            return ""
        return self._normalize_neow_option_text(text)

    @staticmethod
    def _payload_with_click_point(payload: dict[str, object], point: tuple[int, int]) -> dict[str, object]:
        updated = dict(payload)
        updated["click_point"] = [int(point[0]), int(point[1])]
        return updated

    def _event_option_text(self, screenshot: Image.Image, point: tuple[int, int]) -> str:
        width, height = screenshot.size
        region = Rect(
            max(0, point[0] - round(width * 0.18)),
            max(0, point[1] - round(height * 0.035)),
            min(width, round(width * 0.36)),
            max(24, round(height * 0.07)),
        )
        return self._extract_text_safe(screenshot, "event_option_text", region, fast=True)

    @staticmethod
    def _classify_event_option_tags(text: str) -> list[str]:
        normalized = text.lower()
        tags = ["progress"]
        keyword_groups = {
            "heal": ("heal", "rest", "recover"),
            "gold": ("gold",),
            "hp_cost": ("lose", "hp"),
            "remove": ("remove", "purge"),
            "upgrade": ("upgrade", "smith"),
            "combat": ("fight", "combat"),
            "relic": ("relic",),
            "curse": ("curse", "clumsy", "wound", "regret", "decay"),
        }
        for tag, keywords in keyword_groups.items():
            if any(keyword in normalized for keyword in keywords):
                if tag not in tags:
                    tags.append(tag)
        return tags

    def _event_choice_actions(self, screenshot: Image.Image) -> list[GameAction]:
        option_points = self._event_option_points(screenshot)
        if len(option_points) <= 1:
            return []
        actions: list[GameAction] = []
        for option_index, point in enumerate(option_points):
            option_text = self._event_option_text(screenshot, point)
            tags = self._classify_event_option_tags(option_text)
            payload = self._payload_with_click_point(
                {
                    "target": "generic_event_option",
                    "option_index": option_index,
                    "option_text": option_text,
                    "choice_domain": ChoiceDomain.EVENT.value,
                },
                point,
            )
            actions.append(
                GameAction(
                    kind=ActionKind.NAVIGATE,
                    label=f"Event option {option_index + 1}",
                    payload=payload,
                    tags=tags,
                )
            )
        return actions

    def _looks_like_rest_screen(self, screenshot: Image.Image) -> bool:
        width, height = screenshot.size
        title_region = Rect(430, 120, 500, 70).scaled(width / self.profile.reference_width, height / self.profile.reference_height)
        title_text = self._extract_text_safe(
            screenshot,
            "rest_title",
            title_region,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ?!'",
        ).lower()
        normalized = "".join(char for char in title_text if char.isalpha())
        return "whatshallido" in normalized

    def _reward_card_name(self, screenshot: Image.Image, point: tuple[int, int]) -> str | None:
        width, height = screenshot.size
        region = Rect(
            max(0, point[0] - round(width * 0.08)),
            max(0, point[1] - round(height * 0.20)),
            max(80, round(width * 0.16)),
            max(28, round(height * 0.06)),
        )
        text = self._extract_text_safe(
            screenshot,
            "reward_card_name",
            region,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz -+'",
        )
        return canonicalize_card_name(text) if text else None

    def _reward_card_actions(self, screenshot: Image.Image) -> list[GameAction]:
        points = self._reward_card_points(screenshot)
        if len(points) < 2:
            return []
        fallback_tags = {0: ["attack"], 1: ["block"], 2: ["scaling"]}
        actions: list[GameAction] = []
        for option_index, point in enumerate(points[:3]):
            card_name = self._reward_card_name(screenshot, point)
            tags = fallback_tags.get(option_index, []).copy()
            if card_name:
                knowledge = lookup_card_knowledge(self.profile.static_character, card_name)
                if knowledge is not None:
                    tags = list(dict.fromkeys(list(knowledge.tags) + tags))
            payload = self._payload_with_click_point(
                {
                    "choice_domain": ChoiceDomain.REWARD_CARD.value,
                    "option_index": option_index,
                    "card": card_name or f"slot_{option_index + 1}",
                },
                point,
            )
            actions.append(
                GameAction(
                    kind=ActionKind.PICK_CARD,
                    label=f"Card option {option_index + 1}",
                    payload=payload,
                    tags=tags,
                )
            )
        return actions

    def _reward_center_name(self, screenshot: Image.Image, *, name: str) -> str:
        width, height = screenshot.size
        region = Rect(
            max(0, round(width * 0.35)),
            max(0, round(height * 0.18)),
            max(120, round(width * 0.30)),
            max(36, round(height * 0.07)),
        )
        return self._extract_text_safe(
            screenshot,
            name,
            region,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz -+'",
        )

    def _reward_relic_actions(self, screenshot: Image.Image) -> list[GameAction]:
        relic_name = canonicalize_relic_name(self._reward_center_name(screenshot, name="reward_relic_name"))
        if not relic_name:
            return []
        take_point = (round(screenshot.width * 0.50), round(screenshot.height * 0.63))
        skip_point = (round(screenshot.width * 0.50), round(screenshot.height * 0.94))
        return [
            GameAction(
                kind=ActionKind.TAKE_RELIC,
                label=f"Take {relic_name}",
                payload=self._payload_with_click_point(
                    {
                        "choice_domain": ChoiceDomain.REWARD_RELIC.value,
                        "relic": relic_name,
                    },
                    take_point,
                ),
                tags=["reward"],
            ),
            GameAction(
                kind=ActionKind.SKIP_REWARD,
                label="Skip reward",
                payload=self._payload_with_click_point(
                    {
                        "choice_domain": ChoiceDomain.REWARD_RELIC.value,
                        "target": "skip",
                    },
                    skip_point,
                ),
                tags=["skip"],
            ),
        ]

    def _reward_potion_actions(self, screenshot: Image.Image) -> list[GameAction]:
        potion_name = canonicalize_potion_name(self._reward_center_name(screenshot, name="reward_potion_name"))
        if not potion_name:
            return []
        take_point = (round(screenshot.width * 0.50), round(screenshot.height * 0.63))
        skip_point = (round(screenshot.width * 0.50), round(screenshot.height * 0.94))
        potion_tags = list(lookup_potion_knowledge(potion_name).tags) if lookup_potion_knowledge(potion_name) is not None else ["potion"]
        return [
            GameAction(
                kind=ActionKind.TAKE_POTION,
                label=f"Take {potion_name}",
                payload=self._payload_with_click_point(
                    {
                        "choice_domain": ChoiceDomain.REWARD_POTION.value,
                        "potion": potion_name,
                    },
                    take_point,
                ),
                tags=potion_tags,
            ),
            GameAction(
                kind=ActionKind.SKIP_REWARD,
                label="Skip reward",
                payload=self._payload_with_click_point(
                    {
                        "choice_domain": ChoiceDomain.REWARD_POTION.value,
                        "target": "skip",
                    },
                    skip_point,
                ),
                tags=["skip"],
            ),
        ]

    def _boss_relic_name(self, screenshot: Image.Image, option_index: int) -> str | None:
        width, height = screenshot.size
        centers = [0.25, 0.50, 0.75]
        center_x = round(width * centers[min(max(option_index, 0), 2)])
        region = Rect(
            max(0, center_x - round(width * 0.10)),
            max(0, round(height * 0.15)),
            max(120, round(width * 0.20)),
            max(30, round(height * 0.06)),
        )
        raw = self._extract_text_safe(
            screenshot,
            f"boss_relic_{option_index}",
            region,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz -+'",
        )
        knowledge = lookup_boss_relic_knowledge(raw)
        return knowledge.name if knowledge is not None else None

    def _boss_relic_actions(self, screenshot: Image.Image) -> list[GameAction]:
        actions: list[GameAction] = []
        found_names = 0
        for option_index in range(3):
            relic_name = self._boss_relic_name(screenshot, option_index)
            if relic_name is None:
                continue
            found_names += 1
            point = (
                round(screenshot.width * [0.25, 0.50, 0.75][option_index]),
                round(screenshot.height * 0.58),
            )
            actions.append(
                GameAction(
                    kind=ActionKind.TAKE_RELIC,
                    label=f"Take {relic_name}",
                    payload=self._payload_with_click_point(
                        {
                            "choice_domain": ChoiceDomain.BOSS_RELIC.value,
                            "boss_relic": relic_name,
                            "relic": relic_name,
                            "option_index": option_index,
                        },
                        point,
                    ),
                    tags=["boss", "relic"],
                )
            )
        if found_names < 2:
            return []
        skip_point = (round(screenshot.width * 0.50), round(screenshot.height * 0.93))
        actions.append(
            GameAction(
                kind=ActionKind.SKIP_REWARD,
                label="Skip reward",
                payload=self._payload_with_click_point(
                    {
                        "choice_domain": ChoiceDomain.BOSS_RELIC.value,
                        "target": "skip",
                    },
                    skip_point,
                ),
                tags=["skip"],
            )
        )
        return actions

    def _reward_menu_row_kind(self, screenshot: Image.Image, point: tuple[int, int]) -> str:
        width, height = screenshot.size
        region = Rect(
            max(0, point[0] - round(width * 0.12)),
            max(0, point[1] - round(height * 0.03)),
            max(120, round(width * 0.24)),
            max(24, round(height * 0.05)),
        )
        text = self._extract_text_safe(screenshot, "reward_menu_row", region, fast=True)
        normalized = text.lower()
        if "gold" in normalized:
            return "gold"
        if "relic" in normalized:
            return "relic_reward"
        if "potion" in normalized:
            return "potion_reward"
        if "card" in normalized:
            return "card_reward"
        return ""

    def _reward_menu_actions(self, screenshot: Image.Image) -> list[GameAction]:
        actions: list[GameAction] = []
        for point, fallback_kind in self._reward_menu_option_points(screenshot):
            kind = self._reward_menu_row_kind(screenshot, point) or fallback_kind
            if kind == "gold":
                label = "Take gold"
            elif kind == "relic_reward":
                label = "Open relic reward"
            elif kind == "potion_reward":
                label = "Open potion reward"
            else:
                label = "Open card reward"
                kind = "card_reward"
            payload = self._payload_with_click_point({"target": kind}, point)
            actions.append(GameAction(kind=ActionKind.NAVIGATE, label=label, payload=payload, tags=["reward", "progress"]))
        return actions

    def _shop_offer_definitions(self, image_size: tuple[int, int]) -> list[dict[str, object]]:
        width, height = image_size
        return [
            {
                "item_type": "card",
                "point": (round(width * 0.24), round(height * 0.37)),
                "name_region": Rect(round(width * 0.16), round(height * 0.24), round(width * 0.16), round(height * 0.05)),
                "price_region": Rect(round(width * 0.19), round(height * 0.44), round(width * 0.08), round(height * 0.05)),
            },
            {
                "item_type": "card",
                "point": (round(width * 0.39), round(height * 0.37)),
                "name_region": Rect(round(width * 0.31), round(height * 0.24), round(width * 0.16), round(height * 0.05)),
                "price_region": Rect(round(width * 0.34), round(height * 0.44), round(width * 0.08), round(height * 0.05)),
            },
            {
                "item_type": "relic",
                "point": (round(width * 0.58), round(height * 0.34)),
                "name_region": Rect(round(width * 0.50), round(height * 0.22), round(width * 0.16), round(height * 0.05)),
                "price_region": Rect(round(width * 0.53), round(height * 0.43), round(width * 0.08), round(height * 0.05)),
            },
            {
                "item_type": "potion",
                "point": (round(width * 0.76), round(height * 0.33)),
                "name_region": Rect(round(width * 0.68), round(height * 0.22), round(width * 0.16), round(height * 0.05)),
                "price_region": Rect(round(width * 0.72), round(height * 0.42), round(width * 0.08), round(height * 0.05)),
            },
            {
                "item_type": "remove",
                "point": (round(width * 0.86), round(height * 0.74)),
                "name_region": Rect(round(width * 0.76), round(height * 0.68), round(width * 0.18), round(height * 0.05)),
                "price_region": Rect(round(width * 0.82), round(height * 0.79), round(width * 0.08), round(height * 0.05)),
            },
        ]

    @staticmethod
    def _parse_shop_price(text: str) -> int | None:
        digits = "".join(char for char in str(text) if char.isdigit())
        if not digits:
            return None
        value = int(digits)
        if 1 <= value <= 999:
            return value
        return None

    def _shop_actions(self, screenshot: Image.Image) -> list[GameAction]:
        actions: list[GameAction] = []
        for offer in self._shop_offer_definitions(screenshot.size):
            item_type = str(offer["item_type"])
            name_text = self._extract_text_safe(
                screenshot,
                f"shop_{item_type}_name",
                offer["name_region"],
                whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz -+'",
            )
            price_text = self._extract_text_safe(
                screenshot,
                f"shop_{item_type}_price",
                offer["price_region"],
                whitelist="0123456789",
                fast=True,
            )
            price = self._parse_shop_price(price_text)
            if item_type == "card":
                item_name = canonicalize_card_name(name_text) or name_text
            elif item_type == "relic":
                item_name = canonicalize_relic_name(name_text) or name_text
            elif item_type == "potion":
                item_name = canonicalize_potion_name(name_text) or name_text
            else:
                item_name = "Remove card"
            if not item_name or price is None:
                continue
            payload = self._payload_with_click_point(
                {
                    "choice_domain": ChoiceDomain.SHOP_REMOVE.value if item_type == "remove" else ChoiceDomain.SHOP_PURCHASE.value,
                    "shop_item_type": item_type,
                    "price": price,
                },
                offer["point"],
            )
            if item_type == "card":
                payload["card"] = item_name
            elif item_type == "relic":
                payload["relic"] = item_name
            elif item_type == "potion":
                payload["potion"] = item_name
            kind = ActionKind.BUY
            label = f"Buy {item_name}" if item_type != "remove" else "Buy remove service"
            tags = ["shop", item_type]
            actions.append(GameAction(kind=kind, label=label, payload=payload, tags=tags))
        return actions

    def _battle_progress_made(self, previous: Image.Image, current: Image.Image) -> bool:
        frame_diff = self._frame_diff_score(previous, current)
        hand_diff = self._hand_diff_score(previous, current)
        enemy_diff = self._enemy_progress_score(previous, current)
        previous_hand_count = len(self._visible_hand_drag_starts(previous))
        current_hand_count = len(self._visible_hand_drag_starts(current))
        selection_cleared = self._selection_requires_target(previous) and not self._selection_requires_target(current)
        drag_cleared = self._selected_card_drag_origin(previous) is not None and self._selected_card_drag_origin(current) is None
        if enemy_diff >= 4.0:
            return True
        if previous_hand_count > 0 and current_hand_count < previous_hand_count and hand_diff >= 4.0:
            return True
        if selection_cleared and max(frame_diff, hand_diff) >= 4.0:
            return True
        if drag_cleared and max(frame_diff, hand_diff) >= 4.0:
            return True
        return False

    def _enemy_progress_score(self, previous: Image.Image, current: Image.Image) -> float:
        previous_enemies = sorted(self._extract_battle_enemies(previous, include_hp_text=False), key=lambda enemy: enemy.x)
        current_enemies = sorted(self._extract_battle_enemies(current, include_hp_text=False), key=lambda enemy: enemy.x)
        if not previous_enemies or not current_enemies:
            return 0.0
        if len(current_enemies) < len(previous_enemies):
            return 100.0
        score = 0.0
        for previous_enemy, current_enemy in zip(previous_enemies, current_enemies):
            score = max(score, abs(float(previous_enemy.width - current_enemy.width)))
            if (
                previous_enemy.intent_damage is not None
                and current_enemy.intent_damage is not None
                and previous_enemy.intent_damage != current_enemy.intent_damage
            ):
                score = max(score, 8.0)
            if current_enemy.status_icon_count != previous_enemy.status_icon_count:
                score = max(score, 6.0)
        return score

    def _target_candidate_points(
        self,
        screenshot: Image.Image,
        *,
        selected_reference: Image.Image | None = None,
    ) -> list[tuple[int, int]]:
        candidates = self._raw_target_candidate_points(screenshot)
        preferred_enemies = self._preferred_target_enemies(selected_reference)
        if not preferred_enemies:
            return candidates[:6]
        ordered: list[tuple[int, int]] = []
        remaining = candidates[:]
        for enemy in preferred_enemies:
            if not remaining:
                break
            best = min(
                remaining,
                key=lambda point: self._target_point_enemy_distance(point, enemy),
            )
            ordered.append(best)
            remaining.remove(best)
        ordered.extend(remaining)
        return ordered[:6]

    def _raw_target_candidate_points(self, screenshot: Image.Image) -> list[tuple[int, int]]:
        candidates: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        self._update_scale(screenshot.size)
        for point in self._battle_enemy_body_target_points(screenshot):
            normalized = (int(point[0]), int(point[1]))
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        for point in self._infer_enemy_target_points(screenshot):
            normalized = (int(point[0]), int(point[1]))
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        for point in self._fixed_target_candidate_points():
            scaled = (
                round(point[0] * self._scale_x),
                round(point[1] * self._scale_y),
            )
            normalized = (int(scaled[0]), int(scaled[1]))
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        return candidates[:6]

    def _preferred_target_enemies(self, selected_reference: Image.Image | None) -> list[EnemyState]:
        state = self._last_state
        if state is None or state.screen != ScreenKind.BATTLE or not state.enemies:
            return []
        card_damage = self._selected_card_damage(selected_reference)
        enemies = [replace(enemy) for enemy in state.enemies]
        enemies.sort(
            key=lambda enemy: (
                0
                if card_damage > 0
                and enemy.hp is not None
                and (enemy.hp + (enemy.block or 0)) <= card_damage
                else 1,
                (enemy.hp + (enemy.block or 0)) if enemy.hp is not None else 999,
                -(enemy.intent_damage or 0),
                enemy.x,
            )
        )
        return enemies

    def _selected_card_damage(self, selected_reference: Image.Image | None) -> int:
        if selected_reference is None or self._last_state is None:
            return 0
        card_name = self._selected_card_name(selected_reference, self._last_state)
        knowledge = lookup_card_knowledge(self._last_state.character, card_name)
        return knowledge.damage if knowledge is not None else 0

    @staticmethod
    def _target_point_enemy_distance(point: tuple[int, int], enemy: EnemyState) -> float:
        enemy_center_x = enemy.x + max(1, enemy.width // 2)
        enemy_body_y = enemy.y + max(60, round(enemy.height * 8))
        return abs(point[0] - enemy_center_x) + abs(point[1] - enemy_body_y) * 0.35

    def _target_navigation_sequences(self, selected_reference: Image.Image, *, use_gamepad: bool) -> list[list[str]]:
        preferred_enemies = self._preferred_target_enemies(selected_reference)
        if not preferred_enemies or self._last_state is None or len(self._last_state.enemies) <= 1:
            if use_gamepad:
                return [
                    ["a"],
                    ["dpad_up", "a"],
                    ["dpad_up", "dpad_right", "a"],
                    ["dpad_up", "dpad_left", "a"],
                ]
            return [
                ["up", "right", "enter"],
                ["up", "left", "enter"],
                ["up", "right", "right", "enter"],
                ["up", "left", "left", "enter"],
            ]
        ordered_by_x = sorted(self._last_state.enemies, key=lambda enemy: enemy.x)
        target = preferred_enemies[0]
        try:
            target_index = ordered_by_x.index(next(enemy for enemy in ordered_by_x if enemy.x == target.x and enemy.y == target.y))
        except StopIteration:
            target_index = 0
        anchor_index = len(ordered_by_x) // 2
        horizontal_moves = abs(target_index - anchor_index)
        if target_index > anchor_index:
            horizontal_key = "dpad_right" if use_gamepad else "right"
        else:
            horizontal_key = "dpad_left" if use_gamepad else "left"
        confirm_key = "a" if use_gamepad else "enter"
        up_key = "dpad_up" if use_gamepad else "up"
        primary = [up_key] + ([horizontal_key] * horizontal_moves) + [confirm_key]
        fallback = [up_key, confirm_key]
        stay = [confirm_key]
        sequences = [primary]
        if fallback != primary:
            sequences.append(fallback)
        if stay != primary and stay != fallback:
            sequences.append(stay)
        return sequences

    def _initial_tracked_battle_energy(self) -> int | None:
        if self._last_state is None or self._last_state.screen != ScreenKind.BATTLE:
            return None
        return self._last_state.energy

    @staticmethod
    def _consume_tracked_battle_energy(
        tracked_energy: int | None,
        observation: BattleCardObservation | None,
    ) -> int | None:
        if tracked_energy is None:
            return None
        if observation is None or observation.energy_cost is None:
            return tracked_energy
        return max(0, tracked_energy - max(0, observation.energy_cost))

    @staticmethod
    def _gain_tracked_battle_block(
        tracked_block: int | None,
        observation: BattleCardObservation | None,
    ) -> int | None:
        if tracked_block is None:
            return None
        if observation is None or observation.block is None:
            return tracked_block
        return max(0, tracked_block + max(0, observation.block))

    @staticmethod
    def _fixed_target_candidate_points() -> list[tuple[int, int]]:
        return [
            (860, 450),
            (1040, 450),
            (1215, 450),
            (930, 520),
            (1116, 546),
            (1280, 520),
            (930, 360),
            (1116, 360),
            (1280, 360),
            (690, 300),
        ]

    @staticmethod
    def _battle_enemy_body_target_points(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.18))
        bottom = min(height, round(height * 0.66))
        left = max(0, round(width * 0.52))
        right = min(width, round(width * 0.95))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = (
            (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 60)
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        candidates: list[tuple[int, int]] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 1200 or area > 60000:
                continue
            if comp_width < 40 or comp_height < 40:
                continue
            if comp_width > round(region.shape[1] * 0.70) or comp_height > round(region.shape[0] * 0.70):
                continue
            center_x, center_y = centroids[index]
            candidates.append(
                (
                    left + int(round(center_x)),
                    top + int(round(center_y)),
                )
            )
        candidates = [
            point
            for point in candidates
            if round(height * 0.36) <= point[1] <= round(height * 0.58)
        ]
        candidates.sort(key=lambda point: point[0])
        return candidates

    @staticmethod
    def _infer_enemy_target_points(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.12))
        bottom = min(height, round(height * 0.68))
        left = max(0, round(width * 0.12))
        right = min(width, round(width * 0.93))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        warm_mask = (
            ((region[:, :, 0] > 145) & (region[:, :, 1] < 140) & (region[:, :, 2] < 140))
            | ((region[:, :, 0] > 170) & (region[:, :, 1] > 100) & (region[:, :, 1] < 220) & (region[:, :, 2] < 120))
        )
        histogram = warm_mask.sum(axis=0).astype(np.float32)
        if histogram.size == 0 or float(histogram.max()) < 8.0:
            return []
        window = min(21, histogram.size)
        if window % 2 == 0:
            window -= 1
        if window <= 1:
            smoothed = histogram
        else:
            smoothed = np.convolve(histogram, np.ones(window, dtype=np.float32) / window, mode="same")
        threshold = float(smoothed.max()) * 0.45
        peak_indices: list[int] = []
        for index in range(1, len(smoothed) - 1):
            if smoothed[index] < threshold:
                continue
            if smoothed[index] < smoothed[index - 1] or smoothed[index] < smoothed[index + 1]:
                continue
            if peak_indices and (index - peak_indices[-1]) < 50:
                if smoothed[index] > smoothed[peak_indices[-1]]:
                    peak_indices[-1] = index
                continue
            peak_indices.append(index)
        upper_cut = max(1, round(region.shape[0] * 0.45))
        candidates: list[tuple[int, int]] = []
        for peak in peak_indices:
            x0 = max(0, peak - 40)
            x1 = min(region.shape[1], peak + 40)
            band = warm_mask[:, x0:x1]
            upper_band = band[:upper_cut, :]
            ys, xs = np.where(upper_band)
            if xs.size < 50:
                continue
            x = left + x0 + int(np.median(xs))
            y = top + int(np.quantile(ys, 0.35))
            candidates.append((x, y))
        return candidates

    def apply_action(self, action: GameAction, *, backend: str | None = None, mode: str = "auto") -> str:
        self._require_runtime()
        previous_state = self._last_state or self.current_state()
        previous_screenshot = self._last_screenshot.copy() if self._last_screenshot is not None else self._capture_window_image()
        definition = self._find_action_definition(action)
        selected_backend = self._preferred_backend_for_definition(definition, backend)
        self._record_action(action)
        used_backend = self._execute_action(definition, backend=selected_backend, mode=mode)
        self._wait_for_change(previous_state, previous_screenshot)
        self._last_state = None
        return used_backend

    def is_run_over(self) -> bool:
        state = self.current_state()
        return state.screen == ScreenKind.GAME_OVER

    def run_summary(self) -> RunSummary:
        state = self.current_state()
        return RunSummary(
            character=state.character,
            won=self._won,
            act_reached=self._act_reached,
            floor_reached=self._floor_reached,
            score=(1000 if self._won else 0) + self._floor_reached * 10,
            deck=[DeckCard(card.name, upgraded=card.upgraded, tags=card.tags[:]) for card in self._deck_cards],
            relics=self._relics[:],
            picked_cards=self._picked_cards[:],
            skipped_cards=self._skipped_cards[:],
            path=self._path[:],
            strategy_tags=sorted(self._strategy_tags),
        )

    def probe(self) -> GameState:
        return self.current_state()

    def probe_fast(self) -> GameState:
        if not self._started:
            self.start_run(focus=False)
        return self._observe_live_state(read_metrics=False)

    def capture_image(self):
        if not self._started:
            self.start_run(focus=False)
        return self._capture_window_image_with_retry()

    def last_screenshot(self):
        if self._last_screenshot is None:
            return None
        return self._last_screenshot.copy()

    def _observe_live_state(self, *, read_metrics: bool) -> GameState:
        screenshot = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
        state = self.inspect_image(screenshot, read_metrics=read_metrics)
        if state.screen != ScreenKind.UNKNOWN:
            return state
        for _attempt in range(2):
            time.sleep(0.12)
            screenshot = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            candidate = self.inspect_image(screenshot, read_metrics=read_metrics)
            if candidate.screen != ScreenKind.UNKNOWN:
                return candidate
        return state

    def capture_image_retry(self, *, attempts: int = 6, backoff_seconds: float = 0.12):
        if not self._started:
            self.start_run(focus=False)
        return self._capture_window_image_with_retry(attempts=attempts, backoff_seconds=backoff_seconds)

    def send_gamepad_buttons(
        self,
        buttons: list[str],
        *,
        settle_ms: int = 0,
        hold_ms: int = 120,
        gap_ms: int = 110,
    ) -> None:
        if not self._started:
            self.start_run(focus=False)
        self._press_gamepad_sequence(buttons, settle_ms=settle_ms, hold_ms=hold_ms, gap_ms=gap_ms)

    def last_anchor_scores(self) -> dict[str, float]:
        return {name: match.score for name, match in self._last_matches.items()}

    def last_metrics(self) -> dict[str, int | tuple[int, int] | str | None]:
        return dict(self._last_metrics)

    def last_metric_sources(self) -> dict[str, str]:
        return dict(self._last_metric_sources)

    def last_memory_probe(self) -> dict[str, object]:
        return dict(self._last_memory_probe_data)

    @staticmethod
    def _state_source_from_metric_sources(metric_sources: dict[str, str]) -> StateSource:
        if not metric_sources:
            return StateSource.OCR
        canonical_sources = {
            "memory" if source == "memory" else "ocr"
            for source in metric_sources.values()
            if isinstance(source, str) and source
        }
        if not canonical_sources:
            return StateSource.OCR
        if canonical_sources == {"memory"}:
            return StateSource.MEMORY
        if canonical_sources == {"ocr"}:
            return StateSource.OCR
        return StateSource.HYBRID

    def probe_memory(self, *, screen: ScreenKind | None = None) -> dict[str, object]:
        if not self._started:
            self.start_run(focus=False)
        selected_screen = screen
        if selected_screen is None:
            screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
            selected_screen = self.inspect_image(screenshot, read_metrics=False).screen
        snapshot = self._probe_memory_snapshot_for_screen(selected_screen)
        if snapshot is None:
            payload = dict(self._last_memory_probe_data)
            payload["screen"] = selected_screen.value
            return payload
        payload = snapshot.to_dict()
        payload["screen"] = selected_screen.value
        self._last_memory_probe_data = payload
        return dict(payload)

    def capability_report(self) -> CapabilityReport:
        runtime = self._require_runtime()
        return runtime.capability_report()

    def runtime_diagnostics(self) -> dict[str, object]:
        runtime = self._require_runtime()
        return runtime.diagnostics()

    def _detect_screen(self, screenshot) -> ScreenKind:
        best_screen = ScreenKind.UNKNOWN
        best_score = -1.0
        self._last_matches = {}
        for anchor in self.profile.anchors:
            if not anchor.template_path.exists():
                continue
            match = match_template(
                screenshot,
                template_path=anchor.template_path,
                region=anchor.region.scaled(self._scale_x, self._scale_y) if anchor.scale_region else anchor.region,
                threshold=anchor.threshold,
                scale_x=self._scale_x if anchor.scale_template else 1.0,
                scale_y=self._scale_y if anchor.scale_template else 1.0,
            )
            self._last_matches[anchor.name] = match
            if match.found and match.score > best_score:
                best_screen = anchor.screen
                best_score = match.score
        if self._looks_like_game_over(screenshot):
            return ScreenKind.GAME_OVER
        reward_menu_match = self._last_matches.get("reward_menu_anchor")
        reward_menu_single_match = self._last_matches.get("reward_menu_single_anchor")
        battle_family_score = max(
            (match.score for name, match in self._last_matches.items() if name.startswith("battle_anchor")),
            default=-1.0,
        )
        if (reward_menu_match and reward_menu_match.found) or (reward_menu_single_match and reward_menu_single_match.found):
            return self._detect_reward_variant(screenshot)
        if best_screen in {ScreenKind.REWARD_CARDS, ScreenKind.REWARD_GOLD_ONLY}:
            return self._detect_reward_variant(screenshot)
        if self._boss_relic_actions(screenshot):
            return ScreenKind.BOSS_RELIC
        if self._looks_like_map_legend(screenshot):
            return ScreenKind.MAP
        if self._looks_like_rest_screen(screenshot):
            return ScreenKind.REST
        neow_dialog_score = self._last_matches.get("neow_dialog_anchor")
        card_grid_score = self._last_matches.get("card_grid_anchor")
        confirm_context = (
            (neow_dialog_score is not None and neow_dialog_score.found and neow_dialog_score.score >= 0.55)
            or (card_grid_score is not None and card_grid_score.score >= 0.22)
            or self._looks_like_card_grid(screenshot)
        )
        if self._looks_like_transform_confirm_popup(screenshot):
            return ScreenKind.CONFIRM_POPUP
        if best_screen in {ScreenKind.CARD_GRID, ScreenKind.NEOW_DIALOG} and confirm_context and self._looks_like_confirm_popup(screenshot):
            return ScreenKind.CONFIRM_POPUP
        if best_screen == ScreenKind.CARD_GRID and battle_family_score >= 0.44 and self._looks_like_battle_hud(screenshot):
            return ScreenKind.BATTLE
        if best_screen == ScreenKind.CONTINUE and self._looks_like_character_select(screenshot):
            return ScreenKind.CHARACTER_SELECT
        if best_screen == ScreenKind.UNKNOWN:
            if self._looks_like_transform_confirm_popup(screenshot):
                return ScreenKind.CONFIRM_POPUP
            if confirm_context and self._looks_like_confirm_popup(screenshot):
                return ScreenKind.CONFIRM_POPUP
            if self._looks_like_game_over(screenshot):
                return ScreenKind.GAME_OVER
            if self._looks_like_reward_cards(screenshot):
                return ScreenKind.REWARD_CARDS
            if self._reward_relic_actions(screenshot):
                return ScreenKind.REWARD_RELIC
            if self._reward_potion_actions(screenshot):
                return ScreenKind.REWARD_POTION
            if battle_family_score >= 0.28 and self._looks_like_battle_hud(screenshot):
                return ScreenKind.BATTLE
            map_score = self._last_matches.get("map_anchor_live_v9")
            if map_score is not None and map_score.found and map_score.score >= 0.07:
                return ScreenKind.MAP
            if self._looks_like_map_legend(screenshot):
                return ScreenKind.MAP
            if self._looks_like_rest_screen(screenshot):
                return ScreenKind.REST
            if self._looks_like_shop_card_popup(screenshot):
                return ScreenKind.SHOP
            if self._looks_like_shop_screen(screenshot):
                return ScreenKind.SHOP
            card_grid_score = self._last_matches.get("card_grid_anchor")
            if card_grid_score is not None and card_grid_score.found and card_grid_score.score >= 0.22:
                return ScreenKind.CARD_GRID
            if self._looks_like_card_grid(screenshot):
                return ScreenKind.CARD_GRID
            character_select_score = self._last_matches.get("character_select_anchor")
            if character_select_score is not None and character_select_score.found and character_select_score.score >= 0.20:
                return ScreenKind.CHARACTER_SELECT
            if self._looks_like_character_select(screenshot):
                return ScreenKind.CHARACTER_SELECT
            neow_choice_score = self._last_matches.get("neow_choice_anchor")
            if neow_choice_score is not None and neow_choice_score.score >= 0.48 and self._looks_like_neow_panel(screenshot):
                return ScreenKind.NEOW_CHOICE
            neow_dialog_score = self._last_matches.get("neow_dialog_anchor")
            if neow_dialog_score is not None and neow_dialog_score.score >= 0.30 and self._looks_like_neow_panel(screenshot):
                return ScreenKind.NEOW_DIALOG
            if self._looks_like_menu(screenshot):
                return ScreenKind.MENU
            if self._looks_like_mode_select(screenshot):
                return ScreenKind.MODE_SELECT
            if self._navigation_arrow_point(screenshot, direction="right") is not None:
                return ScreenKind.CONTINUE
            if self._event_option_points(screenshot):
                return ScreenKind.EVENT
        return best_screen

    def _read_metrics(self, screenshot) -> dict[str, int | tuple[int, int] | str | None]:
        metrics, sources = self._read_metrics_with_sources(screenshot)
        self._last_ocr_metric_sources = dict(sources)
        return metrics

    def _read_metrics_with_sources(self, screenshot) -> tuple[dict[str, int | tuple[int, int] | str | None], dict[str, str]]:
        metrics: dict[str, int | tuple[int, int] | str | None] = {}
        for definition in self.profile.text_regions:
            try:
                raw_text = extract_text_fast(
                    screenshot,
                    replace(definition, region=definition.region.scaled(self._scale_x, self._scale_y)),
                )
            except RuntimeError:
                if self.strict_ocr:
                    raise
                raw_text = ""
            metrics[definition.name] = parse_text_value(raw_text, definition.parser)
        for key, value in self._small_window_metric_fallbacks(screenshot).items():
            metrics[key] = value
        return self._sanitize_metrics_with_sources(metrics)

    def _small_window_metric_fallbacks(self, screenshot: Image.Image) -> dict[str, int | tuple[int, int] | str | None]:
        width, height = screenshot.size
        if width > 1150 or height > 650:
            return {}
        fallbacks: dict[str, int | tuple[int, int] | str | None] = {}
        hp_region = Rect(20, 0, 130, 40)
        gold_region = Rect(130, 0, 70, 34)
        if width > 980 or height > 580:
            hp_region = Rect(20, 0, 140, 42)
            gold_region = Rect(160, 0, 90, 44)
        hp_value = self._read_metric_with_region(
            screenshot,
            TextRegionDefinition("hp_small_window", hp_region, whitelist="0123456789/", parser="pair"),
        )
        hp_value = self._normalize_small_window_hp_pair(hp_value)
        if hp_value is not None:
            fallbacks["hp"] = hp_value
        gold_value = self._read_metric_with_region(
            screenshot,
            TextRegionDefinition("gold_small_window", gold_region, whitelist="0123456789", parser="int"),
        )
        if gold_value is not None:
            fallbacks["gold"] = gold_value
        energy_value = self._read_metric_with_region(
            screenshot,
            TextRegionDefinition("energy_small_window", Rect(15, max(0, height - 110), 95, 100), whitelist="0123456789/", parser="int"),
        )
        if energy_value is not None:
            fallbacks["energy"] = energy_value
        inferred_floor = max(self._floor_reached, len(self._path))
        if inferred_floor > 0:
            fallbacks["floor"] = inferred_floor
        return fallbacks

    @staticmethod
    def _normalize_small_window_hp_pair(value: int | tuple[int, int] | str | None) -> tuple[int, int] | None:
        if not isinstance(value, tuple) or len(value) != 2:
            return None
        current_hp, max_hp = value
        if not isinstance(current_hp, int) or not isinstance(max_hp, int):
            return None
        if current_hp <= max_hp:
            return value
        if current_hp >= 100:
            reduced = current_hp % 100
            if 0 <= reduced <= max_hp:
                return reduced, max_hp
        return value

    def _read_metric_with_region(
        self,
        screenshot: Image.Image,
        definition: TextRegionDefinition,
    ) -> int | tuple[int, int] | str | None:
        try:
            raw_text = extract_text_fast(screenshot, definition)
        except RuntimeError:
            if self.strict_ocr:
                raise
            return None
        return parse_text_value(raw_text, definition.parser)

    def _metrics_for_screen(
        self,
        screenshot,
        screen: ScreenKind,
        *,
        read_metrics: bool,
    ) -> dict[str, int | tuple[int, int] | str | None]:
        if not read_metrics:
            return dict(self._last_metrics)
        metric_screens = self._metric_screens()
        if screen not in metric_screens:
            return dict(self._last_metrics)
        if (
            screen == ScreenKind.BATTLE
            and self._last_metrics
            and (time.time() - self._last_metric_read_at) < 0.75
            and not self._should_refresh_battle_metrics(screenshot)
        ):
            return dict(self._last_metrics)
        memory_snapshot = self._probe_memory_snapshot_for_screen(screen)
        if memory_snapshot is not None:
            memory_metrics, memory_sources = self._metrics_from_memory_snapshot(memory_snapshot)
            if self._memory_metrics_cover_screen(memory_metrics, screen):
                self._last_metric_sources = dict(memory_sources)
                self._last_metric_read_at = time.time()
                return memory_metrics
        metrics = self._read_metrics(screenshot)
        sources = dict(self._last_ocr_metric_sources)
        metrics, sources = self._merge_memory_metrics(screen, metrics, sources, snapshot=memory_snapshot)
        self._last_metric_sources = dict(sources)
        self._last_metric_read_at = time.time()
        return metrics

    def _metrics_from_memory_snapshot(
        self,
        snapshot: MemoryReadSnapshot,
    ) -> tuple[dict[str, int | tuple[int, int] | str | None], dict[str, str]]:
        metrics: dict[str, int | tuple[int, int] | str | None] = {}
        sources: dict[str, str] = {}
        hp_pair = self._memory_hp_pair(snapshot.values)
        if hp_pair is not None and self._is_valid_metric_value("hp", hp_pair):
            metrics["hp"] = hp_pair
            sources["hp"] = "memory"
            sources["max_hp"] = "memory"
        for name in ("gold", "energy", "max_energy", "block", "floor", "ascension"):
            value = snapshot.values.get(name)
            if self._is_valid_metric_value(name, value):
                metrics[name] = value
                sources[name] = "memory"
        floor_value = metrics.get("floor")
        if isinstance(floor_value, int):
            metrics["act"] = self._infer_act_from_floor(floor_value)
            sources["act"] = "memory"
        return metrics, sources

    def _memory_metrics_cover_screen(
        self,
        metrics: dict[str, int | tuple[int, int] | str | None],
        screen: ScreenKind,
    ) -> bool:
        required = {"hp", "gold", "floor"}
        if screen == ScreenKind.BATTLE:
            required.add("energy")
        return all(name in metrics for name in required)

    def _should_refresh_battle_metrics(self, screenshot: Image.Image) -> bool:
        return (
            self._looks_like_zero_energy(screenshot)
            or self._selected_card_drag_origin(screenshot) is not None
            or self._selection_requires_target(screenshot)
        )

    @staticmethod
    def _metric_screens() -> set[ScreenKind]:
        return {
            ScreenKind.BATTLE,
            ScreenKind.REWARD_MENU,
            ScreenKind.REWARD_CARDS,
            ScreenKind.REWARD_RELIC,
            ScreenKind.REWARD_POTION,
            ScreenKind.REWARD_GOLD_ONLY,
            ScreenKind.MAP,
            ScreenKind.REST,
            ScreenKind.SHOP,
            ScreenKind.BOSS_RELIC,
            ScreenKind.GAME_OVER,
        }

    def _sanitize_metrics(self, metrics: dict[str, int | tuple[int, int] | str | None]) -> dict[str, int | tuple[int, int] | str | None]:
        return self._sanitize_metrics_with_sources(metrics)[0]

    def _sanitize_metrics_with_sources(
        self,
        metrics: dict[str, int | tuple[int, int] | str | None],
    ) -> tuple[dict[str, int | tuple[int, int] | str | None], dict[str, str]]:
        sanitized = dict(metrics)
        previous = self._last_metrics
        sources: dict[str, str] = {}

        hp_value = sanitized.get("hp")
        if isinstance(hp_value, tuple):
            hp, max_hp = hp_value
            if hp < 0 or max_hp <= 0 or hp > max_hp or max_hp > 200:
                sanitized["hp"] = previous.get("hp")
                if previous.get("hp") is not None:
                    sources["hp"] = "cache"
                    sources["max_hp"] = "cache"
            else:
                sources["hp"] = "ocr"
                sources["max_hp"] = "ocr"
        elif previous.get("hp") is not None:
            sanitized["hp"] = previous.get("hp")
            sources["hp"] = "cache"
            sources["max_hp"] = "cache"

        gold = sanitized.get("gold")
        if isinstance(gold, int) and 0 <= gold <= 9999:
            sources["gold"] = "ocr"
        else:
            sanitized["gold"] = previous.get("gold")
            if previous.get("gold") is not None:
                sources["gold"] = "cache"

        energy = sanitized.get("energy")
        if isinstance(energy, int) and 0 <= energy <= 5:
            sources["energy"] = "ocr"
        else:
            sanitized["energy"] = previous.get("energy")
            if previous.get("energy") is not None:
                sources["energy"] = "cache"

        floor = sanitized.get("floor")
        if isinstance(floor, int) and 0 <= floor <= 60:
            sources["floor"] = "ocr"
        else:
            sanitized["floor"] = previous.get("floor")
            if previous.get("floor") is not None:
                sources["floor"] = "cache"

        act = sanitized.get("act")
        if isinstance(act, int) and 0 <= act <= 5:
            sources["act"] = "ocr"
        elif act is not None:
            sanitized["act"] = previous.get("act")
            if previous.get("act") is not None:
                sources["act"] = "cache"

        ascension = sanitized.get("ascension")
        if isinstance(ascension, int) and 0 <= ascension <= 20:
            sources["ascension"] = "ocr"
        elif ascension is not None:
            sanitized["ascension"] = previous.get("ascension")
            if previous.get("ascension") is not None:
                sources["ascension"] = "cache"

        return sanitized, sources

    def _merge_memory_metrics(
        self,
        screen: ScreenKind,
        metrics: dict[str, int | tuple[int, int] | str | None],
        sources: dict[str, str],
        *,
        snapshot: MemoryReadSnapshot | None = None,
    ) -> tuple[dict[str, int | tuple[int, int] | str | None], dict[str, str]]:
        if snapshot is None:
            snapshot = self._probe_memory_snapshot_for_screen(screen)
        if snapshot is None:
            return metrics, sources
        merged = dict(metrics)
        merged_sources = dict(sources)
        memory_values = snapshot.values
        hp_pair = self._memory_hp_pair(memory_values)
        if hp_pair is not None and self._is_valid_metric_value("hp", hp_pair):
            merged["hp"] = hp_pair
            merged_sources["hp"] = "memory"
            merged_sources["max_hp"] = "memory"
        for name in ("gold", "energy", "max_energy", "block", "floor", "ascension"):
            value = memory_values.get(name)
            if self._is_valid_metric_value(name, value):
                merged[name] = value
                merged_sources[name] = "memory"
        act_value = memory_values.get("act")
        if self._is_valid_metric_value("act", act_value):
            merged["act"] = act_value
            merged_sources["act"] = "memory"
        elif "floor" in merged:
            floor_value = merged.get("floor")
            if isinstance(floor_value, int):
                merged["act"] = self._infer_act_from_floor(floor_value)
                merged_sources["act"] = merged_sources.get("floor", merged_sources.get("act", "derived"))
        return merged, merged_sources

    @staticmethod
    def _memory_hp_pair(memory_values: dict[str, int | None]) -> tuple[int, int] | None:
        hp = memory_values.get("hp")
        max_hp = memory_values.get("max_hp")
        if isinstance(hp, int) and isinstance(max_hp, int):
            return (hp, max_hp)
        return None

    @staticmethod
    def _is_valid_metric_value(name: str, value: object) -> bool:
        if name == "hp":
            if not isinstance(value, tuple) or len(value) != 2:
                return False
            current_hp, max_hp = value
            return isinstance(current_hp, int) and isinstance(max_hp, int) and 0 <= current_hp <= max_hp <= 200
        if name == "gold":
            return isinstance(value, int) and 0 <= value <= 9999
        if name == "energy":
            return isinstance(value, int) and 0 <= value <= 100
        if name == "max_energy":
            return isinstance(value, int) and 0 <= value <= 100
        if name == "block":
            return isinstance(value, int) and 0 <= value <= 999
        if name == "floor":
            return isinstance(value, int) and 0 <= value <= 60
        if name == "act":
            return isinstance(value, int) and 0 <= value <= 5
        if name == "ascension":
            return isinstance(value, int) and 0 <= value <= 20
        return False

    def _probe_memory_snapshot_for_screen(self, screen: ScreenKind) -> MemoryReadSnapshot | None:
        config = self.profile.memory_read
        if not config.enabled:
            self._last_memory_probe_data = {
                "enabled": False,
                "module": config.module,
                "refresh_ms": config.refresh_ms,
                "screen": screen.value,
                "values": {},
                "fields": {},
                "errors": ["memory_read_disabled"],
            }
            return None
        if self._runtime is None:
            self._last_memory_probe_data = {
                "enabled": True,
                "module": config.module,
                "refresh_ms": config.refresh_ms,
                "screen": screen.value,
                "values": {},
                "fields": {},
                "errors": ["runtime_not_started"],
            }
            return None
        fields = [field for field in config.fields if self._memory_field_allowed_on_screen(field.name, field.screens, screen)]
        if not fields:
            managed_snapshot = self._probe_managed_snapshot_for_screen(screen)
            if managed_snapshot is not None:
                payload = managed_snapshot.to_dict()
                payload["enabled"] = True
                payload["provider"] = "managed"
                payload["screen"] = screen.value
                payload.update(self._managed_probe_detail_payload())
                self._last_memory_probe_data = payload
                return managed_snapshot
            self._last_memory_probe_data = {
                "enabled": True,
                "module": config.module,
                "refresh_ms": config.refresh_ms,
                "provider": "managed",
                "screen": screen.value,
                "values": {},
                "fields": {},
                "errors": ["no_fields_for_screen"],
            }
            return None
        reader = self._memory_reader_for_runtime()
        if reader is None:
            return None
        snapshot = reader.read_fields(fields)
        payload = snapshot.to_dict()
        payload["enabled"] = True
        payload["provider"] = "raw"
        payload["screen"] = screen.value
        self._last_memory_probe_data = payload
        return snapshot

    def _probe_managed_snapshot_for_screen(self, screen: ScreenKind) -> MemoryReadSnapshot | None:
        if screen not in self._metric_screens():
            return None
        if self._runtime is None:
            return None
        runtime_target = getattr(self._runtime, "target", None)
        runtime_pid = getattr(runtime_target, "pid", None)
        if not isinstance(runtime_pid, int) or runtime_pid <= 0:
            self._last_memory_probe_data = {
                "enabled": True,
                "module": self.profile.memory_read.module,
                "refresh_ms": self.profile.memory_read.refresh_ms,
                "provider": "managed",
                "screen": screen.value,
                "values": {},
                "fields": {},
                "errors": ["runtime_target_missing"],
            }
            return None
        refresh_ms = max(0, int(self.profile.memory_read.refresh_ms))
        if (
            self._last_managed_snapshot is not None
            and refresh_ms > 0
            and (time.time() - self._last_managed_read_at) * 1000 < refresh_ms
        ):
            snapshot = self._last_managed_snapshot
        else:
            probe = self._managed_probe_for_runtime()
            if probe is None:
                return None
            try:
                snapshot = probe.probe_pid(runtime_pid)
            except ManagedProbeError as exc:
                self._last_memory_probe_data = {
                    "enabled": True,
                    "module": self.profile.memory_read.module,
                    "refresh_ms": refresh_ms,
                    "provider": "managed",
                    "screen": screen.value,
                    "values": {},
                    "fields": {},
                    "errors": [f"managed_probe_error:{exc}"],
                }
                return None
            self._last_managed_snapshot = snapshot
            self._last_managed_read_at = time.time()
        return self._managed_probe_snapshot_to_memory_snapshot(snapshot)

    def _managed_probe_for_runtime(self) -> ManagedSnapshotProbe | None:
        if self._runtime is None:
            return None
        if self._managed_probe is None:
            self._managed_probe = ManagedSnapshotProbe(workspace_dir=self.profile_path.parent.parent)
        return self._managed_probe

    def _managed_probe_detail_payload(self) -> dict[str, object]:
        snapshot = self._last_managed_snapshot
        if snapshot is None:
            return {}
        return {
            "player_powers": [power.to_dict() for power in snapshot.player_powers],
            "enemies": [enemy.to_dict() for enemy in snapshot.enemies],
        }

    @staticmethod
    def _managed_probe_snapshot_to_memory_snapshot(snapshot: ManagedProbeSnapshot) -> MemoryReadSnapshot:
        values = {
            "hp": snapshot.hp,
            "max_hp": snapshot.max_hp,
            "gold": snapshot.gold,
            "energy": snapshot.energy,
            "max_energy": snapshot.max_energy,
            "block": snapshot.block,
            "floor": snapshot.floor,
            "act": None,
            "ascension": snapshot.ascension,
        }
        fields = {
            name: MemoryFieldResult(name=name, value=value if isinstance(value, int) else None, source="memory")
            for name, value in values.items()
            if name != "act"
        }
        return MemoryReadSnapshot(
            pid=snapshot.pid,
            module="managed_probe",
            values=values,
            fields=fields,
            errors=[],
            captured_at=time.time(),
            cached=False,
        )

    def _memory_reader_for_runtime(self) -> ProcessMemoryReader | None:
        if self._runtime is None:
            return None
        if self._memory_reader is None:
            self._memory_reader = ProcessMemoryReader(
                pid=self._runtime.target.pid,
                module=self.profile.memory_read.module,
                refresh_ms=self.profile.memory_read.refresh_ms,
            )
        return self._memory_reader

    def _memory_field_allowed_on_screen(
        self,
        name: str,
        screens: list[ScreenKind],
        screen: ScreenKind,
    ) -> bool:
        if screen not in self._metric_screens():
            return False
        if screens and screen not in screens:
            return False
        if name == "energy":
            return screen == ScreenKind.BATTLE
        return True

    @staticmethod
    def _infer_act_from_floor(floor: int) -> int:
        if floor <= 0:
            return 0
        if floor <= 17:
            return 1
        if floor <= 34:
            return 2
        if floor <= 51:
            return 3
        return 4

    @staticmethod
    def _looks_like_battle_hud(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        orb_region = rgb[round(height * 0.72):round(height * 0.98), 0:round(width * 0.18)]
        orb_ratio = 0.0
        if orb_region.size != 0:
            orb_mask = (
                (orb_region[:, :, 0] > 170)
                & (orb_region[:, :, 1] > 70)
                & (orb_region[:, :, 1] < 180)
                & (orb_region[:, :, 2] < 80)
            )
            orb_ratio = float(orb_mask.mean())
        if orb_ratio >= 0.02:
            return True
        end_turn_region = rgb[
            round(height * 0.72):round(height * 0.98),
            round(width * 0.78):round(width * 0.99),
        ]
        if end_turn_region.size == 0:
            return False
        end_turn_mask = (
            (end_turn_region[:, :, 0] > 120)
            & (end_turn_region[:, :, 1] > 100)
            & (end_turn_region[:, :, 2] < 90)
        )
        teal_mask = (
            (end_turn_region[:, :, 2] > 100)
            & (end_turn_region[:, :, 1] > 90)
            & (end_turn_region[:, :, 0] < 110)
        )
        if float(end_turn_mask.mean()) < 0.04 and float(teal_mask.mean()) < 0.015:
            return False
        card_region = rgb[
            round(height * 0.72):round(height * 0.98),
            round(width * 0.18):round(width * 0.78),
        ]
        if card_region.size == 0:
            return False
        hsv = cv2.cvtColor(card_region, cv2.COLOR_RGB2HSV)
        saturated_cards = (
            (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 80)
            & (
                (hsv[:, :, 0] < 18)
                | (hsv[:, :, 0] > 150)
                | ((hsv[:, :, 0] > 78) & (hsv[:, :, 0] < 110))
            )
        )
        return float(saturated_cards.mean()) >= 0.06

    @staticmethod
    def _looks_like_mode_select(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.12))
        bottom = min(height, round(height * 0.90))
        left = max(0, round(width * 0.12))
        right = min(width, round(width * 0.88))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        mask = (gray > 58).astype(np.uint8) * 255
        kernel_w = max(3, round(region.shape[1] * 0.025))
        kernel_h = max(3, round(region.shape[0] * 0.045))
        kernel = np.ones((kernel_h, kernel_w), dtype=np.uint8)
        merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(merged, 8)
        candidates: list[tuple[float, float]] = []
        region_area = float(region.shape[0] * region.shape[1])
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < region_area * 0.04 or area > region_area * 0.28:
                continue
            if comp_width < region.shape[1] * 0.10 or comp_width > region.shape[1] * 0.30:
                continue
            if comp_height < region.shape[0] * 0.35 or comp_height > region.shape[0] * 0.92:
                continue
            center_x, center_y = centroids[index]
            candidates.append((float(center_x), float(center_y)))
        if len(candidates) < 3:
            return False
        candidates.sort(key=lambda point: point[0])
        spread = candidates[-1][0] - candidates[0][0]
        if spread < region.shape[1] * 0.38:
            return False
        return True

    @staticmethod
    def _looks_like_menu(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        logo_region = rgb[
            round(height * 0.08):round(height * 0.58),
            round(width * 0.18):round(width * 0.66),
        ]
        menu_region = rgb[
            round(height * 0.58):round(height * 0.94),
            round(width * 0.24):round(width * 0.56),
        ]
        if logo_region.size == 0 or menu_region.size == 0:
            return False
        gold_mask = (
            (logo_region[:, :, 0] > 140)
            & (logo_region[:, :, 1] > 110)
            & (logo_region[:, :, 2] < 110)
        )
        cyan_mask = (
            (logo_region[:, :, 2] > 120)
            & (logo_region[:, :, 1] > 90)
            & (logo_region[:, :, 0] < 120)
        )
        menu_gray = cv2.cvtColor(menu_region, cv2.COLOR_RGB2GRAY)
        bright_ratio = float((menu_gray > 110).mean())
        dark_ratio = float((menu_gray < 70).mean())
        return (
            float(gold_mask.mean()) >= 0.06
            and float(cyan_mask.mean()) >= 0.03
            and 0.015 <= bright_ratio <= 0.09
            and 0.12 <= dark_ratio <= 0.35
        )

    @staticmethod
    def _looks_like_shop_card_popup(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        center = rgb[
            round(height * 0.08):round(height * 0.88),
            round(width * 0.28):round(width * 0.74),
        ]
        if center.size == 0:
            return False
        warm_mask = (
            (center[:, :, 0] > 90)
            & (center[:, :, 1] > 60)
            & (center[:, :, 1] < 200)
            & (center[:, :, 2] < 170)
        )
        top_banner = center[: max(1, round(center.shape[0] * 0.18)), :]
        banner_mask = (
            (top_banner[:, :, 2] > 130)
            & (top_banner[:, :, 1] > 110)
            & (top_banner[:, :, 0] < 120)
        )
        dim_corners = np.concatenate(
            [
                rgb[: round(height * 0.20), : round(width * 0.18)].reshape(-1, 3),
                rgb[: round(height * 0.20), round(width * 0.82) :].reshape(-1, 3),
            ],
            axis=0,
        )
        if dim_corners.size == 0:
            return False
        dim_ratio = float((dim_corners.mean(axis=1) < 30).mean())
        return float(warm_mask.mean()) >= 0.06 and float(banner_mask.mean()) >= 0.06 and dim_ratio >= 0.70

    @staticmethod
    def _looks_like_shop_screen(screenshot: Image.Image) -> bool:
        left_arrow = WindowsStsAdapter._navigation_arrow_point(screenshot, direction="left")
        if left_arrow is None:
            return False
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        board = rgb[
            round(height * 0.10):round(height * 0.90),
            round(width * 0.12):round(width * 0.90),
        ]
        if board.size == 0:
            return False
        teal_mask = (
            (board[:, :, 2] > 90)
            & (board[:, :, 1] > 90)
            & (board[:, :, 0] < 150)
        )
        price_mask = (
            (board[:, :, 0] > 170)
            & (board[:, :, 1] > 110)
            & (board[:, :, 2] < 120)
        )
        return float(teal_mask.mean()) >= 0.28 and float(price_mask.mean()) >= 0.01

    @staticmethod
    def _looks_like_shop_continue_room(screenshot: Image.Image) -> bool:
        if WindowsStsAdapter._navigation_arrow_point(screenshot, direction="right") is None:
            return False
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        npc_region = rgb[
            round(height * 0.40):round(height * 0.86),
            round(width * 0.60):round(width * 0.86),
        ]
        if npc_region.size == 0:
            return False
        hood_mask = (
            (npc_region[:, :, 2] > 120)
            & (npc_region[:, :, 1] > 100)
            & (npc_region[:, :, 0] < 130)
        )
        warm_room = rgb[
            round(height * 0.08):round(height * 0.90),
            round(width * 0.08):round(width * 0.92),
        ]
        warm_mask = (
            (warm_room[:, :, 0] > 90)
            & (warm_room[:, :, 1] < 130)
            & (warm_room[:, :, 2] < 130)
        )
        return float(hood_mask.mean()) >= 0.03 and float(warm_mask.mean()) >= 0.07

    @staticmethod
    def _looks_like_character_select(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        warm_region = rgb[:, round(width * 0.38):width]
        if warm_region.size == 0:
            return False
        warm_mask = (
            (warm_region[:, :, 0] > 85)
            & (warm_region[:, :, 1] < 135)
            & (warm_region[:, :, 2] < 110)
        )
        warm_ratio = float(warm_mask.mean())
        confirm_region = rgb[
            round(height * 0.60):round(height * 0.88),
            round(width * 0.90):width,
        ]
        if confirm_region.size == 0:
            return False
        confirm_mask = (
            (confirm_region[:, :, 2] > 110)
            & (confirm_region[:, :, 1] > 95)
            & (confirm_region[:, :, 0] < 120)
        )
        confirm_ratio = float(confirm_mask.mean())
        return warm_ratio >= 0.28 and confirm_ratio >= 0.02

    @staticmethod
    def _looks_like_neow_panel(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        region = rgb[
            round(height * 0.45):round(height * 0.82),
            round(width * 0.22):round(width * 0.78),
        ]
        if region.size == 0:
            return False
        blue_mask = (
            (region[:, :, 2] > 110)
            & (region[:, :, 1] > 80)
            & (region[:, :, 0] < 90)
        )
        return float(blue_mask.mean()) >= 0.18

    @staticmethod
    def _looks_like_confirm_popup(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        button_region = rgb[
            round(height * 0.72):round(height * 0.97),
            round(width * 0.85):round(width * 0.99),
        ]
        if button_region.size == 0:
            return False
        blue_mask = (
            (button_region[:, :, 2] > 110)
            & (button_region[:, :, 1] > 95)
            & (button_region[:, :, 0] < 120)
        )
        return float(blue_mask.mean()) >= 0.12

    @staticmethod
    def _looks_like_transform_confirm_popup(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        yellow_region = rgb[
            round(height * 0.44):round(height * 0.58),
            round(width * 0.42):round(width * 0.58),
        ]
        if yellow_region.size == 0:
            return False
        yellow_mask = (
            (yellow_region[:, :, 0] > 160)
            & (yellow_region[:, :, 1] > 130)
            & (yellow_region[:, :, 2] < 120)
        )
        if float(yellow_mask.mean()) < 0.04:
            return False
        if WindowsStsAdapter._navigation_arrow_point(screenshot, direction="left") is None:
            return False

        region = rgb[
            round(height * 0.08):round(height * 0.88),
            round(width * 0.04):round(width * 0.96),
        ]
        if region.size == 0:
            return False
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = (
            (hsv[:, :, 1] > 90)
            & (hsv[:, :, 2] > 70)
            & (
                (hsv[:, :, 0] < 18)
                | (hsv[:, :, 0] > 150)
                | ((hsv[:, :, 0] > 78) & (hsv[:, :, 0] < 110))
            )
        ).astype(np.uint8) * 255
        kernel_w = max(3, round(region.shape[1] * 0.01))
        kernel_h = max(3, round(region.shape[0] * 0.02))
        kernel = np.ones((kernel_h, kernel_w), dtype=np.uint8)
        merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(merged, 8)
        matches = 0
        region_area = float(region.shape[0] * region.shape[1])
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < region_area * 0.004 or area > region_area * 0.12:
                continue
            if comp_width < region.shape[1] * 0.035 or comp_width > region.shape[1] * 0.28:
                continue
            if comp_height < region.shape[0] * 0.12 or comp_height > region.shape[0] * 0.58:
                continue
            matches += 1
        return matches >= 2

    @staticmethod
    def _looks_like_card_grid(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.06))
        bottom = min(height, round(height * 0.88))
        left = max(0, round(width * 0.04))
        right = min(width, round(width * 0.96))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = (
            (hsv[:, :, 1] > 90)
            & (hsv[:, :, 2] > 70)
            & (
                (hsv[:, :, 0] < 18)
                | (hsv[:, :, 0] > 150)
                | ((hsv[:, :, 0] > 78) & (hsv[:, :, 0] < 110))
            )
        ).astype(np.uint8) * 255
        kernel_w = max(3, round(region.shape[1] * 0.01))
        kernel_h = max(3, round(region.shape[0] * 0.02))
        kernel = np.ones((kernel_h, kernel_w), dtype=np.uint8)
        merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(merged, 8)
        matches = 0
        region_area = float(region.shape[0] * region.shape[1])
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < region_area * 0.008 or area > region_area * 0.08:
                continue
            if comp_width < region.shape[1] * 0.045 or comp_width > region.shape[1] * 0.22:
                continue
            if comp_height < region.shape[0] * 0.16 or comp_height > region.shape[0] * 0.48:
                continue
            matches += 1
        return matches >= 6

    @staticmethod
    def _looks_like_map_legend(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        region = rgb[
            round(height * 0.18):round(height * 0.78),
            round(width * 0.78):round(width * 0.98),
        ]
        if region.size == 0:
            return False
        legend_mask = (
            (region[:, :, 0] > 120)
            & (region[:, :, 1] > 145)
            & (region[:, :, 2] > 145)
        )
        return float(legend_mask.mean()) >= 0.08

    @staticmethod
    def _looks_like_game_over(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        red_region = rgb[
            round(height * 0.08):round(height * 0.92),
            round(width * 0.05):round(width * 0.95),
        ]
        if red_region.size == 0:
            return False
        red_mask = (
            (red_region[:, :, 0] > 25)
            & (red_region[:, :, 0] > (red_region[:, :, 1] * 1.2))
            & (red_region[:, :, 0] > (red_region[:, :, 2] * 1.2))
        )
        banner_region = rgb[
            round(height * 0.10):round(height * 0.30),
            round(width * 0.28):round(width * 0.72),
        ]
        if banner_region.size == 0:
            return False
        banner_mask = (
            (banner_region[:, :, 0] > 130)
            & (banner_region[:, :, 1] > 100)
            & (banner_region[:, :, 2] > 80)
        )
        if float(red_mask.mean()) >= 0.55 and float(banner_mask.mean()) >= 0.10:
            return True
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        dark_ratio = float((gray < 18).mean())
        if dark_ratio < 0.93:
            return False
        top_region = gray[
            round(height * 0.10):round(height * 0.28),
            round(width * 0.25):round(width * 0.75),
        ]
        mid_region = gray[
            round(height * 0.30):round(height * 0.65),
            round(width * 0.30):round(width * 0.68),
        ]
        if top_region.size == 0 or mid_region.size == 0:
            return False
        top_bright_ratio = float((top_region > 35).mean())
        mid_bright_ratio = float((mid_region > 35).mean())
        return 0.08 <= top_bright_ratio <= 0.20 and 0.01 <= mid_bright_ratio <= 0.05

    def _actions_for_screen(self, screen: ScreenKind, screenshot) -> list[GameAction]:
        available: list[GameAction] = []
        event_option_points = self._event_option_points(screenshot) if screen == ScreenKind.EVENT else []
        visible_potion_slots = set(self._battle_potion_slots(screenshot)) if screen == ScreenKind.BATTLE else set()
        if screen == ScreenKind.MAP and self._map_has_highlighted_node(screenshot):
            for definition in self.profile.actions:
                if definition.screen == ScreenKind.MAP and definition.label == "Take highlighted node":
                    return [self._action_from_definition(definition)]
        if screen == ScreenKind.REWARD_MENU:
            dynamic_reward_menu_actions = self._reward_menu_actions(screenshot)
            if dynamic_reward_menu_actions:
                return dynamic_reward_menu_actions
            targets = self._reward_menu_targets(screenshot)
            for definition in self.profile.actions:
                if definition.screen != ScreenKind.REWARD_MENU:
                    continue
                target = str(definition.payload.get("target", ""))
                if target in targets:
                    available.append(self._action_from_definition(definition))
            return available
        if screen == ScreenKind.REWARD_GOLD_ONLY:
            for definition in self.profile.actions:
                if definition.screen == ScreenKind.REWARD_GOLD_ONLY:
                    available.append(self._action_from_definition(definition))
            return available
        if screen == ScreenKind.REWARD_CARDS:
            dynamic_reward_card_actions = self._reward_card_actions(screenshot)
            if dynamic_reward_card_actions:
                skip_action = next(
                    (
                        self._action_from_definition(definition)
                        for definition in self.profile.actions
                        if definition.screen == ScreenKind.REWARD_CARDS and definition.kind == ActionKind.SKIP_REWARD
                    ),
                    None,
                )
                if skip_action is not None:
                    dynamic_reward_card_actions.append(skip_action)
                return dynamic_reward_card_actions
            visible_reward_cards = len(self._reward_card_points(screenshot))
            for definition in self.profile.actions:
                if definition.screen != ScreenKind.REWARD_CARDS:
                    continue
                if definition.kind == ActionKind.PICK_CARD and visible_reward_cards >= 2:
                    option_index = 1
                    if definition.label.endswith("2"):
                        option_index = 2
                    elif definition.label.endswith("3"):
                        option_index = 3
                    if option_index > visible_reward_cards:
                        continue
                available.append(self._action_from_definition(definition))
            return available
        if screen == ScreenKind.REWARD_RELIC:
            dynamic_relic_actions = self._reward_relic_actions(screenshot)
            if dynamic_relic_actions:
                return dynamic_relic_actions
        if screen == ScreenKind.REWARD_POTION:
            dynamic_potion_actions = self._reward_potion_actions(screenshot)
            if dynamic_potion_actions:
                return dynamic_potion_actions
        if screen == ScreenKind.BOSS_RELIC:
            dynamic_boss_relic_actions = self._boss_relic_actions(screenshot)
            if dynamic_boss_relic_actions:
                return dynamic_boss_relic_actions
        if screen == ScreenKind.NEOW_CHOICE:
            dynamic_neow_actions = self._neow_choice_actions(screenshot)
            if dynamic_neow_actions:
                return dynamic_neow_actions
        if screen == ScreenKind.EVENT:
            dynamic_event_actions = self._event_choice_actions(screenshot)
            if dynamic_event_actions:
                return dynamic_event_actions
        if screen == ScreenKind.SHOP and self._looks_like_shop_card_popup(screenshot):
            for definition in self.profile.actions:
                if definition.screen == ScreenKind.SHOP and definition.label == "Close detail popup":
                    available.append(self._action_from_definition(definition))
            return available
        if screen == ScreenKind.SHOP:
            dynamic_shop_actions = self._shop_actions(screenshot)
            if dynamic_shop_actions:
                close_popup = next(
                    (
                        self._action_from_definition(definition)
                        for definition in self.profile.actions
                        if definition.screen == ScreenKind.SHOP and definition.label == "Close detail popup"
                    ),
                    None,
                )
                leave_shop = next(
                    (
                        self._action_from_definition(definition)
                        for definition in self.profile.actions
                        if definition.screen == ScreenKind.SHOP and definition.label == "Leave shop"
                    ),
                    None,
                )
                if close_popup is not None:
                    dynamic_shop_actions.insert(0, close_popup)
                if leave_shop is not None:
                    dynamic_shop_actions.append(leave_shop)
                return dynamic_shop_actions
        if screen == ScreenKind.CONTINUE:
            for definition in self.profile.actions:
                if definition.screen == ScreenKind.CONTINUE:
                    available.append(self._action_from_definition(definition))
            return available
        for definition in self.profile.actions:
            if definition.screen != screen:
                continue
            if definition.screen == ScreenKind.REWARD_MENU:
                continue
            if definition.screen == ScreenKind.BATTLE:
                slot_index = self._battle_potion_slot(definition)
                if slot_index is not None and slot_index not in visible_potion_slots:
                    continue
            if definition.screen == ScreenKind.EVENT and definition.payload.get("target") == "generic_event_option":
                option_index = int(definition.payload.get("option_index", -1))
                if len(event_option_points) <= 1:
                    continue
                if not (0 <= option_index < len(event_option_points)):
                    continue
                available.append(self._action_from_definition(definition))
                continue
            if definition.screen == ScreenKind.EVENT and definition.payload.get("target") == "generic_event_proceed":
                if len(event_option_points) != 1:
                    continue
                available.append(self._action_from_definition(definition))
                continue
            if definition.template_path and definition.region:
                if not definition.template_path.exists():
                    continue
                match = match_template(
                    screenshot,
                    definition.template_path,
                    definition.region.scaled(self._scale_x, self._scale_y) if definition.scale_region else definition.region,
                    definition.threshold,
                    scale_x=self._scale_x if definition.scale_template else 1.0,
                    scale_y=self._scale_y if definition.scale_template else 1.0,
                )
                if not match.found:
                    continue
            available.append(self._action_from_definition(definition))
        return available

    @staticmethod
    def _action_from_definition(definition: ActionDefinition) -> GameAction:
        return GameAction(
            kind=definition.kind,
            label=definition.label,
            payload=dict(definition.payload),
            tags=definition.tags[:],
        )

    def _find_action_definition(self, action: GameAction) -> ActionDefinition:
        if self._last_state is None:
            raise RuntimeError("No state has been observed before action application.")
        if self._last_state.screen == ScreenKind.NEOW_CHOICE and action.payload.get("target") == "generic_neow_option":
            return ActionDefinition(
                screen=ScreenKind.NEOW_CHOICE,
                kind=action.kind,
                label=action.label,
                point=(0, 0),
                tags=action.tags[:],
                payload=dict(action.payload),
            )
        click_point = action.payload.get("click_point")
        if (
            isinstance(click_point, list)
            and len(click_point) == 2
            and all(isinstance(value, int) for value in click_point)
        ):
            return ActionDefinition(
                screen=self._last_state.screen,
                kind=action.kind,
                label=action.label,
                point=(int(click_point[0]), int(click_point[1])),
                tags=action.tags[:],
                payload=dict(action.payload),
            )
        for definition in self.profile.actions:
            if definition.screen == self._last_state.screen and definition.kind == action.kind and definition.label == action.label:
                return definition
        raise RuntimeError(f"Action definition not found for {action.kind.value}:{action.label}")

    def _execute_action(
        self,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
        mode: str = "auto",
    ) -> str:
        self._require_runtime()
        if mode not in {"auto", "key", "click"}:
            raise ValueError(f"Unsupported action mode: {mode}")
        if definition.payload.get("battle_macro") == "basic_turn":
            played = self.play_basic_battle_turn(backend=backend)
            return played[-1].split(":", 1)[1] if played else "end_turn"
        runtime = self._require_runtime()
        use_live_helpers = hasattr(runtime, "capture_backend")
        if use_live_helpers:
            if definition.screen == ScreenKind.BATTLE and definition.kind == ActionKind.END_TURN:
                input_backend = self._resolve_input_backend(backend)
                result = self._end_battle_turn(input_backend, definition, backend=backend)
                self._close_temporary_backend(input_backend, backend)
                return result
            if definition.screen == ScreenKind.BATTLE and self._battle_potion_slot(definition) is not None:
                input_backend = self._resolve_input_backend(backend)
                result = self._use_battle_potion(input_backend, definition, backend=backend)
                self._close_temporary_backend(input_backend, backend)
                return result
            if definition.label == "Continue":
                input_backend = self._resolve_input_backend(backend)
                result = self._advance_continue(input_backend, fallback_point=definition.point, backend=backend)
                self._close_temporary_backend(input_backend, backend)
                return result
            if definition.screen == ScreenKind.EVENT and definition.payload.get("target") == "generic_event_option":
                input_backend = self._resolve_input_backend(backend)
                result = self._choose_generic_event_option(input_backend, definition, backend=backend)
                self._close_temporary_backend(input_backend, backend)
                return result
            if definition.screen == ScreenKind.EVENT and definition.payload.get("target") == "generic_event_proceed":
                input_backend = self._resolve_input_backend(backend)
                result = self._advance_single_option_event(input_backend, backend=backend)
                self._close_temporary_backend(input_backend, backend)
                return result
        if self._should_use_gamepad_buttons(definition, backend):
            press_xbox_sequence(
                definition.buttons,
                settle_ms=definition.settle_ms,
                hold_ms=definition.hold_ms,
                gap_ms=definition.gap_ms,
            )
            return "gamepad"
        input_backend = self._resolve_input_backend(backend)
        if definition.screen == ScreenKind.MAP and definition.kind == ActionKind.CHOOSE_PATH:
            result = self._navigate_map_selection(input_backend, definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.SHOP and definition.label == "Leave shop":
            result = self._click_navigation_arrow(input_backend, direction="left", fallback_point=definition.point)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.REWARD_MENU and definition.payload.get("target") == "card_reward":
            result = self._open_card_reward(input_backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.REWARD_MENU and definition.payload.get("target") == "gold":
            result = self._take_reward_gold(input_backend, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.REWARD_GOLD_ONLY and definition.payload.get("target") == "gold_only":
            result = self._take_reward_gold_only(input_backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.REWARD_CARDS and definition.kind == ActionKind.PICK_CARD:
            result = self._click_reward_card(input_backend, definition)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen in {ScreenKind.REWARD_RELIC, ScreenKind.REWARD_POTION}:
            result = self._execute_reward_choice(input_backend, definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.REST and definition.kind in {ActionKind.REST, ActionKind.SMITH}:
            result = self._execute_rest_choice(input_backend, definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.BOSS_RELIC:
            result = self._execute_boss_relic_choice(input_backend, definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.NEOW_CHOICE and definition.payload.get("target") == "generic_neow_option":
            result = self._choose_generic_neow_option(input_backend, definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.CONFIRM_POPUP and definition.payload.get("target") == "confirm_modal":
            result = self._confirm_modal(input_backend, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.screen == ScreenKind.CARD_GRID and definition.kind == ActionKind.PICK_CARD:
            result = self._pick_card_grid_card(definition, backend=backend)
            self._close_temporary_backend(input_backend, backend)
            return result
        if definition.keys:
            last_backend = input_backend.diagnostics().backend
            for key_name in definition.keys:
                input_backend.key_press(key_name, hold_ms=definition.hold_ms)
                self._action_sleep(definition.post_key_delay_ms)
                last_backend = input_backend.diagnostics().backend
            self._close_temporary_backend(input_backend, backend)
            return last_backend
        key_backend: str | None = None
        if definition.key and mode in {"auto", "key"}:
            input_backend.key_press(definition.key, hold_ms=definition.hold_ms)
            self._action_sleep(definition.post_key_delay_ms)
            key_backend = input_backend.diagnostics().backend
            if definition.drag_point is None and definition.point == (0, 0):
                self._close_temporary_backend(input_backend, backend)
                return key_backend
        if mode == "key":
            raise RuntimeError(f"Action does not define a key binding: {definition.label}")
        if definition.drag_point is not None:
            start = self._scale_reference_point(definition.point)
            end = self._scale_reference_point(definition.drag_point)
            input_backend.drag(start[0], start[1], end[0], end[1], duration_ms=definition.drag_duration_ms)
            self._action_sleep(self.profile.action_delay_ms)
            drag_backend = input_backend.diagnostics().backend
            self._close_temporary_backend(input_backend, backend)
            return key_backend or drag_backend
        point = self._scale_reference_point(definition.point)
        input_backend.click(point[0], point[1])
        self._action_sleep(self.profile.action_delay_ms)
        click_backend = input_backend.diagnostics().backend
        self._close_temporary_backend(input_backend, backend)
        return key_backend or click_backend

    def _navigate_map_selection(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        screenshot = self._capture_window_image()
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            buttons = self._map_choice_buttons(screenshot, definition)
            self._press_gamepad_sequence(buttons, hold_ms=110, gap_ms=130)
            return "gamepad"
        if self._map_has_highlighted_node(screenshot):
            if definition.payload.get("path") == "elite":
                input_backend.key_press("up", hold_ms=80)
                self._action_sleep(120)
            elif definition.payload.get("path") == "safe":
                input_backend.key_press("down", hold_ms=80)
                self._action_sleep(120)
            input_backend.key_press("enter", hold_ms=80)
            self._action_sleep(self.profile.action_delay_ms)
            return input_backend.diagnostics().backend
        point = self._map_choice_point(screenshot, definition.point)
        if point is None:
            for key_name in ("down", "down"):
                input_backend.key_press(key_name, hold_ms=80)
                self._action_sleep(120)
            screenshot = self._capture_window_image()
            if self._map_has_highlighted_node(screenshot):
                input_backend.key_press("enter", hold_ms=80)
                self._action_sleep(self.profile.action_delay_ms)
                return input_backend.diagnostics().backend
            point = self._map_choice_point(screenshot, definition.point)
        if point is None:
            point = self._scale_reference_point(definition.point)
        input_backend.click(point[0], point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    def _map_choice_buttons(self, screenshot: Image.Image, definition: ActionDefinition) -> list[str]:
        buttons: list[str] = []
        if not self._map_has_highlighted_node(screenshot):
            buttons.append("dpad_up")
            candidates = self._map_reachable_node_points(screenshot)
            if len(candidates) >= 2:
                target = self._map_choice_point(screenshot, definition.point)
                default_index = self._map_default_gamepad_index(screenshot, candidates)
                if target is not None:
                    target_index = min(
                        range(len(candidates)),
                        key=lambda index: abs(candidates[index][0] - target[0]) + abs(candidates[index][1] - target[1]),
                    )
                    delta = target_index - default_index
                    if delta > 0:
                        buttons.extend(["dpad_right"] * delta)
                    elif delta < 0:
                        buttons.extend(["dpad_left"] * abs(delta))
        buttons.extend(["a", "a"])
        return buttons

    @staticmethod
    def _map_default_gamepad_index(screenshot: Image.Image, candidates: list[tuple[int, int]]) -> int:
        if not candidates:
            return 0
        current_x = WindowsStsAdapter._map_current_node_x(screenshot)
        return min(range(len(candidates)), key=lambda index: abs(candidates[index][0] - current_x))

    @staticmethod
    def _map_current_node_x(screenshot: Image.Image) -> int:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        left = max(0, round(width * 0.14))
        right = min(width, round(width * 0.76))
        top = max(0, round(height * 0.45))
        bottom = min(height, round(height * 0.88))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return round(width * 0.5)
        mask = (
            (region[:, :, 0] < 120)
            & (region[:, :, 1] < 110)
            & (region[:, :, 2] < 110)
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        candidates: list[tuple[float, float]] = []
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < 180 or area > 3000:
                continue
            if comp_width < 20 or comp_height < 20:
                continue
            if comp_width > 110 or comp_height > 110:
                continue
            center_x, center_y = centroids[index]
            world_x = left + float(center_x)
            world_y = top + float(center_y)
            candidates.append((world_x, world_y))
        if not candidates:
            return round(width * 0.5)
        candidates.sort(key=lambda point: point[1], reverse=True)
        return int(round(candidates[0][0]))

    @staticmethod
    def _map_has_highlighted_node(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.08))
        bottom = min(height, round(height * 0.88))
        left = max(0, round(width * 0.12))
        right = min(width, round(width * 0.72))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        mask = (
            (region[:, :, 0] > 215)
            & (region[:, :, 1] > 215)
            & (region[:, :, 2] > 215)
        ).astype(np.uint8)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < 110:
                continue
            if comp_width < 10 or comp_height < 10:
                continue
            if comp_width > 120 or comp_height > 120:
                continue
            return True
        return False

    def _map_choice_point(self, screenshot: Image.Image, fallback_point: tuple[int, int]) -> tuple[int, int] | None:
        candidates = self._map_reachable_node_points(screenshot)
        if not candidates:
            return None
        self._update_scale(screenshot.size)
        target = (
            round(fallback_point[0] * self._scale_x),
            round(fallback_point[1] * self._scale_y),
        )
        return min(candidates, key=lambda point: abs(point[0] - target[0]) + abs(point[1] - target[1]))

    @staticmethod
    def _map_reachable_node_points(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        left = max(0, round(width * 0.15))
        right = min(width, round(width * 0.76))
        top = max(0, round(height * 0.08))
        bottom = min(height, round(height * 0.75))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        mask = (
            (region[:, :, 0] < 120)
            & (region[:, :, 1] < 105)
            & (region[:, :, 2] < 105)
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        candidates: list[tuple[int, int]] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            center_x, center_y = centroids[index]
            world_x = left + int(round(center_x))
            world_y = top + int(round(center_y))
            if area < 180 or area > 2200:
                continue
            if comp_width < 18 or comp_height < 18:
                continue
            if comp_width > 90 or comp_height > 90:
                continue
            if world_y < round(height * 0.22) or world_y > round(height * 0.64):
                continue
            candidates.append((world_x, world_y))
        if not candidates:
            return []
        bottom_row_y = max(point[1] for point in candidates)
        row_tolerance = max(36, round(height * 0.05))
        bottom_row = [point for point in candidates if abs(point[1] - bottom_row_y) <= row_tolerance]
        bottom_row.sort(key=lambda point: point[0])
        deduped: list[tuple[int, int]] = []
        for point in bottom_row:
            if deduped and abs(point[0] - deduped[-1][0]) <= 22 and abs(point[1] - deduped[-1][1]) <= 22:
                continue
            deduped.append(point)
        return deduped

    def _click_navigation_arrow(
        self,
        input_backend: InputBackend,
        *,
        direction: str,
        fallback_point: tuple[int, int],
        backend: str | None = None,
    ) -> str:
        screenshot = self._capture_window_image()
        point = self._navigation_arrow_point(screenshot, direction=direction)
        if point is None:
            point = self._scale_reference_point(fallback_point)
        click_backend, temporary_backend = self._resolve_click_backend(input_backend, backend=backend)
        try:
            click_backend.click(point[0], point[1])
            self._action_sleep(self.profile.action_delay_ms)
            return click_backend.diagnostics().backend
        finally:
            if temporary_backend:
                self._close_temporary_backend(click_backend, "window_messages")

    def _resolve_click_backend(
        self,
        input_backend: InputBackend,
        *,
        backend: str | None = None,
    ) -> tuple[InputBackend, bool]:
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name != "gamepad":
            return input_backend, False
        fallback_backend = self._resolve_input_backend("window_messages")
        return fallback_backend, True

    def _advance_continue(self, input_backend: InputBackend, *, fallback_point: tuple[int, int], backend: str | None = None) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        if self._looks_like_shop_continue_room(before):
            return self._click_navigation_arrow(input_backend, direction="right", fallback_point=fallback_point, backend=backend)
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            progressed, _after = self._try_gamepad_progress_sequences(
                before,
                [["y"], ["a", "a"], ["a", "a", "a", "a"], ["a", "a", "y"]],
            )
            if progressed:
                return "gamepad"
        return self._click_navigation_arrow(input_backend, direction="right", fallback_point=fallback_point, backend=backend)

    def _advance_single_option_event(self, input_backend: InputBackend, *, backend: str | None = None) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            progressed, _after = self._try_gamepad_progress_sequences(
                before,
                [["a", "a"], ["a", "a", "a", "a"], ["y"], ["a", "a", "y"]],
            )
            if progressed:
                return "gamepad"
        option_points = self._event_option_points(before)
        if option_points:
            click_backend, temporary_backend = self._resolve_click_backend(input_backend, backend=backend)
            try:
                click_backend.click(option_points[0][0], option_points[0][1])
                self._action_sleep(self.profile.action_delay_ms)
                after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                self._last_screenshot = after.copy()
                if len(self._event_option_points(after)) == 1:
                    click_backend.click(option_points[0][0], option_points[0][1])
                    self._action_sleep(self.profile.action_delay_ms)
                    settled = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                    self._last_screenshot = settled.copy()
                    if len(self._event_option_points(settled)) == 1:
                        progressed, _after = self._try_gamepad_progress_sequences(
                            settled,
                            [["a", "a"], ["a", "a", "a", "a"]],
                        )
                        if progressed:
                            return "gamepad"
                return click_backend.diagnostics().backend
            finally:
                if temporary_backend:
                    self._close_temporary_backend(click_backend, "window_messages")
        return input_backend.diagnostics().backend

    def _choose_generic_event_option(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        option_points = self._event_option_points(before)
        if not option_points:
            return input_backend.diagnostics().backend
        option_index = int(definition.payload.get("option_index", 0))
        target_index = min(max(option_index, 0), len(option_points) - 1)
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            progressed, _after = self._try_gamepad_menu_selection(
                before,
                orientation="vertical",
                option_index=target_index,
            )
            if progressed:
                return "gamepad"
        target_point = option_points[target_index]
        click_backend, temporary_backend = self._resolve_click_backend(input_backend, backend=backend)
        try:
            click_backend.click(target_point[0], target_point[1])
            self._action_sleep(self.profile.action_delay_ms)
            after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
            self._last_screenshot = after.copy()
            if len(self._event_option_points(after)) == len(option_points):
                click_backend.click(target_point[0], target_point[1])
                self._action_sleep(self.profile.action_delay_ms)
                settled = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                self._last_screenshot = settled.copy()
            return click_backend.diagnostics().backend
        finally:
            if temporary_backend:
                self._close_temporary_backend(click_backend, "window_messages")

    def _choose_generic_neow_option(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        option_index = int(definition.payload.get("option_index", 0))
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            progressed, _after = self._try_gamepad_menu_selection(
                before,
                orientation="vertical",
                option_index=option_index,
            )
            if progressed:
                return "gamepad"
        target_point = self._neow_option_point(before, option_index)
        click_backend, temporary_backend = self._resolve_click_backend(input_backend, backend=backend)
        try:
            click_backend.click(target_point[0], target_point[1])
            self._action_sleep(self.profile.action_delay_ms)
            after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
            self._last_screenshot = after.copy()
            if self.inspect_image(after, read_metrics=False).screen == ScreenKind.NEOW_CHOICE:
                click_backend.click(target_point[0], target_point[1])
                self._action_sleep(self.profile.action_delay_ms)
                settled = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                self._last_screenshot = settled.copy()
            return click_backend.diagnostics().backend
        finally:
            if temporary_backend:
                self._close_temporary_backend(click_backend, "window_messages")

    def _neow_option_point(self, screenshot: Image.Image, option_index: int) -> tuple[int, int]:
        title_region, body_region = self._neow_option_regions(screenshot.size, option_index)
        center_x = title_region.left + title_region.width // 2
        center_y = body_region.top + body_region.height // 2
        return center_x, center_y

    def _try_gamepad_menu_selection(
        self,
        reference: Image.Image,
        *,
        orientation: str,
        option_index: int,
        reset_count: int = 3,
        confirm_button: str = "a",
    ) -> tuple[bool, Image.Image]:
        if orientation not in {"vertical", "horizontal"}:
            raise ValueError(f"Unsupported menu orientation: {orientation}")
        clamped_index = max(0, option_index)
        reset_button = "dpad_up" if orientation == "vertical" else "dpad_left"
        move_button = "dpad_down" if orientation == "vertical" else "dpad_right"
        reset_sequence = [reset_button] * max(1, reset_count)
        move_sequence = [move_button] * clamped_index
        sequences: list[list[str]] = []
        if move_sequence:
            sequences.append(move_sequence + [confirm_button])
            sequences.append(move_sequence + [confirm_button, confirm_button])
        else:
            sequences.append([confirm_button])
            sequences.append([confirm_button, confirm_button])
        sequences.append(reset_sequence + move_sequence + [confirm_button])
        sequences.append(reset_sequence + move_sequence + [confirm_button, confirm_button])
        return self._try_gamepad_progress_sequences(reference, sequences)

    def _execute_reward_choice(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            option_index = 1 if definition.kind == ActionKind.SKIP_REWARD else 0
            progressed, _after = self._try_gamepad_menu_selection(
                before,
                orientation="vertical",
                option_index=option_index,
            )
            if progressed:
                return "gamepad"
        target_point = self._scale_reference_point(definition.point)
        input_backend.click(target_point[0], target_point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    def _execute_rest_choice(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            if definition.kind == ActionKind.SMITH:
                sequences = [
                    ["dpad_left", "dpad_right", "a"],
                    ["dpad_left", "dpad_left", "dpad_right", "a"],
                    ["dpad_left", "dpad_right", "a", "a"],
                ]
            else:
                sequences = [
                    ["dpad_left", "a"],
                    ["dpad_left", "dpad_left", "a"],
                    ["a"],
                ]
            progressed, _after = self._try_gamepad_progress_sequences(before, sequences)
            if progressed:
                return "gamepad"
        target_point = self._scale_reference_point(definition.point)
        input_backend.click(target_point[0], target_point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    def _execute_boss_relic_choice(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            if definition.kind == ActionKind.SKIP_REWARD:
                progressed, _after = self._try_gamepad_progress_sequences(
                    before,
                    [["dpad_down", "a"], ["dpad_left", "dpad_left", "dpad_down", "a"], ["dpad_down", "a", "a"]],
                )
            else:
                option_index = int(definition.payload.get("option_index", 0))
                progressed, _after = self._try_gamepad_menu_selection(
                    before,
                    orientation="horizontal",
                    option_index=option_index,
                )
            if progressed:
                return "gamepad"
        target_point = self._scale_reference_point(definition.point)
        input_backend.click(target_point[0], target_point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    def _end_battle_turn(
        self,
        input_backend: InputBackend | None = None,
        definition: ActionDefinition | None = None,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        backend_name = self._backend_name_for_actions(backend or (input_backend.diagnostics().backend if input_backend is not None else None))
        selection_active = self._selection_requires_target(before) or self._selected_card_drag_origin(before) is not None
        if backend_name == "gamepad":
            runtime = self._require_runtime()
            if not hasattr(runtime, "capture_backend"):
                buttons = ["y"] if not selection_active else ["dpad_down", "y"]
                self._press_gamepad_sequence(buttons)
                return "gamepad"
            sequences = [["y"], ["dpad_down", "y"], ["dpad_down", "a"]]
            if selection_active:
                sequences = [["dpad_down", "y"], ["dpad_down", "a"], ["dpad_down", "dpad_down", "a"]]
            progressed, _after = self._try_gamepad_progress_sequences(before, sequences)
            if progressed:
                return "gamepad"
        fallback_backend = self._resolve_input_backend("window_messages")
        try:
            fallback_backend.key_press(definition.key if definition is not None and definition.key else "e", hold_ms=definition.hold_ms if definition is not None else 80)
            self._action_sleep(self.profile.action_delay_ms)
            settled = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
            self._last_screenshot = settled.copy()
            return fallback_backend.diagnostics().backend
        finally:
            self._close_temporary_backend(fallback_backend, "window_messages")

    def _use_battle_potion(
        self,
        input_backend: InputBackend,
        definition: ActionDefinition,
        *,
        backend: str | None = None,
    ) -> str:
        before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        self._last_screenshot = before.copy()
        slot_index = self._battle_potion_slot(definition)
        before_slots = set(self._battle_potion_slots(before))
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if slot_index is not None and backend_name == "gamepad":
            gamepad_result = self._use_battle_potion_with_gamepad(before, slot_index, before_slots=before_slots)
            if gamepad_result is not None:
                return gamepad_result
        point = self._battle_potion_point(before, definition)
        click_backend, temporary_backend = self._resolve_click_backend(input_backend, backend=backend)
        try:
            click_backend.click(point[0], point[1])
            self._action_sleep(140)
            after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after):
                resolution = self._resolve_targeted_card(click_backend, after)
                self._last_screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                return resolution.backend
            if self._selected_card_drag_origin(after) is not None:
                resolution = self._resolve_non_target_card_with_background_drag(after.copy())
                self._last_screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
                return resolution.backend
            after_slots = set(self._battle_potion_slots(after))
            if (
                (slot_index is not None and slot_index in before_slots and slot_index not in after_slots)
                or self._battle_progress_made(before, after)
            ):
                return click_backend.diagnostics().backend
            return click_backend.diagnostics().backend
        finally:
            if temporary_backend:
                self._close_temporary_backend(click_backend, "window_messages")

    def _use_battle_potion_with_gamepad(self, before: Image.Image, slot_index: int, *, before_slots: set[int]) -> str | None:
        sequences = [["x"], ["x", "a"], ["x", "a", "a"]]
        if slot_index >= 2:
            sequences = [
                ["x", "dpad_right"],
                ["x", "dpad_right", "a"],
                ["x", "dpad_right", "a", "a"],
            ]
        current_reference = before.copy()
        for buttons in sequences:
            self._press_gamepad_sequence(buttons, hold_ms=80, gap_ms=90)
            after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = after.copy()
            if self._selection_requires_target(after):
                targeted_attempt = self._resolve_targeted_card_with_gamepad(after.copy())
                if targeted_attempt.played:
                    return targeted_attempt.backend
                background_attempt = self._resolve_targeted_card_with_background_messages(after.copy())
                if background_attempt.played:
                    return background_attempt.backend
            after_slots = set(self._battle_potion_slots(after))
            if slot_index in before_slots and slot_index not in after_slots:
                return "gamepad"
            if self._battle_progress_made(current_reference, after):
                return "gamepad"
            menu_result = self._attempt_confirm_battle_potion_menu(current_reference, slot_index)
            if menu_result is not None:
                return menu_result
            if self._frame_diff_score(current_reference, after) >= 1.0:
                current_reference = after.copy()
        self._press_gamepad_sequence(["dpad_down", "dpad_down", "dpad_down"], hold_ms=70, gap_ms=70)
        self._last_screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04)
        return None

    def _attempt_confirm_battle_potion_menu(self, reference: Image.Image, slot_index: int) -> str | None:
        input_backend = self._resolve_input_backend("window_messages")
        try:
            input_backend.key_press("enter", hold_ms=70)
            self._action_sleep(110)
            after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
            self._last_screenshot = after.copy()
            after_slots = set(self._battle_potion_slots(after))
            if self._selection_requires_target(after):
                resolution = self._resolve_targeted_card(input_backend, after)
                if resolution.played:
                    return resolution.backend
            if (
                slot_index not in after_slots
                or self._battle_progress_made(reference, after)
                or self._frame_diff_score(reference, after) >= 2.0
            ):
                return input_backend.diagnostics().backend
            return None
        finally:
            self._close_temporary_backend(input_backend, "window_messages")

    def _click_reward_card(self, input_backend: InputBackend, definition: ActionDefinition) -> str:
        screenshot = self._capture_window_image()
        point = self._reward_card_point(screenshot, definition.label)
        if point is None:
            point = self._scale_reference_point(definition.point)
        input_backend.click(point[0], point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    def _take_reward_gold(self, input_backend: InputBackend, *, backend: str | None = None) -> str:
        screenshot = self._capture_window_image()
        targets = self._reward_menu_targets(screenshot)
        highlighted = self._reward_menu_highlighted_target(screenshot)
        if "gold" not in targets:
            return input_backend.diagnostics().backend
        if highlighted != "gold":
            if self._backend_name_for_actions(backend) == "gamepad":
                self._press_gamepad_sequence(["dpad_up"], hold_ms=90, gap_ms=90)
                return "gamepad"
            input_backend.key_press("up", hold_ms=70)
            self._action_sleep(90)
        input_backend.key_press("enter", hold_ms=80)
        self._action_sleep(100)
        return input_backend.diagnostics().backend

    def _take_reward_gold_only(self, input_backend: InputBackend) -> str:
        screenshot = self._capture_window_image()
        if not self._reward_gold_only_focused(screenshot):
            self._press_gamepad_sequence(["dpad_down"], hold_ms=90, gap_ms=90)
            screenshot = self._capture_window_image_with_retry(attempts=4, backoff_seconds=0.08)
            self._last_screenshot = screenshot.copy()
        input_backend.key_press("enter", hold_ms=80)
        self._action_sleep(100)
        return input_backend.diagnostics().backend

    def _confirm_modal(self, input_backend: InputBackend, *, backend: str | None = None) -> str:
        backend_name = self._backend_name_for_actions(backend or input_backend.diagnostics().backend)
        if backend_name == "gamepad":
            before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
            self._last_screenshot = before.copy()
            for buttons in [["a"], ["dpad_down", "a"], ["a", "a"], ["dpad_down", "a", "a"]]:
                self._press_gamepad_sequence(buttons, hold_ms=90, gap_ms=90)
                after = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
                self._last_screenshot = after.copy()
                state = self.inspect_image(after, read_metrics=False)
                if state.screen != ScreenKind.CONFIRM_POPUP:
                    return "gamepad"
            fallback_backend = self._resolve_input_backend("window_messages")
            try:
                return self._confirm_modal(fallback_backend, backend="window_messages")
            finally:
                self._close_temporary_backend(fallback_backend, "window_messages")
        screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
        self._last_screenshot = screenshot.copy()
        confirm_point = self._confirm_modal_point(screenshot)
        prefer_click_first = self._looks_like_neow_panel(screenshot) and not self._looks_like_transform_confirm_popup(screenshot)
        last_backend = input_backend.diagnostics().backend
        for attempt in range(4):
            if prefer_click_first and attempt == 0:
                input_backend.click(confirm_point[0], confirm_point[1])
                self._action_sleep(100)
                last_backend = input_backend.diagnostics().backend
            else:
                input_backend.key_press("enter", hold_ms=80)
                self._action_sleep(100)
                last_backend = input_backend.diagnostics().backend
            screenshot = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.05)
            self._last_screenshot = screenshot.copy()
            state = self.inspect_image(screenshot, read_metrics=False)
            if state.screen != ScreenKind.CONFIRM_POPUP:
                return last_backend
            if not prefer_click_first and attempt == 0:
                input_backend.click(confirm_point[0], confirm_point[1])
                self._action_sleep(100)
                last_backend = input_backend.diagnostics().backend
                continue
            input_backend.key_press("down", hold_ms=70)
            self._action_sleep(70)
            last_backend = input_backend.diagnostics().backend
        return last_backend

    def _confirm_modal_point(self, screenshot: Image.Image) -> tuple[int, int]:
        if self._looks_like_transform_confirm_popup(screenshot):
            return self._scale_reference_point((1432, 690))
        width, height = screenshot.size
        if self._looks_like_neow_panel(screenshot):
            return (round(width * 0.50), round(height * 0.905))
        return self._scale_reference_point((1432, 690))

    def _detect_reward_variant(self, screenshot: Image.Image) -> ScreenKind:
        if self._looks_like_reward_cards(screenshot):
            return ScreenKind.REWARD_CARDS
        if self._reward_relic_actions(screenshot):
            return ScreenKind.REWARD_RELIC
        if self._reward_potion_actions(screenshot):
            return ScreenKind.REWARD_POTION
        targets = self._reward_menu_targets(screenshot)
        if "gold" in targets and any(kind in targets for kind in {"card_reward", "relic_reward", "potion_reward"}):
            return ScreenKind.REWARD_MENU
        if any(kind in targets for kind in {"card_reward", "relic_reward", "potion_reward"}):
            return ScreenKind.REWARD_MENU
        if "gold" in targets:
            return ScreenKind.REWARD_GOLD_ONLY
        reward_gold_only_match = self._last_matches.get("reward_gold_only_anchor_live")
        if reward_gold_only_match and reward_gold_only_match.found:
            return ScreenKind.REWARD_GOLD_ONLY
        return ScreenKind.REWARD_GOLD_ONLY

    def _reward_menu_targets(self, screenshot: Image.Image) -> set[str]:
        targets: set[str] = set()
        for definition in self.profile.actions:
            if definition.screen != ScreenKind.REWARD_MENU:
                continue
            if not definition.template_path or not definition.region:
                continue
            if not definition.template_path.exists():
                continue
            match = match_template(
                screenshot,
                definition.template_path,
                definition.region.scaled(self._scale_x, self._scale_y) if definition.scale_region else definition.region,
                definition.threshold,
                scale_x=self._scale_x if definition.scale_template else 1.0,
                scale_y=self._scale_y if definition.scale_template else 1.0,
            )
            if match.found:
                targets.add(str(definition.payload.get("target", "")))
        if not targets:
            targets.update(self._infer_reward_menu_targets(screenshot))
        return targets

    def _infer_reward_menu_targets(self, screenshot: Image.Image) -> set[str]:
        targets: set[str] = set()
        for point, kind in WindowsStsAdapter._reward_menu_option_points(screenshot):
            targets.add(self._reward_menu_row_kind(screenshot, point) or kind)
        return targets

    @staticmethod
    def _reward_menu_option_points(screenshot: Image.Image) -> list[tuple[tuple[int, int], str]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.30))
        bottom = min(height, round(height * 0.60))
        left = max(0, round(width * 0.34))
        right = min(width, round(width * 0.66))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = (
            (hsv[:, :, 0] > 75)
            & (hsv[:, :, 0] < 115)
            & (hsv[:, :, 1] > 45)
            & (hsv[:, :, 2] > 85)
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        options: list[tuple[tuple[int, int], str]] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 4500:
                continue
            if comp_width < round(region.shape[1] * 0.55):
                continue
            if comp_height < 28 or comp_height > max(90, round(region.shape[0] * 0.42)):
                continue
            world_x = left + int(round(float(centroids[index][0])))
            world_y = top + int(round(float(centroids[index][1])))
            row_crop = region[y : y + comp_height, x : x + comp_width]
            icon_width = max(16, round(comp_width * 0.22))
            icon_crop = row_crop[:, :icon_width]
            kind = "card_reward"
            if icon_crop.size != 0:
                warm_mask = (
                    (icon_crop[:, :, 0] > 150)
                    & (icon_crop[:, :, 1] > 100)
                    & (icon_crop[:, :, 2] < 120)
                )
                if int(warm_mask.sum()) >= 35:
                    kind = "gold"
            options.append(((world_x, world_y), kind))
        options.sort(key=lambda item: item[0][1])
        deduped: list[tuple[tuple[int, int], str]] = []
        for point, kind in options:
            if deduped and abs(point[1] - deduped[-1][0][1]) <= 18:
                continue
            deduped.append((point, kind))
        return deduped

    def _reward_menu_highlighted_target(self, screenshot: Image.Image) -> str | None:
        targets = self._reward_menu_targets(screenshot)
        if not targets:
            return None
        if self._reward_gold_only_focused(screenshot):
            return "gold"
        option_points = self._reward_menu_option_points(screenshot)
        if len(option_points) >= 2:
            top_kind = option_points[0][1]
            return top_kind
        if "gold" in targets and "card_reward" not in targets:
            return "gold"
        return None

    @staticmethod
    def _reward_gold_only_focused(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.18))
        bottom = min(height, round(height * 0.35))
        left = max(0, round(width * 0.24))
        right = min(width, round(width * 0.49))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        mask = (
            (region[:, :, 0] > 180)
            & (region[:, :, 1] > 130)
            & (region[:, :, 1] < 240)
            & (region[:, :, 2] < 140)
        ).astype(np.uint8)
        return int(mask.sum()) >= 50

    def _pick_card_grid_card(self, definition: ActionDefinition, *, backend: str | None = None) -> str:
        backend_name = self._backend_name_for_actions(self._preferred_backend_for_definition(definition, backend))
        if backend_name == "gamepad":
            runtime = self._require_runtime()
            before = self._capture_window_image_with_retry(attempts=2, backoff_seconds=0.04) if hasattr(runtime, "capture_backend") else None
            buttons = self._card_grid_buttons(definition)
            self._press_gamepad_sequence(buttons, hold_ms=120, gap_ms=150)
            if before is not None:
                after = self._capture_window_image_with_retry(attempts=3, backoff_seconds=0.05)
                self._last_screenshot = after.copy()
                if self.inspect_image(after, read_metrics=False).screen == ScreenKind.CARD_GRID:
                    fallback_backend = self._resolve_input_backend("window_messages")
                    try:
                        point = self._scale_reference_point(definition.point)
                        fallback_backend.click(point[0], point[1])
                        self._action_sleep(120)
                        confirm = self._scale_reference_point((1432, 690))
                        fallback_backend.click(confirm[0], confirm[1])
                        self._action_sleep(self.profile.action_delay_ms)
                        return fallback_backend.diagnostics().backend
                    finally:
                        self._close_temporary_backend(fallback_backend, "window_messages")
            return "gamepad"
        input_backend = self._resolve_input_backend(backend)
        point = self._scale_reference_point(definition.point)
        input_backend.click(point[0], point[1])
        self._action_sleep(self.profile.action_delay_ms)
        return input_backend.diagnostics().backend

    @staticmethod
    def _card_grid_buttons(definition: ActionDefinition) -> list[str]:
        slot_value = definition.payload.get("card")
        if not isinstance(slot_value, str) or not slot_value.startswith("slot_"):
            return ["dpad_right", "a", "a"]
        try:
            slot_index = int(slot_value.split("_", 1)[1])
        except ValueError:
            return ["dpad_right", "a", "a"]
        slot_index = min(max(slot_index, 1), 10)
        row_index = 0 if slot_index <= 5 else 1
        column_index = (slot_index - 1) % 5
        buttons = ["dpad_right"]
        buttons.extend(["dpad_right"] * column_index)
        if row_index:
            buttons.append("dpad_down")
        buttons.extend(["a", "a"])
        return buttons

    @staticmethod
    def _reward_card_point(screenshot: Image.Image, label: str) -> tuple[int, int] | None:
        points = WindowsStsAdapter._reward_card_points(screenshot)
        if len(points) < 2:
            return None
        option_index = 0
        if label.endswith("2"):
            option_index = 1
        elif label.endswith("3"):
            option_index = 2
        option_index = min(option_index, len(points) - 1)
        return points[option_index]

    @staticmethod
    def _reward_card_points(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.26))
        bottom = min(height, round(height * 0.86))
        left = max(0, round(width * 0.15))
        right = min(width, round(width * 0.88))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return None
        card_mask = (
            (region[:, :, 0] > 80)
            & (region[:, :, 1] < 190)
            & (region[:, :, 2] < 190)
            & (region[:, :, 0] > (region[:, :, 1] * 0.9))
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(card_mask * 255, 8)
        points: list[tuple[int, int]] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 2500:
                continue
            if comp_width < 70 or comp_height < 120:
                continue
            if comp_width > 400 or comp_height > 600:
                continue
            if comp_height / max(comp_width, 1) < 0.75:
                continue
            centroid_x, centroid_y = centroids[index]
            points.append((left + int(round(centroid_x)), top + int(round(centroid_y))))
        points.sort(key=lambda point: point[0])
        return points

    @staticmethod
    def _looks_like_reward_cards(screenshot: Image.Image) -> bool:
        return len(WindowsStsAdapter._reward_card_points(screenshot)) >= 3

    @staticmethod
    def _navigation_arrow_point(
        screenshot: Image.Image,
        *,
        direction: str,
    ) -> tuple[int, int] | None:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.66))
        bottom = height
        if direction == "right":
            left = max(0, round(width * 0.68))
            right = width
        elif direction == "left":
            left = 0
            right = min(width, round(width * 0.32))
        else:
            raise ValueError(f"Unsupported direction: {direction}")
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return None
        mask = (
            ((region[:, :, 0] > 120) & (region[:, :, 1] < 120) & (region[:, :, 2] < 120))
            | ((region[:, :, 0] > 150) & (region[:, :, 1] > 50) & (region[:, :, 1] < 190) & (region[:, :, 2] < 110))
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        best_index: int | None = None
        best_area = 0
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 400:
                continue
            if comp_width < 70 or comp_height < 35:
                continue
            if area > best_area:
                best_area = area
                best_index = index
        if best_index is None:
            return None
        cx, cy = centroids[best_index]
        return left + round(float(cx)), top + round(float(cy))

    def _battle_potion_slots(self, screenshot: Image.Image) -> list[int]:
        slots: list[int] = []
        for slot_index in (1, 2):
            if self._battle_potion_present(screenshot, slot_index):
                slots.append(slot_index)
        return slots

    @staticmethod
    def _battle_potion_slot(definition: ActionDefinition) -> int | None:
        slot_value = definition.payload.get("battle_potion_slot", definition.payload.get("slot"))
        try:
            return int(slot_value)
        except (TypeError, ValueError):
            return None

    def _battle_potion_present(self, screenshot: Image.Image, slot_index: int) -> bool:
        region = self._battle_potion_region(screenshot, slot_index)
        if region is None:
            return False
        rgb = np.asarray(region.convert("RGB"))
        if rgb.size == 0:
            return False
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        mask = (
            ((hsv[:, :, 1] > 35) & (hsv[:, :, 2] > 55) & ((hsv[:, :, 0] < 90) | (hsv[:, :, 0] > 120)))
            | ((hsv[:, :, 2] > 110) & (hsv[:, :, 1] < 40))
        ).astype(np.uint8)
        if int(mask.sum()) < 90:
            return False
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < 24:
                continue
            if comp_width < 6 or comp_height < 6:
                continue
            return True
        return False

    def _battle_potion_point(self, screenshot: Image.Image, definition: ActionDefinition) -> tuple[int, int]:
        slot_index = self._battle_potion_slot(definition)
        if slot_index is None:
            return self._scale_reference_point(definition.point)
        region = self._battle_potion_region(screenshot, slot_index)
        if region is None:
            return self._scale_reference_point(definition.point)
        rgb = np.asarray(region.convert("RGB"))
        if rgb.size == 0:
            return self._scale_reference_point(definition.point)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        mask = (
            ((hsv[:, :, 1] > 35) & (hsv[:, :, 2] > 55) & ((hsv[:, :, 0] < 90) | (hsv[:, :, 0] > 120)))
            | ((hsv[:, :, 2] > 110) & (hsv[:, :, 1] < 40))
        ).astype(np.uint8)
        ys, xs = np.where(mask > 0)
        if xs.size == 0:
            return self._scale_reference_point(definition.point)
        box = self._battle_potion_region_box(screenshot.size, slot_index)
        return box[0] + int(round(float(xs.mean()))), box[1] + int(round(float(ys.mean())))

    def _battle_potion_region_diff(
        self,
        before: Image.Image,
        after: Image.Image,
        definition: ActionDefinition | None = None,
        *,
        slot_index: int | None = None,
    ) -> float:
        if slot_index is None and definition is not None:
            slot_index = self._battle_potion_slot(definition)
        if slot_index is None:
            return 0.0
        before_box = self._battle_potion_region_box(before.size, slot_index)
        after_box = self._battle_potion_region_box(after.size, slot_index)
        before_crop = before.crop(before_box)
        after_crop = after.crop(after_box)
        return self._frame_diff_score(before_crop, after_crop)

    def _battle_potion_region(self, screenshot: Image.Image, slot_index: int) -> Image.Image | None:
        box = self._battle_potion_region_box(screenshot.size, slot_index)
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        return screenshot.crop(box)

    def _battle_potion_region_box(self, image_size: tuple[int, int], slot_index: int) -> tuple[int, int, int, int]:
        centers = {
            1: (423, 47),
            2: (468, 48),
        }
        center = centers.get(slot_index)
        if center is None:
            point = self._scale_reference_point((423, 47))
        else:
            self._update_scale(image_size)
            point = (
                round(center[0] * self._scale_x),
                round(center[1] * self._scale_y),
            )
        half_width = max(18, round(28 * self._scale_x))
        half_height = max(14, round(22 * self._scale_y))
        left = max(0, point[0] - half_width)
        top = max(0, point[1] - half_height)
        right = min(image_size[0], point[0] + half_width)
        bottom = min(image_size[1], point[1] + half_height)
        return left, top, right, bottom

    def _preferred_backend_for_definition(self, definition: ActionDefinition, backend: str | None) -> str | None:
        if backend not in {None, "", "auto"}:
            return backend
        return self.profile.scene_input_backends.get(definition.screen.value)

    @staticmethod
    def _should_use_gamepad_buttons(definition: ActionDefinition, backend: str | None) -> bool:
        if not definition.buttons:
            return False
        if backend == "gamepad":
            return True
        if backend not in {None, "", "auto"}:
            return False
        has_non_gamepad_path = bool(definition.keys or definition.key or definition.drag_point is not None or definition.point != (0, 0))
        return not has_non_gamepad_path

    def _open_card_reward(self, input_backend: InputBackend) -> str:
        last_backend = input_backend.diagnostics().backend
        for attempt in range(8):
            input_backend.key_press("enter", hold_ms=80)
            self._action_sleep(90)
            last_backend = input_backend.diagnostics().backend
            time.sleep(0.18)
            screenshot = self._capture_window_image()
            self._last_screenshot = screenshot.copy()
            state = self.inspect_image(screenshot, read_metrics=False)
            if state.screen != ScreenKind.REWARD_MENU:
                return last_backend
            if attempt == 7:
                break
            input_backend.key_press("down", hold_ms=70)
            self._action_sleep(70)
            last_backend = input_backend.diagnostics().backend
            time.sleep(0.10)
        return last_backend

    def _record_action(self, action: GameAction) -> None:
        self._strategy_tags.update(action.tags)
        if action.kind.value == "pick_card":
            card_name = str(action.payload.get("card", action.label))
            self._picked_cards.append(card_name)
            self._deck_cards.append(DeckCard(card_name, tags=action.tags[:]))
        elif action.kind.value == "skip_reward":
            self._skipped_cards.append(action.label)
        elif action.kind.value == "take_relic":
            relic_name = str(action.payload.get("relic", action.label))
            self._relics.append(relic_name)
        elif action.kind.value == "choose_path":
            path_name = str(action.payload.get("path", action.label))
            self._path.append(path_name)

    def _update_scale(self, image_size: tuple[int, int]) -> None:
        width, height = image_size
        self._scale_x = width / self.profile.reference_width
        self._scale_y = height / self.profile.reference_height

    def _capture_window_image(self):
        runtime = self._require_runtime()
        image = runtime.capture_backend.read_latest_frame(timeout_ms=250)
        if self._is_blank_capture(image):
            raise RuntimeError("Capture backend returned a blank frame.")
        return image

    def _capture_window_image_with_retry(
        self,
        *,
        attempts: int = 6,
        backoff_seconds: float = 0.12,
    ):
        last_error: RuntimeError | None = None
        for attempt_index in range(max(1, attempts)):
            try:
                return self._capture_window_image()
            except RuntimeError as exc:
                last_error = exc
                if attempt_index == attempts - 1:
                    break
                time.sleep(backoff_seconds * (attempt_index + 1))
        raise last_error or RuntimeError("Capture backend returned no usable frame.")

    def _extract_battle_enemies(self, screenshot: Image.Image, *, include_hp_text: bool = False) -> list[EnemyState]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.45))
        bottom = min(height, round(height * 0.92))
        left = max(0, round(width * 0.45))
        right = min(width, round(width * 0.98))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return []
        hp_mask = (
            (region[:, :, 0] > 170)
            & (region[:, :, 1] < 110)
            & (region[:, :, 2] < 125)
        ).astype(np.uint8)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(hp_mask * 255, 8)
        enemies: list[EnemyState] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 350:
                continue
            if comp_width < 55 or comp_width > 220:
                continue
            if comp_height < 6 or comp_height > 60:
                continue
            if y > round(region.shape[0] * 0.72):
                continue
            world_x = left + int(x)
            world_y = top + int(y)
            hp_text = None
            hp_value = None
            max_hp = None
            if include_hp_text:
                hp_rect = self._enemy_hp_text_rect(world_x, world_y, int(comp_width), int(comp_height), width, height)
                hp_text, hp_value, max_hp = self._read_enemy_hp_text(screenshot, hp_rect)
            status_icon_count = self._enemy_status_icon_count(rgb, world_x, world_y, int(comp_width), width, height)
            intent_damage = self._enemy_intent_damage(rgb, world_x, world_y, width, height)
            enemies.append(
                EnemyState(
                    x=world_x,
                    y=world_y,
                    width=int(comp_width),
                    height=int(comp_height),
                    hp=hp_value,
                    max_hp=max_hp,
                    hp_text=hp_text,
                    status_icon_count=status_icon_count,
                    intent_damage=intent_damage,
                )
            )
        enemies.sort(key=lambda enemy: enemy.x)
        return enemies

    @staticmethod
    def _enemy_hp_text_rect(x: int, y: int, bar_width: int, bar_height: int, image_width: int, image_height: int) -> tuple[int, int, int, int]:
        left = max(0, x + bar_width - 8)
        top = max(0, y - max(8, round(bar_height * 0.9)))
        width = min(image_width - left, max(84, round(bar_width * 1.45)))
        height = min(image_height - top, max(40, round(bar_height * 5.2)))
        return (left, top, width, height)

    def _read_enemy_hp_text(self, screenshot: Image.Image, rect: tuple[int, int, int, int]) -> tuple[str | None, int | None, int | None]:
        left, top, width, height = rect
        crop_box = (left, top, left + width, top + height)
        crop = screenshot.crop(crop_box)
        hp_text = self._extract_enemy_hp_text(crop)
        if not hp_text:
            return (None, None, None)
        parsed = self._parse_enemy_hp_text(hp_text)
        if parsed is None:
            max_hp = self._extract_enemy_max_hp(crop)
            return (hp_text, None, max_hp)
        return (hp_text, parsed[0], parsed[1])

    def _extract_enemy_hp_text(self, crop: Image.Image) -> str:
        rgb = np.asarray(crop.convert("RGB"))
        text_mask = (
            ((rgb[:, :, 0] > 175) & (rgb[:, :, 1] < 105) & (rgb[:, :, 2] < 130))
            | ((rgb[:, :, 0] > 210) & (rgb[:, :, 1] > 210) & (rgb[:, :, 2] > 210))
        ).astype(np.uint8) * 255
        if int(text_mask.sum()) < 2000:
            return ""
        mask_image = Image.fromarray(text_mask).convert("L")
        region = TextRegionDefinition(
            "enemy_hp",
            Rect(0, 0, mask_image.width, mask_image.height),
            whitelist="0123456789/",
            parser="pair",
        )
        try:
            return extract_text(mask_image, region)
        except RuntimeError:
            return ""

    @staticmethod
    def _parse_enemy_hp_text(text: str) -> tuple[int, int] | None:
        cleaned = "".join(char for char in text if char.isdigit() or char == "/")
        if "/" not in cleaned:
            return None
        left_text, right_text = cleaned.split("/", 1)
        if not left_text or not right_text:
            return None
        hp = int(left_text)
        max_hp = int(right_text)
        if hp < 0 or max_hp <= 0 or hp > max_hp:
            return None
        if len(right_text) > 2 and max_hp > 200:
            trimmed = right_text[:2]
            if trimmed.isdigit():
                candidate = int(trimmed)
                if hp <= candidate <= 200:
                    max_hp = candidate
        return (hp, max_hp) if hp <= max_hp else None

    def _extract_enemy_max_hp(self, crop: Image.Image) -> int | None:
        right_half = crop.crop((max(0, crop.width // 2 - 6), 0, crop.width, crop.height))
        raw_text = self._extract_enemy_hp_text(right_half)
        digits = "".join(char for char in raw_text if char.isdigit())
        if not digits:
            return None
        if len(digits) >= 2:
            candidate = int(digits[-2:])
        else:
            candidate = int(digits)
        if 1 <= candidate <= 200:
            return candidate
        return None

    @staticmethod
    def _enemy_status_icon_count(rgb: np.ndarray, x: int, y: int, bar_width: int, image_width: int, image_height: int) -> int:
        left = max(0, x - 12)
        top = min(image_height - 1, y + 18)
        width = min(image_width - left, max(90, bar_width + 40))
        height = min(image_height - top, 44)
        if width <= 0 or height <= 0:
            return 0
        region = rgb[top : top + height, left : left + width]
        mask = (
            ((region[:, :, 0] > 160) & (region[:, :, 1] < 120) & (region[:, :, 2] < 120))
            | ((region[:, :, 0] > 170) & (region[:, :, 1] > 170) & (region[:, :, 2] > 170))
            | ((region[:, :, 0] > 180) & (region[:, :, 1] > 90) & (region[:, :, 1] < 180) & (region[:, :, 2] < 90))
        ).astype(np.uint8)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask * 255, 8)
        count = 0
        for index in range(1, num_labels):
            _x, _y, comp_width, comp_height, area = stats[index]
            if area < 35:
                continue
            if comp_width < 6 or comp_height < 6:
                continue
            count += 1
        return count

    @staticmethod
    def _enemy_intent_damage(rgb: np.ndarray, x: int, y: int, image_width: int, image_height: int) -> int | None:
        left = max(0, x - 10)
        top = max(0, y - 72)
        width = min(image_width - left, 120)
        height = min(image_height - top, 70)
        if width <= 0 or height <= 0:
            return None
        crop = Image.fromarray(rgb[top : top + height, left : left + width])
        definition = TextRegionDefinition("intent_damage", Rect(0, 0, crop.width, crop.height), whitelist="0123456789x", parser="int")
        try:
            raw_text = extract_text(crop, definition)
        except RuntimeError:
            return None
        parsed = parse_text_value(raw_text, "int")
        return int(parsed) if isinstance(parsed, int) else None

    def _wait_for_change(self, previous_state: GameState, previous_screenshot: Image.Image) -> None:
        timeout_by_screen = {
            ScreenKind.BATTLE: 1.8,
            ScreenKind.REWARD_MENU: 2.4,
            ScreenKind.REWARD_CARDS: 2.4,
            ScreenKind.REWARD_RELIC: 2.4,
            ScreenKind.REWARD_POTION: 2.4,
            ScreenKind.REWARD_GOLD_ONLY: 2.0,
            ScreenKind.MAP: 2.2,
            ScreenKind.MENU: 3.0,
            ScreenKind.MODE_SELECT: 3.0,
            ScreenKind.CHARACTER_SELECT: 3.0,
            ScreenKind.NEOW_CHOICE: 3.0,
            ScreenKind.NEOW_DIALOG: 3.0,
            ScreenKind.RELIC_POPUP: 2.4,
            ScreenKind.CARD_GRID: 2.4,
            ScreenKind.CONFIRM_POPUP: 2.4,
            ScreenKind.REST: 2.4,
            ScreenKind.SHOP: 2.4,
            ScreenKind.BOSS_RELIC: 2.4,
            ScreenKind.GAME_OVER: 2.0,
        }
        deadline = time.time() + timeout_by_screen.get(previous_state.screen, 2.5)
        while time.time() < deadline:
            try:
                screenshot = self._capture_window_image()
            except RuntimeError:
                time.sleep(0.08)
                continue
            state = self.inspect_image(screenshot, read_metrics=False)
            if state.screen == ScreenKind.UNKNOWN:
                time.sleep(0.08)
                continue
            if state.screen != previous_state.screen:
                return
            if [action.label for action in state.available_actions] != [action.label for action in previous_state.available_actions]:
                return
            if self._frame_diff_score(previous_screenshot, screenshot) >= 6.0:
                return
            time.sleep(0.08)

    def _scale_reference_point(self, point: tuple[int, int]) -> tuple[int, int]:
        runtime = self._require_runtime()
        runtime.transform.refresh()
        return runtime.transform.reference_to_client(point)

    def _require_runtime(self) -> IoRuntime:
        if self._runtime is None:
            raise RuntimeError("Live runtime is unavailable. Call start_run first.")
        return self._runtime

    def _resolve_input_backend(self, backend: str | None) -> InputBackend:
        runtime = self._require_runtime()
        if backend is None or backend in {runtime.input_backend.name, self.profile.input_backend_name, "auto"}:
            return runtime.input_backend
        if backend == "window_messages":
            backend_instance = WindowMessageInputBackend(dry_run=self.profile.dry_run, delivery=self.profile.window_message_delivery)
        elif backend == "window_messages_post":
            backend_instance = WindowMessageInputBackend(dry_run=self.profile.dry_run, delivery="post")
        elif backend == "window_messages_send":
            backend_instance = WindowMessageInputBackend(dry_run=self.profile.dry_run, delivery="send")
        elif backend == "legacy":
            backend_instance = LegacyForegroundInputBackend(backend=self.profile.legacy_input_backend, dry_run=self.profile.dry_run)
        else:
            backend_instance = LegacyForegroundInputBackend(backend=backend, dry_run=self.profile.dry_run)
        backend_instance.open(runtime.target)
        return backend_instance

    def _close_temporary_backend(self, backend: InputBackend, requested_backend: str | None) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        if backend is runtime.input_backend:
            return
        if requested_backend is None:
            return
        backend.close()

    @staticmethod
    def _action_sleep(delay_ms: int) -> None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

    @staticmethod
    def _selection_requires_target(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.20))
        bottom = min(height, round(height * 0.76))
        left = max(0, round(width * 0.28))
        right = min(width, round(width * 0.96))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        red_mask = (region[:, :, 0] > 145) & (region[:, :, 1] < 120) & (region[:, :, 2] < 120)
        corridor = region[
            round(region.shape[0] * 0.20):round(region.shape[0] * 0.68),
            round(region.shape[1] * 0.08):round(region.shape[1] * 0.75),
        ]
        corridor_red_pixels = 0
        if corridor.size != 0:
            corridor_red_pixels = int(
                ((corridor[:, :, 0] > 145) & (corridor[:, :, 1] < 120) & (corridor[:, :, 2] < 120)).sum()
            )
        red_pixels = int(red_mask.sum())
        if corridor_red_pixels >= 1800 or red_pixels >= 4500:
            return True
        return False

    @staticmethod
    def _selected_card_drag_origin(screenshot: Image.Image) -> tuple[int, int] | None:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.08))
        bottom = min(height, round(height * 0.74))
        left = max(0, round(width * 0.22))
        right = min(width, round(width * 0.86))
        region = rgb[top:bottom, left:right]
        cyan_mask = (region[:, :, 0] < 130) & (region[:, :, 1] > 180) & (region[:, :, 2] > 180)
        coords = np.argwhere(cyan_mask)
        if coords.size == 0:
            return None
        top_band = coords[:, 0].min() + max(16, round(region.shape[0] * 0.10))
        focused = coords[coords[:, 0] <= top_band]
        if focused.size == 0:
            focused = coords
        y0, x0 = focused.min(axis=0)
        y1, x1 = focused.max(axis=0)
        center_x = left + round((x0 + x1) / 2)
        center_y = top + round((y0 + y1) / 2)
        return (center_x, center_y)

    @staticmethod
    def _looks_like_zero_energy(screenshot: Image.Image) -> bool:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.83))
        bottom = min(height, round(height * 0.995))
        left = max(0, round(width * 0.015))
        right = min(width, round(width * 0.115))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return False
        bright_ratio = float(((region[:, :, 0] > 150) & (region[:, :, 1] > 100)).mean())
        red_ratio = float(((region[:, :, 0] > 100) & (region[:, :, 1] < 120) & (region[:, :, 2] < 120)).mean())
        return 0.04 <= red_ratio <= 0.10 and bright_ratio <= 0.06

    def _selected_card_cost(self, screenshot: Image.Image) -> int | None:
        badge_rect = self._selected_card_cost_badge_rect(screenshot)
        if badge_rect is None:
            return None
        left, top, width, height = badge_rect
        crop = screenshot.crop((left, top, left + width, top + height)).convert("RGB")
        enlarged = crop.resize((max(1, crop.width * 3), max(1, crop.height * 3)), Image.Resampling.LANCZOS)
        rgb = np.asarray(enlarged)
        digit_mask = (
            ((rgb[:, :, 0] > 175) & (rgb[:, :, 1] > 120) & (rgb[:, :, 2] > 70))
            | ((rgb[:, :, 0] > 200) & (rgb[:, :, 1] > 200) & (rgb[:, :, 2] > 200))
        ).astype(np.uint8) * 255
        if int(digit_mask.sum()) < 800:
            return None
        mask_image = Image.fromarray(digit_mask).convert("L")
        region = TextRegionDefinition(
            "selected_card_cost",
            Rect(0, 0, mask_image.width, mask_image.height),
            whitelist="0123456789",
            parser="int",
        )
        try:
            raw_text = extract_text(mask_image, region)
        except RuntimeError:
            return None
        parsed = parse_text_value(raw_text, "int")
        if not isinstance(parsed, int):
            return None
        if parsed < 0 or parsed > 5:
            return None
        return int(parsed)

    @staticmethod
    def _selected_card_cost_badge_rect(screenshot: Image.Image) -> tuple[int, int, int, int] | None:
        origin = WindowsStsAdapter._selected_card_drag_origin(screenshot)
        fallback = WindowsStsAdapter._fallback_selected_card_drag_origin(screenshot)
        anchors = [point for point in (origin, fallback) if point is not None]
        if not anchors:
            return None
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        base_anchor = origin or fallback
        assert base_anchor is not None
        min_anchor_x = min(point[0] for point in anchors)
        search_left = max(0, min_anchor_x - round(width * 0.22))
        search_right = min(width, base_anchor[0] - round(width * 0.01))
        search_top = max(0, base_anchor[1] - round(height * 0.08))
        search_bottom = min(height, base_anchor[1] + round(height * 0.10))
        if search_right <= search_left or search_bottom <= search_top:
            return None
        region = rgb[search_top:search_bottom, search_left:search_right]
        badge_mask = (
            (region[:, :, 0] > 150)
            & (region[:, :, 1] < 170)
            & (region[:, :, 2] < 120)
        ).astype(np.uint8)
        num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(badge_mask * 255, 8)
        best_rect: tuple[int, int, int, int] | None = None
        best_area = 0
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 140:
                continue
            if comp_width < 16 or comp_height < 16:
                continue
            if comp_width > 120 or comp_height > 120:
                continue
            if x > round(region.shape[1] * 0.75):
                continue
            if area > best_area:
                best_area = int(area)
                best_rect = (
                    search_left + int(x),
                    search_top + int(y),
                    int(comp_width),
                    int(comp_height),
                )
        return best_rect

    @staticmethod
    def _fallback_selected_card_drag_origin(screenshot: Image.Image) -> tuple[int, int] | None:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.08))
        bottom = min(height, round(height * 0.95))
        left = max(0, round(width * 0.10))
        right = min(width, round(width * 0.92))
        region = rgb[top:bottom, left:right]
        if region.size == 0:
            return None
        cyan_mask = ((region[:, :, 0] < 130) & (region[:, :, 1] > 180) & (region[:, :, 2] > 180)).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(cyan_mask * 255, 8)
        best: tuple[int, int] | None = None
        best_area = 0
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 400 or comp_width < 90 or comp_height < 20:
                continue
            if y < round(region.shape[0] * 0.50):
                continue
            if area > best_area:
                centroid_x, centroid_y = centroids[index]
                best = (
                    left + int(round(centroid_x)),
                    top + int(round(centroid_y)),
                )
                best_area = int(area)
        return best

    @staticmethod
    def _visible_hand_drag_starts(screenshot: Image.Image) -> list[tuple[int, int]]:
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        top = max(0, round(height * 0.52))
        left = max(0, round(width * 0.15))
        right = min(width, round(width * 0.90))
        region = rgb[top:height, left:right]
        if region.size == 0:
            return []
        badge_mask = (
            (region[:, :, 0] > 150)
            & (region[:, :, 1] < 135)
            & (region[:, :, 2] < 110)
        ).astype(np.uint8)
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(badge_mask * 255, 8)
        badges: list[tuple[float, float]] = []
        for index in range(1, num_labels):
            x, y, comp_width, comp_height, area = stats[index]
            if area < 600 or area > 1800:
                continue
            if comp_width < 28 or comp_width > 60 or comp_height < 28 or comp_height > 60:
                continue
            centroid_x, centroid_y = centroids[index]
            badges.append((left + float(centroid_x), top + float(centroid_y)))
        badges.sort(key=lambda point: point[0])
        starts: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        offsets = [(0, 0)]
        min_y = round(height * 0.60)
        max_y = height - 18
        for badge_x, badge_y in badges:
            for delta_x, delta_y in offsets:
                point = (
                    int(round(min(max(18, badge_x + delta_x), width - 18))),
                    int(round(min(max(min_y, badge_y + delta_y), max_y))),
                )
                if point in seen:
                    continue
                seen.add(point)
                starts.append(point)
        return starts

    @staticmethod
    def _frame_diff_score(previous: Image.Image, current: Image.Image) -> float:
        diff = ImageChops.difference(previous.convert("RGB"), current.convert("RGB"))
        stat = ImageStat.Stat(diff)
        return float(sum(stat.mean) / len(stat.mean))

    @staticmethod
    def _hand_diff_score(previous: Image.Image, current: Image.Image) -> float:
        width, height = previous.size
        box = (
            int(width * 0.15),
            int(height * 0.58),
            int(width * 0.90),
            height,
        )
        previous_region = previous.convert("RGB").crop(box)
        current_region = current.convert("RGB").crop(box)
        diff = ImageChops.difference(previous_region, current_region)
        stat = ImageStat.Stat(diff)
        return float(sum(stat.mean) / len(stat.mean))

    @staticmethod
    def _is_blank_capture(image: Image.Image) -> bool:
        grayscale = image.convert("L")
        extrema = grayscale.getextrema()
        if extrema is None:
            return True
        low, high = extrema
        return high <= 5 or (high - low) <= 3
