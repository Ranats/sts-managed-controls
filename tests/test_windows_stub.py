from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from sts_bot.adapters.windows_stub import CardPlayAttempt, WindowsStsAdapter
from sts_bot.config import ActionDefinition, Rect
from sts_bot.managed_probe import ManagedEnemySnapshot, ManagedPowerSnapshot, ManagedProbeSnapshot
from sts_bot.memory_reader import MemoryFieldResult, MemoryReadSnapshot
from sts_bot.models import ActionKind, BattleCardObservation, BattleTargetKind, DeckCard, EnemyState, GameAction, GameState, ScreenKind
from sts_bot.vision import TemplateMatch


class WindowsBattleHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")

    def test_target_detection_distinguishes_attack_selection(self) -> None:
        attack_image = Image.open(self.root / "captures" / "preview_key_1.png")
        non_target_image = Image.open(self.root / "captures" / "preview_key_3.png")

        self.assertTrue(self.adapter._selection_requires_target(attack_image))
        self.assertFalse(self.adapter._selection_requires_target(non_target_image))

    def test_target_detection_does_not_treat_gamepad_focus_card_as_target_selection(self) -> None:
        focus_image = Image.open(self.root / "captures" / "after_manual_end_turn.png")

        self.assertFalse(self.adapter._selection_requires_target(focus_image))

    def test_zero_energy_heuristic_detects_empty_orb(self) -> None:
        zero_energy = Image.open(self.root / "captures" / "after_10_more_iters.png")
        full_energy = Image.open(self.root / "captures" / "after_manual_end_turn.png")

        self.assertTrue(self.adapter._looks_like_zero_energy(zero_energy))
        self.assertFalse(self.adapter._looks_like_zero_energy(full_energy))

    def test_drag_origin_is_detected_for_non_target_selection(self) -> None:
        non_target_image = Image.open(self.root / "captures" / "preview_key_3.png")
        origin = self.adapter._selected_card_drag_origin(non_target_image)

        self.assertIsNotNone(origin)
        assert origin is not None
        self.assertGreater(origin[0], 500)
        self.assertLess(origin[1], 340)

    def test_fallback_drag_origin_detects_live_non_target_selection(self) -> None:
        selected_image = Image.open(self.root / "captures" / "skill_live_slot3_selected.png")
        base_image = Image.open(self.root / "captures" / "skill_live_base_ready.png")

        selected_origin = self.adapter._fallback_selected_card_drag_origin(selected_image)
        base_origin = self.adapter._fallback_selected_card_drag_origin(base_image)

        self.assertIsNotNone(selected_origin)
        self.assertIsNone(base_origin)

    def test_selected_card_cost_reads_one_cost_guard(self) -> None:
        screenshot = Image.open(self.root / "captures" / "slot3_defend_before.png")

        self.assertEqual(self.adapter._selected_card_cost(screenshot), 1)

    def test_selected_card_cost_reads_zero_cost_bloodletting(self) -> None:
        screenshot = Image.open(self.root / "captures" / "manual_bloodletting_before.png")

        self.assertEqual(self.adapter._selected_card_cost(screenshot), 0)

    @patch("sts_bot.adapters.windows_stub.extract_text", return_value="17")
    def test_selected_card_cost_rejects_outlier_value(self, _mock_extract) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_current_battle_20260402.png")

        self.assertIsNone(self.adapter._selected_card_cost(screenshot))

    def test_visible_hand_drag_starts_detect_live_badges(self) -> None:
        screenshot = Image.new("RGB", (1600, 900), "black")
        for center_x, center_y in [(500, 760), (660, 735), (820, 725), (980, 740), (1140, 770)]:
            for x in range(center_x - 20, center_x + 20):
                for y in range(center_y - 20, center_y + 20):
                    if (x - center_x) ** 2 + (y - center_y) ** 2 <= 18 ** 2:
                        screenshot.putpixel((x, y), (210, 85, 55))

        starts = self.adapter._visible_hand_drag_starts(screenshot)

        self.assertEqual(len(starts), 5)
        xs = [point[0] for point in starts]
        self.assertGreater(min(xs), 420)
        self.assertLess(max(xs), 1180)

    def test_infer_enemy_target_points_uses_upper_battlefield_candidates(self) -> None:
        attack_image = Image.open(self.root / "captures" / "battle_turn2_key2_select.png")

        candidates = self.adapter._infer_enemy_target_points(attack_image)

        self.assertGreaterEqual(len(candidates), 2)
        self.assertTrue(any(y < attack_image.height * 0.45 for _, y in candidates))

    def test_extract_battle_enemies_reads_enemy_hp_bar(self) -> None:
        screenshot = Image.open(self.root / "captures" / "battle_enemy_hp_work.png")

        enemies = self.adapter._extract_battle_enemies(screenshot, include_hp_text=True)

        self.assertGreaterEqual(len(enemies), 1)
        rightmost = max(enemies, key=lambda enemy: enemy.x)
        self.assertGreater(rightmost.width, 60)
        self.assertEqual(rightmost.max_hp, 39)
        self.assertTrue(rightmost.hp is None or rightmost.hp == 12)
        self.assertIsNotNone(rightmost.hp_text)
        self.assertGreaterEqual(rightmost.status_icon_count, 1)

    def test_battle_potion_slots_detect_current_live_layout(self) -> None:
        screenshot = Image.open(self.root / "captures" / "potion_test_start.png")

        self.assertEqual(self.adapter._battle_potion_slots(screenshot), [1, 2])

    def test_battle_offers_visible_potion_actions(self) -> None:
        screenshot = Image.open(self.root / "captures" / "potion_test_start.png")

        labels = [action.label for action in self.adapter._actions_for_screen(ScreenKind.BATTLE, screenshot)]

        self.assertIn("Use potion 1", labels)
        self.assertIn("Use potion 2", labels)

    def test_parse_enemy_hp_text_trims_common_ocr_tail_noise(self) -> None:
        parsed = self.adapter._parse_enemy_hp_text("12/329")

        self.assertEqual(parsed, (12, 32))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_use_battle_potion_falls_back_to_background_click(self, _mock_sleep) -> None:
        screenshot = Image.open(self.root / "captures" / "potion_test_start.png")
        definition = next(item for item in self.adapter.profile.actions if item.label == "Use potion 1")

        class FakeBackend:
            def __init__(self, backend_name: str) -> None:
                self.backend_name = backend_name
                self.clicked: list[tuple[int, int]] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def diagnostics(self):
                return SimpleNamespace(backend=self.backend_name)

        primary_backend = FakeBackend("gamepad")
        click_backend = FakeBackend("window_messages")

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                        with patch.object(self.adapter, "_battle_potion_region_diff", return_value=4.5):
                            with patch.object(self.adapter, "_resolve_click_backend", return_value=(click_backend, False)):
                                with patch.object(self.adapter, "_use_battle_potion_with_gamepad", return_value=None):
                                    backend_name = self.adapter._use_battle_potion(primary_backend, definition, backend="gamepad")

        self.assertEqual(backend_name, "window_messages")
        self.assertEqual(primary_backend.clicked, [])
        self.assertEqual(len(click_backend.clicked), 1)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_use_battle_potion_prefers_gamepad_slot_sequence_when_slot_disappears(self, _mock_sleep) -> None:
        screenshot = Image.open(self.root / "captures" / "potion_test_start.png")
        definition = next(item for item in self.adapter.profile.actions if item.label == "Use potion 1")

        class FakeBackend:
            def __init__(self, backend_name: str) -> None:
                self.backend_name = backend_name
                self.clicked: list[tuple[int, int]] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del x, y, button, double
                self.clicked.append((0, 0))

            def diagnostics(self):
                return SimpleNamespace(backend=self.backend_name)

        primary_backend = FakeBackend("gamepad")

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                    with patch.object(self.adapter, "_battle_potion_slots", side_effect=[[1, 2], [2]]):
                        with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                            backend_name = self.adapter._use_battle_potion(primary_backend, definition, backend="gamepad")

        self.assertEqual(backend_name, "gamepad")
        press_gamepad.assert_any_call(["x"], hold_ms=80, gap_ms=90)
        self.assertEqual(primary_backend.clicked, [])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_use_battle_potion_confirms_throw_menu_with_enter_and_resolves_target(self, _mock_sleep) -> None:
        screenshot = Image.open(self.root / "captures" / "potion_test_start.png")
        definition = next(item for item in self.adapter.profile.actions if item.label == "Use potion 2")

        class FakeBackend:
            def __init__(self, backend_name: str) -> None:
                self.backend_name = backend_name
                self.clicked: list[tuple[int, int]] = []
                self.pressed: list[str] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del x, y, button, double
                self.clicked.append((0, 0))

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend=self.backend_name)

            def close(self) -> None:
                return None

        primary_backend = FakeBackend("gamepad")
        wm_backend = FakeBackend("window_messages")

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy(), screenshot.copy()]):
            with patch.object(self.adapter, "_selection_requires_target", side_effect=[False, True]):
                with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                    with patch.object(self.adapter, "_battle_potion_slots", side_effect=[[1, 2], [1, 2], [1, 2]]):
                        with patch.object(self.adapter, "_resolve_input_backend", return_value=wm_backend):
                            with patch.object(
                                self.adapter,
                                "_resolve_targeted_card",
                                return_value=CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played"),
                            ) as resolve_target:
                                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                    backend_name = self.adapter._use_battle_potion(primary_backend, definition, backend="gamepad")

        self.assertEqual(backend_name, "window_messages")
        press_gamepad.assert_any_call(["x", "dpad_right"], hold_ms=80, gap_ms=90)
        self.assertIn("enter", wm_backend.pressed)
        resolve_target.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_resolve_targeted_card_cancels_when_target_selection_stays_active(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        class FakeBackend:
            def __init__(self) -> None:
                self.clicked: list[tuple[int, int]] = []
                self.moved: list[tuple[int, int]] = []
                self.pressed: list[str] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def move(self, x: int, y: int) -> None:
                self.moved.append((x, y))

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_resolve_targeted_card_with_navigation", return_value=None):
            with patch.object(self.adapter, "_target_candidate_points", return_value=[(430, 189), (539, 189)]):
                with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
                    with patch.object(self.adapter, "_selection_requires_target", return_value=True):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                            attempt = self.adapter._resolve_targeted_card(fake_backend, screenshot)

        self.assertFalse(attempt.played)
        self.assertEqual(attempt.reason, "target_selection_failed")
        self.assertEqual(fake_backend.clicked, [(430, 189), (539, 189)])
        self.assertEqual(fake_backend.moved, [(430, 189), (539, 189)])
        self.assertEqual(fake_backend.pressed, ["enter", "enter", "down"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_resolve_targeted_card_prefers_keyboard_navigation(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        class FakeBackend:
            def __init__(self) -> None:
                self.clicked: list[tuple[int, int]] = []
                self.pressed: list[str] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del x, y, button, double
                self.clicked.append((x, y))

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1047, 588), "white")):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=8.5):
                        attempt = self.adapter._resolve_targeted_card(fake_backend, screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_navigation")
        self.assertEqual(fake_backend.pressed, ["up", "right", "enter"])
        self.assertEqual(fake_backend.clicked, [])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_resolve_targeted_card_uses_hover_enter_before_click(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        class FakeBackend:
            def __init__(self) -> None:
                self.clicked: list[tuple[int, int]] = []
                self.moved: list[tuple[int, int]] = []
                self.pressed: list[str] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def move(self, x: int, y: int) -> None:
                self.moved.append((x, y))

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_resolve_targeted_card_with_navigation", return_value=None):
            with patch.object(self.adapter, "_target_candidate_points", return_value=[(430, 189)]):
                with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1047, 588), "white")):
                    with patch.object(self.adapter, "_selection_requires_target", side_effect=[True, False, False]):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                            with patch.object(self.adapter, "_frame_diff_score", return_value=8.5):
                                attempt = self.adapter._resolve_targeted_card(fake_backend, screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_hover_enter")
        self.assertEqual(fake_backend.moved, [(430, 189)])
        self.assertEqual(fake_backend.pressed, ["enter"])
        self.assertEqual(fake_backend.clicked, [])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_resolve_targeted_card_uses_hand_diff_as_success_signal(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1047, 588), "white")):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=1.0):
                        with patch.object(self.adapter, "_hand_diff_score", return_value=12.0):
                            attempt = self.adapter._resolve_targeted_card(fake_backend, screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_navigation")

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_resolve_targeted_card_navigation_continues_after_clear_without_progress(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[Image.new("RGB", (1047, 588), "white"), Image.new("RGB", (1047, 588), "white")],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(
                        self.adapter,
                        "_assess_target_resolution",
                        side_effect=[
                            CardPlayAttempt(backend="window_messages", played=False, reason="target_selection_navigation_cleared_without_resolution"),
                            CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played_navigation"),
                        ],
                    ):
                        attempt = self.adapter._resolve_targeted_card_with_navigation(fake_backend, screenshot)

        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_navigation")
        self.assertEqual(fake_backend.pressed, ["up", "right", "enter", "up", "left", "enter"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_attempt_play_card_slot_gamepad_skips_zero_cost_non_target_cards(self, _mock_sleep) -> None:
        before = Image.new("RGB", (1280, 720), "black")
        after = Image.new("RGB", (1280, 720), "white")

        with patch.object(self.adapter, "_capture_window_image", side_effect=[before, after, before]):
            with patch.object(self.adapter, "_press_gamepad_sequence") as press_sequence:
                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                    with patch.object(self.adapter, "_selected_card_drag_origin", return_value=(640, 300)):
                        with patch.object(self.adapter, "_selected_card_cost", return_value=0):
                            attempt = self.adapter._attempt_play_card_slot_with_gamepad(3)

        self.assertFalse(attempt.played)
        self.assertEqual(attempt.reason, "skip_zero_cost_non_target:slot_3")
        press_sequence.assert_called()

    def test_card_grid_buttons_focus_top_left_then_confirm(self) -> None:
        definition = ActionDefinition(
            screen=ScreenKind.CARD_GRID,
            kind=ActionKind.PICK_CARD,
            label="Card slot 1",
            point=(294, 294),
            payload={"card": "slot_1"},
        )

        buttons = self.adapter._card_grid_buttons(definition)

        self.assertEqual(buttons, ["dpad_right", "a", "a"])

    def test_card_grid_buttons_navigate_second_row(self) -> None:
        definition = ActionDefinition(
            screen=ScreenKind.CARD_GRID,
            kind=ActionKind.PICK_CARD,
            label="Card slot 8",
            point=(725, 631),
            payload={"card": "slot_8"},
        )

        buttons = self.adapter._card_grid_buttons(definition)

        self.assertEqual(buttons, ["dpad_right", "dpad_right", "dpad_right", "dpad_down", "a", "a"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_pick_card_grid_card_falls_back_to_click_and_confirm_when_gamepad_stays_on_grid(self, _mock_sleep) -> None:
        screenshot = Image.open(self.root / "captures" / "act1_neow_card_grid_current.png")
        definition = ActionDefinition(
            screen=ScreenKind.CARD_GRID,
            kind=ActionKind.PICK_CARD,
            label="Card slot 3",
            point=(725, 294),
            payload={"card": "slot_3"},
        )

        class FakeBackend:
            def __init__(self) -> None:
                self.clicked: list[tuple[int, int]] = []

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fallback_backend = FakeBackend()
        self.adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=SimpleNamespace())

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                with patch.object(self.adapter, "_resolve_input_backend", return_value=fallback_backend):
                    with patch.object(self.adapter, "_scale_reference_point", side_effect=lambda point: point):
                        backend = self.adapter._pick_card_grid_card(definition, backend="gamepad")

        self.assertEqual(backend, "window_messages")
        press_gamepad.assert_called_once()
        self.assertEqual(len(fallback_backend.clicked), 2)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_relies_on_attempt_result(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=None)

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []
                self.clicked: list[tuple[int, int]] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()
        screenshot = Image.new("RGB", (1047, 588), "black")
        attempts = [
            CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played", visual_score=8.0),
            CardPlayAttempt(backend="window_messages", played=False, reason="card_not_selectable:slot_1"),
            CardPlayAttempt(backend="window_messages", played=False, reason="card_not_selectable:slot_2"),
        ]

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
                with patch.object(self.adapter, "_attempt_probe_drag_play", side_effect=[None, None]):
                    with patch.object(self.adapter, "_attempt_play_card_slot", side_effect=attempts):
                        played = self.adapter.play_basic_battle_turn(backend="window_messages", max_slots=2)

        self.assertEqual(played, ["slot=1:window_messages"])
        self.assertEqual(fake_backend.pressed, ["e"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_does_not_count_selected_card_as_progress(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=None)

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="gamepad")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()
        before = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_backend_name_for_actions", return_value="gamepad"):
            with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
                with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=before.copy()):
                    with patch.object(
                        self.adapter,
                        "_attempt_resolve_selected_gamepad_card",
                        return_value=None,
                    ):
                        with patch.object(
                            self.adapter,
                            "_attempt_play_card_slot",
                            return_value=CardPlayAttempt(backend="gamepad", played=False, reason="selection_stuck"),
                        ):
                            with patch.object(self.adapter, "_battle_progress_made", return_value=True):
                                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                                    with patch.object(
                                        self.adapter,
                                        "_selected_card_drag_origin",
                                        side_effect=[None, (600, 300), (600, 300), (600, 300)],
                                    ):
                                        with patch.object(self.adapter, "_press_gamepad_sequence") as press_sequence:
                                            played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        press_sequence.assert_called_once_with(["dpad_down"], hold_ms=70, gap_ms=60)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_infers_progress_from_final_settled_battle_state(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        before = Image.new("RGB", (1047, 588), "black")
        after = Image.new("RGB", (1047, 588), "white")

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[before.copy(), before.copy(), after.copy()]):
            with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[]):
                with patch.object(self.adapter, "_looks_like_zero_energy", return_value=False):
                    with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                            with patch.object(
                                self.adapter,
                                "_attempt_probe_drag_play",
                                return_value=None,
                            ):
                                with patch.object(
                                    self.adapter,
                                    "_attempt_play_card_slot",
                                    return_value=CardPlayAttempt(backend="gamepad", played=False, reason="no_visual_progress:slot_1"),
                                ):
                                    with patch.object(self.adapter, "_battle_progress_made", side_effect=[False, True]):
                                        with patch.object(self.adapter, "_end_battle_turn", return_value="gamepad") as end_turn:
                                            played = self.adapter.play_basic_battle_turn(
                                                backend="gamepad",
                                                max_slots=1,
                                                time_budget_seconds=0.8,
                                            )

        self.assertEqual(played, ["progress:inferred"])
        end_turn.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_attempt_resolve_selected_gamepad_card_resolves_targeted_selection_first(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_selection_requires_target", return_value=True):
            with patch.object(
                self.adapter,
                "_resolve_targeted_card_with_gamepad",
                return_value=CardPlayAttempt(backend="gamepad", played=True, reason="targeted_card_played_gamepad"),
            ) as resolve_targeted:
                attempt = self.adapter._attempt_resolve_selected_gamepad_card(screenshot)

        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertTrue(attempt.played)
        resolve_targeted.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_attempt_resolve_selected_gamepad_card_falls_back_to_background_target_resolution(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_selection_requires_target", return_value=True):
            with patch.object(
                self.adapter,
                "_resolve_targeted_card_with_gamepad",
                return_value=CardPlayAttempt(backend="gamepad", played=False, reason="target_selection_gamepad_failed"),
            ) as resolve_targeted:
                with patch.object(
                    self.adapter,
                    "_resolve_targeted_card_with_background_messages",
                    return_value=CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played_hover_enter"),
                ) as resolve_background:
                    attempt = self.adapter._attempt_resolve_selected_gamepad_card(screenshot)

        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertTrue(attempt.played)
        self.assertEqual(attempt.backend, "window_messages")
        resolve_targeted.assert_called_once()
        resolve_background.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_prefers_resolving_selected_gamepad_card(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=None)
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_backend_name_for_actions", return_value="gamepad"):
            with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                    with patch.object(
                        self.adapter,
                        "_selected_card_drag_origin",
                        side_effect=[(600, 300), None, None, None, None, None],
                    ):
                        with patch.object(
                            self.adapter,
                            "_attempt_resolve_selected_gamepad_card",
                            return_value=CardPlayAttempt(backend="gamepad", played=True, reason="resolved_selected"),
                        ) as resolve_selected:
                            with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                                with patch.object(
                                    self.adapter,
                                    "_attempt_play_card_slot",
                                    return_value=CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                                ) as attempt_play:
                                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_sequence:
                                        played = self.adapter.play_basic_battle_turn(
                                            backend="gamepad",
                                            max_slots=2,
                                            time_budget_seconds=1.0,
                                        )

        self.assertEqual(played[0], "selected:gamepad")
        self.assertGreaterEqual(len(played), 1)
        resolve_selected.assert_called()
        press_sequence.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_uses_probe_drag_fallback(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=None)

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
                with patch.object(
                    self.adapter,
                    "_attempt_play_card_slot",
                    return_value=CardPlayAttempt(backend="window_messages", played=False, reason="card_not_selectable:slot_1"),
                ):
                    with patch.object(
                        self.adapter,
                        "_attempt_probe_drag_play",
                        side_effect=[
                            CardPlayAttempt(backend="window_messages", played=True, reason="probe_drag_card_played"),
                            None,
                        ],
                    ):
                        played = self.adapter.play_basic_battle_turn(backend="window_messages", max_slots=1)

        self.assertEqual(played, ["probe:window_messages"])
        self.assertEqual(fake_backend.pressed, ["e"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_attempt_probe_drag_play_prefers_planned_slots(self, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")
        starts = [(120, 400), (240, 400), (360, 400)]

        class FakeBackend:
            def __init__(self) -> None:
                self.drags: list[tuple[int, int, int, int]] = []

            def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
                del duration_ms
                self.drags.append((x1, y1, x2, y2))

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
                with patch.object(self.adapter, "_scale_reference_point", side_effect=lambda point: point):
                    with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=lambda *args, **kwargs: screenshot.copy()):
                        with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=starts):
                            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                                    with patch.object(self.adapter, "_frame_diff_score", return_value=5.0):
                                        with patch.object(self.adapter, "_hand_diff_score", return_value=0.0):
                                            attempt = self.adapter._attempt_probe_drag_play(
                                                backend="window_messages",
                                                preferred_slots=[3, 1],
                                            )

        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertTrue(attempt.played)
        self.assertEqual(fake_backend.drags[0][:2], starts[2])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_gamepad_uses_probe_drag_fallback_for_playable_plan(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=1,
            hp=80,
            max_hp=80,
            energy=3,
            gold=99,
            character="Ironclad",
        )
        screenshot = Image.new("RGB", (1047, 588), "black")
        slot_plan = [
            BattleCardObservation(
                slot=2,
                playable=True,
                energy_cost=1,
                target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                card_name="Defend",
                block=5,
                score=8.0,
            )
        ]

        with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=lambda *args, **kwargs: screenshot.copy()):
            with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(100, 400), (220, 400)]):
                with patch.object(self.adapter, "_looks_like_zero_energy", return_value=False):
                    with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                            with patch.object(self.adapter, "_planned_battle_cards", return_value=slot_plan):
                                with patch.object(
                                    self.adapter,
                                    "_attempt_play_card_slot",
                                    return_value=CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_2"),
                                ):
                                    with patch.object(
                                        self.adapter,
                                        "_attempt_probe_drag_play",
                                        side_effect=[
                                            CardPlayAttempt(backend="window_messages", played=True, reason="probe_drag_card_played"),
                                            None,
                                        ],
                                    ) as probe_drag:
                                        with patch.object(self.adapter, "_end_battle_turn", return_value="gamepad") as end_turn:
                                            played = self.adapter.play_basic_battle_turn(
                                                backend="gamepad",
                                                max_slots=2,
                                                time_budget_seconds=1.0,
                                            )

        self.assertEqual(played, ["probe:window_messages"])
        self.assertEqual(probe_drag.call_args_list[0].kwargs, {"backend": "window_messages", "preferred_slots": [2]})
        end_turn.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_attempt_probe_drag_play_resolves_target_after_drag_selection(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=None, transform=SimpleNamespace(refresh=lambda: None, reference_to_client=lambda point: point))

        class FakeBackend:
            def __init__(self) -> None:
                self.drags: list[tuple[int, int, int, int]] = []
                self.pressed: list[str] = []

            def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int) -> None:
                del duration_ms
                self.drags.append((x1, y1, x2, y2))

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()
        frames = [
            Image.new("RGB", (1047, 588), "black"),
            Image.new("RGB", (1047, 588), "black"),
            Image.new("RGB", (1047, 588), "white"),
        ]

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", side_effect=frames):
                with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(400, 500)]):
                    with patch.object(self.adapter, "_target_candidate_points", return_value=[(800, 200)]):
                        with patch.object(self.adapter, "_selection_requires_target", return_value=True):
                            with patch.object(
                                self.adapter,
                                "_resolve_targeted_card",
                                return_value=CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played", visual_score=10.0),
                            ):
                                attempt = self.adapter._attempt_probe_drag_play(backend="window_messages")

        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "probe_drag_then_targeted_card_played")
        self.assertEqual(fake_backend.drags, [(400, 500, 861, 469)])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_prefers_probe_drag_for_window_messages(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace(diagnostics=lambda: SimpleNamespace(backend="window_messages")))

        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1047, 588), "black")):
                with patch.object(
                    self.adapter,
                    "_attempt_probe_drag_play",
                    side_effect=[
                        CardPlayAttempt(backend="window_messages", played=True, reason="probe_drag_card_played"),
                        None,
                    ],
                ):
                    with patch.object(
                        self.adapter,
                        "_attempt_play_card_slot",
                        return_value=CardPlayAttempt(backend="window_messages", played=False, reason="card_not_selectable:slot_1"),
                    ) as attempt_slot:
                        played = self.adapter.play_basic_battle_turn(backend="window_messages", max_slots=2)

        self.assertEqual(played, ["probe:window_messages"])
        self.assertGreaterEqual(attempt_slot.call_count, 1)
        self.assertEqual(fake_backend.pressed, ["e"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_confirms_non_target_selection(self, mock_press, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[screenshot.copy(), screenshot.copy(), Image.new("RGB", (1047, 588), "white")],
        ):
            with patch.object(self.adapter, "_selection_requires_target", side_effect=[False, False]):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", side_effect=[0.5, 8.5]):
                        with patch.object(self.adapter, "_hand_diff_score", return_value=0.5):
                            attempt = self.adapter._attempt_play_card_slot(1, backend="gamepad")

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "gamepad_card_played")
        mock_press.assert_called_once_with(["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "a"], settle_ms=0, hold_ms=80, gap_ms=80)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_uses_window_drag_for_non_target_selection(self, mock_press, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[screenshot.copy(), screenshot.copy()],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=(420, 320)):
                    with patch.object(
                        self.adapter,
                        "_resolve_non_target_card_with_gamepad",
                        return_value=CardPlayAttempt(backend="gamepad", played=False, reason="gamepad_selected_card_failed"),
                    ):
                        with patch.object(
                            self.adapter,
                            "_resolve_non_target_card_with_background_drag",
                            return_value=CardPlayAttempt(backend="window_messages", played=True, reason="background_drag_card_played"),
                        ) as resolve_drag:
                            attempt = self.adapter._attempt_play_card_slot(1, backend="gamepad")

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.backend, "window_messages")
        resolve_drag.assert_called_once()
        mock_press.assert_called_once_with(["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "a"], settle_ms=0, hold_ms=80, gap_ms=80)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_resolve_non_target_card_with_gamepad_tries_double_confirm_first(self, mock_press, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[Image.new("RGB", (1047, 588), "white")],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=8.5):
                        attempt = self.adapter._resolve_non_target_card_with_gamepad(screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "gamepad_selected_card_played")
        self.assertEqual([call.args[0] for call in mock_press.call_args_list], [["a"]])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_resolve_non_target_card_with_gamepad_waits_for_delayed_confirmation(self, mock_press, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[screenshot.copy(), Image.new("RGB", (1047, 588), "white")],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=1.0):
                        with patch.object(self.adapter, "_hand_diff_score", return_value=0.5):
                            with patch.object(self.adapter, "_enemy_progress_score", return_value=0.5):
                                with patch.object(self.adapter, "_battle_progress_made", side_effect=[False, True]):
                                    attempt = self.adapter._resolve_non_target_card_with_gamepad(screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "gamepad_selected_card_played")
        self.assertEqual([call.args[0] for call in mock_press.call_args_list], [["a"]])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_resolve_non_target_card_with_gamepad_assumes_success_after_clear(self, mock_press, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            return_value=Image.new("RGB", (1047, 588), "white"),
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(
                        self.adapter,
                        "_assess_non_target_resolution",
                        return_value=CardPlayAttempt(
                            backend="gamepad",
                            played=False,
                            reason="gamepad_selection_cleared_without_resolution",
                            visual_score=1.5,
                        ),
                    ):
                        attempt = self.adapter._resolve_non_target_card_with_gamepad(screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "gamepad_selected_card_assumed_after_clear")
        self.assertEqual(attempt.visual_score, 1.5)
        self.assertEqual([call.args[0] for call in mock_press.call_args_list], [["a"]])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_resolve_non_target_card_with_gamepad_escalates_to_target_resolution(self, mock_press, _mock_sleep) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            return_value=screenshot.copy(),
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=True):
                with patch.object(
                    self.adapter,
                    "_resolve_targeted_card_with_gamepad",
                    return_value=CardPlayAttempt(backend="gamepad", played=True, reason="targeted_card_played_gamepad"),
                ) as resolve_targeted:
                    attempt = self.adapter._resolve_non_target_card_with_gamepad(screenshot)

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_gamepad")
        resolve_targeted.assert_called_once()
        self.assertEqual([call.args[0] for call in mock_press.call_args_list], [["a"]])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_tries_target_navigation_sequences(self, mock_press, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=3,
            hp=55,
            max_hp=80,
            energy=2,
            gold=100,
            character="Ironclad",
            enemies=[],
        )
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[
                screenshot.copy(),
                screenshot.copy(),
                screenshot.copy(),
                screenshot.copy(),
                screenshot.copy(),
                Image.new("RGB", (1047, 588), "white"),
            ],
        ):
            with patch.object(self.adapter, "_selection_requires_target", side_effect=[True, True, True, False, False, False, False]):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=8.5):
                        attempt = self.adapter._attempt_play_card_slot(2, backend="gamepad")

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.reason, "targeted_card_played_gamepad")
        self.assertEqual(
            [call.args[0] for call in mock_press.call_args_list],
            [
                ["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_right", "a"],
                ["a"],
                ["dpad_up", "a"],
            ],
        )

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_falls_back_to_background_target_resolution(self, mock_press, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[screenshot.copy(), screenshot.copy(), screenshot.copy()],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=True):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(
                        self.adapter,
                        "_resolve_targeted_card_with_gamepad",
                        return_value=CardPlayAttempt(backend="gamepad", played=False, reason="target_selection_gamepad_failed"),
                    ) as resolve_gamepad:
                        with patch.object(
                            self.adapter,
                            "_resolve_targeted_card_with_background_messages",
                            return_value=CardPlayAttempt(backend="window_messages", played=True, reason="targeted_card_played_hover_enter"),
                        ) as resolve_background:
                            attempt = self.adapter._attempt_play_card_slot(2, backend="gamepad")

        self.assertTrue(attempt.played)
        self.assertEqual(attempt.backend, "window_messages")
        resolve_gamepad.assert_called_once()
        resolve_background.assert_called_once()
        self.assertEqual(mock_press.call_args_list[0].args[0], ["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_right", "a"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_waits_for_delayed_target_selection(self, mock_press, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        before = Image.new("RGB", (1047, 588), "black")
        immediate_after = Image.new("RGB", (1047, 588), "black")
        delayed_target = Image.new("RGB", (1047, 588), "white")

        with patch.object(self.adapter, "_capture_window_image", side_effect=[before, immediate_after, delayed_target]):
            with patch.object(self.adapter, "_selection_requires_target", side_effect=[False, True, True]):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_frame_diff_score", return_value=2.0):
                        with patch.object(self.adapter, "_hand_diff_score", return_value=2.0):
                            with patch.object(
                                self.adapter,
                                "_resolve_targeted_card_with_gamepad",
                                return_value=CardPlayAttempt(backend="gamepad", played=True, reason="targeted_card_played_gamepad"),
                            ) as resolve_target:
                                attempt = self.adapter._attempt_play_card_slot_with_gamepad(2)

        self.assertTrue(attempt.played)
        resolve_target.assert_called_once()
        self.assertEqual(mock_press.call_args_list[0].args[0], ["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_right", "a"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_attempt_play_card_slot_gamepad_can_defer_targeted_slots(self, mock_press, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[screenshot.copy(), screenshot.copy(), screenshot.copy()],
        ):
            with patch.object(self.adapter, "_selection_requires_target", return_value=True):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    attempt = self.adapter._attempt_play_card_slot(1, backend="gamepad", prefer_non_target_only=True)

        self.assertFalse(attempt.played)
        self.assertEqual(attempt.reason, "defer_targeted:slot_1")
        self.assertEqual(
            [call.args[0] for call in mock_press.call_args_list],
            [
                ["dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "dpad_left", "a"],
                ["dpad_down"],
            ],
        )

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_uses_runtime_backend_for_gamepad_end_turn(self, _mock_sleep) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

        fake_backend = FakeBackend()
        self.adapter._runtime = SimpleNamespace(input_backend=fake_backend)

        with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1047, 588), "black")):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=True, reason="targeted_card_played_gamepad"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ):
                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                    played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, ["slot=1:gamepad"])
        self.assertEqual(fake_backend.pressed, [])
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_prefers_non_target_pass_for_gamepad(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=False, reason="defer_targeted:slot_1"),
                    CardPlayAttempt(backend="gamepad", played=True, reason="gamepad_selected_card_played"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ) as attempt_slot:
                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                        played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=2)

        self.assertEqual(played, ["slot=1:gamepad"])
        self.assertEqual(
            [call.kwargs["prefer_non_target_only"] for call in attempt_slot.call_args_list[:2]],
            [True, True],
        )
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_does_not_probe_live_slot_plan_for_gamepad(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ):
                with patch.object(self.adapter, "_planned_battle_cards") as planned_cards:
                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                        played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        planned_cards.assert_not_called()
        press_gamepad.assert_not_called()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_uses_reverse_slot_order_for_gamepad_non_target_pass(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=False, reason="defer_targeted:slot_3"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="defer_targeted:slot_2"),
                    CardPlayAttempt(backend="window_messages", played=True, reason="background_drag_card_played"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ) as attempt_slot:
                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                    played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=3)

        self.assertEqual(played, ["slot=1:window_messages"])
        self.assertEqual(
            [call.args[0] for call in attempt_slot.call_args_list[:3]],
            [3, 2, 1],
        )
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_limits_gamepad_slot_order_to_visible_hand(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(500, 520), (650, 520), (800, 520)]):
                        with patch.object(
                            self.adapter,
                            "_attempt_play_card_slot",
                            side_effect=[
                                CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_3"),
                                CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_2"),
                                CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                            ],
                        ) as attempt_slot:
                            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=10)

        self.assertEqual(played, [])
        self.assertEqual([call.args[0] for call in attempt_slot.call_args_list], [3, 2, 1])
        press_gamepad.assert_not_called()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_uses_visible_gamepad_slot_plan_when_available(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=0,
            gold=432,
            character="Ironclad",
            available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
        )
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(500, 520), (650, 520), (800, 520)]):
                        with patch.object(
                            self.adapter,
                            "_planned_battle_cards",
                            return_value=[
                                BattleCardObservation(slot=2, playable=True, energy_cost=1, target_kind=BattleTargetKind.SELF_OR_NON_TARGET, score=5.0),
                                BattleCardObservation(slot=1, playable=True, energy_cost=1, target_kind=BattleTargetKind.SELF_OR_NON_TARGET, score=4.0),
                            ],
                        ) as planned_cards:
                            with patch.object(
                                self.adapter,
                                "_attempt_play_card_slot",
                                side_effect=[
                                    CardPlayAttempt(backend="gamepad", played=True, reason="played_slot_2"),
                                ],
                            ) as attempt_slot:
                                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                    played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=10)

        self.assertEqual(played, ["slot=2:gamepad"])
        self.assertGreaterEqual(planned_cards.call_count, 1)
        self.assertEqual(attempt_slot.call_args_list[0].args[0], 2)
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_tracks_gained_block_across_gamepad_planning_loops(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            block=0,
            gold=432,
            character="Ironclad",
            available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
        )
        screenshot = Image.new("RGB", (1047, 588), "black")
        planning_blocks: list[int] = []

        def planned_cards(state, **_kwargs):
            planning_blocks.append(state.block)
            if len(planning_blocks) == 1:
                return [
                    BattleCardObservation(
                        slot=1,
                        playable=True,
                        energy_cost=1,
                        target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                        card_name="Defend",
                        block=5,
                        score=8.0,
                    )
                ]
            return []

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(500, 520)]):
                        with patch.object(self.adapter, "_looks_like_zero_energy", return_value=False):
                            with patch.object(self.adapter, "_planned_battle_cards", side_effect=planned_cards):
                                with patch.object(
                                    self.adapter,
                                    "_attempt_play_card_slot",
                                    side_effect=[
                                        CardPlayAttempt(backend="gamepad", played=True, reason="played_slot_1"),
                                    ],
                                ):
                                    with patch.object(self.adapter, "_end_battle_turn", return_value="gamepad"):
                                        self.adapter.play_basic_battle_turn(
                                            backend="gamepad",
                                            max_slots=1,
                                            time_budget_seconds=1.0,
                                        )

        self.assertEqual(planning_blocks[:2], [0, 5])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_gamepad_ends_turn_without_slot_probe_when_all_observed_cards_are_unplayable(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
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
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                    with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(500, 520), (650, 520)]):
                        with patch.object(
                            self.adapter,
                            "_planned_battle_cards",
                            return_value=[
                                BattleCardObservation(slot=1, playable=False, target_kind=BattleTargetKind.SELF_OR_NON_TARGET, score=-999.0),
                                BattleCardObservation(slot=2, playable=False, target_kind=BattleTargetKind.SELF_OR_NON_TARGET, score=-999.0),
                            ],
                        ):
                            with patch.object(self.adapter, "_attempt_play_card_slot") as attempt_slot:
                                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                    played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=10)

        self.assertEqual(played, [])
        attempt_slot.assert_not_called()
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_gamepad_stops_after_tracked_energy_reaches_zero(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=1,
            gold=432,
            character="Ironclad",
            metric_sources={"energy": "memory"},
            available_actions=[GameAction(ActionKind.PLAY_CARD, "Play basic turn")],
        )
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_looks_like_zero_energy", return_value=False):
                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                    with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                        with patch.object(self.adapter, "_visible_hand_drag_starts", return_value=[(500, 520)]):
                            with patch.object(
                                self.adapter,
                                "_planned_battle_cards",
                                return_value=[
                                    BattleCardObservation(
                                        slot=1,
                                        playable=True,
                                        energy_cost=1,
                                        target_kind=BattleTargetKind.SELF_OR_NON_TARGET,
                                        score=5.0,
                                    )
                                ],
                            ):
                                with patch.object(
                                    self.adapter,
                                    "_attempt_play_card_slot",
                                    return_value=CardPlayAttempt(backend="gamepad", played=True, reason="played_slot_1"),
                                ) as attempt_slot:
                                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                        played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=10)

        self.assertEqual(played, ["slot=1:gamepad"])
        attempt_slot.assert_called_once()
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_cancels_selection_before_gamepad_end_turn(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                return_value=CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
            ):
                with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                    with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=(400, 300)):
                            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        self.assertEqual(press_gamepad.call_args_list[-1].args[0], ["dpad_down"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_treats_visual_progress_as_success_for_gamepad(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ):
                with patch.object(self.adapter, "_battle_progress_made", side_effect=[True, False]):
                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                        played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, ["slot=1:gamepad"])
        press_gamepad.assert_called_once_with(["y"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_does_not_end_turn_without_confirmed_gamepad_progress(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_play_card_slot",
                side_effect=[
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                    CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1"),
                ],
            ):
                with patch.object(self.adapter, "_battle_progress_made", side_effect=[False, True]):
                    with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                        with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        press_gamepad.assert_not_called()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_does_not_count_assumed_clear_as_progress_without_visual_change(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")
        drag_origins = iter([(400, 300), None, None, None, None, None])

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(
                self.adapter,
                "_attempt_resolve_selected_gamepad_card",
                return_value=CardPlayAttempt(backend="gamepad", played=True, reason="gamepad_selected_card_assumed_after_clear"),
            ):
                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                    with patch.object(self.adapter, "_selected_card_drag_origin", side_effect=lambda _img: next(drag_origins, None)):
                        with patch.object(self.adapter, "_battle_progress_made", return_value=False):
                            with patch.object(self.adapter, "_attempt_play_card_slot", return_value=CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1")):
                                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                    played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        press_gamepad.assert_not_called()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_ignores_non_target_gamepad_focus_state(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                with patch.object(self.adapter, "_selected_card_drag_origin", return_value=(400, 360)):
                    with patch.object(self.adapter, "_attempt_resolve_selected_gamepad_card") as resolve_selected:
                        with patch.object(self.adapter, "_attempt_play_card_slot", return_value=CardPlayAttempt(backend="gamepad", played=False, reason="card_not_selectable:slot_1")):
                            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=1)

        self.assertEqual(played, [])
        resolve_selected.assert_not_called()
        press_gamepad.assert_called_once_with(["dpad_down"], hold_ms=70, gap_ms=60)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_play_basic_battle_turn_ends_turn_when_zero_energy_is_visible(self, _mock_sleep) -> None:
        self.adapter._runtime = SimpleNamespace(input_backend=SimpleNamespace())
        screenshot = Image.new("RGB", (1047, 588), "black")

        with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot.copy()):
            with patch.object(self.adapter, "_looks_like_zero_energy", return_value=True):
                with patch.object(self.adapter, "_selection_requires_target", return_value=False):
                    with patch.object(self.adapter, "_selected_card_drag_origin", return_value=None):
                        with patch.object(self.adapter, "_attempt_play_card_slot") as attempt_play:
                            with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                                played = self.adapter.play_basic_battle_turn(backend="gamepad", max_slots=3)

        self.assertEqual(played, [])
        attempt_play.assert_not_called()
        press_gamepad.assert_called_once_with(["y"])

    def test_battle_metric_cache_refreshes_when_zero_energy_is_visible(self) -> None:
        screenshot = Image.open(self.root / "captures" / "battle_resume_current.png")
        self.adapter._last_metrics = {"energy": 3}
        self.adapter._last_metric_read_at = time.time()

        with patch.object(self.adapter, "_read_metrics", return_value={"energy": 0}) as read_metrics:
            metrics = self.adapter._metrics_for_screen(screenshot, ScreenKind.BATTLE, read_metrics=True)

        self.assertEqual(metrics["energy"], 0)
        read_metrics.assert_called_once()


class WindowsRewardMenuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        self.adapter._runtime = SimpleNamespace(input_backend=None)

    def _fake_backend(self) -> object:
        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []
                self.clicked: list[tuple[int, int]] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        return FakeBackend()

    def test_execute_keys_uses_selected_backend(self) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.REWARD_MENU,
            kind=ActionKind.NAVIGATE,
            label="Navigate with keys",
            point=(0, 0),
            keys=["down", "enter"],
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertEqual(fake_backend.pressed, ["down", "enter"])

    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_execute_action_uses_gamepad_buttons_when_requested(self, mock_press) -> None:
        action = ActionDefinition(
            screen=ScreenKind.REWARD_CARDS,
            kind=ActionKind.PICK_CARD,
            label="Pick via gamepad",
            point=(0, 0),
            key="2",
            buttons=["a"],
        )

        backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        mock_press.assert_called_once_with(["a"], settle_ms=0, hold_ms=40, gap_ms=250)

    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_execute_action_does_not_force_gamepad_when_backend_is_explicit(self, mock_press) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.NEOW_DIALOG,
            kind=ActionKind.NAVIGATE,
            label="Advance dialog",
            point=(400, 400),
            buttons=["a"],
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_scale_reference_point", return_value=(400, 400)):
                backend = self.adapter._execute_action(action, backend="legacy")

        self.assertEqual(backend, "window_messages")
        mock_press.assert_not_called()

    def test_apply_action_uses_scene_backend_when_backend_is_auto(self) -> None:
        action = ActionDefinition(
            screen=ScreenKind.REWARD_CARDS,
            kind=ActionKind.PICK_CARD,
            label="Pick via gamepad",
            point=(0, 0),
            key="2",
            buttons=["a"],
        )
        self.adapter.profile.scene_input_backends[ScreenKind.REWARD_CARDS.value] = "gamepad"
        self.adapter._last_state = SimpleNamespace(screen=ScreenKind.REWARD_CARDS)
        self.adapter._last_screenshot = Image.new("RGB", (32, 32), "black")

        with patch.object(self.adapter, "_find_action_definition", return_value=action):
            with patch.object(self.adapter, "_record_action"):
                with patch.object(self.adapter, "_execute_action", return_value="gamepad") as mock_execute:
                    with patch.object(self.adapter, "_wait_for_change"):
                        backend = self.adapter.apply_action(
                            SimpleNamespace(kind=ActionKind.PICK_CARD, tags=[]),
                            backend=None,
                        )

        self.assertEqual(backend, "gamepad")
        mock_execute.assert_called_once_with(action, backend="gamepad", mode="auto")

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_open_card_reward_advances_until_screen_changes(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        reward_state = self.adapter.inspect_image_path(self.root / "captures" / "reward_card_choice.png")
        card_reward_state = self.adapter.inspect_image_path(self.root / "captures" / "reward_probe_now.png")

        with patch.object(
            self.adapter,
            "_capture_window_image",
            side_effect=[
                Image.open(self.root / "captures" / "reward_card_choice.png"),
                Image.open(self.root / "captures" / "reward_probe_now.png"),
            ],
        ):
            with patch.object(
                self.adapter,
                "inspect_image",
                side_effect=[reward_state, card_reward_state],
            ):
                backend = self.adapter._open_card_reward(fake_backend)

        self.assertEqual(backend, "window_messages")
        self.assertEqual(fake_backend.pressed, ["enter", "down", "enter"])

    def test_navigation_arrow_point_detects_right_arrow(self) -> None:
        screenshot = Image.new("RGB", (1200, 800), "black")
        for x in range(900, 1135):
            for y in range(620, 745):
                screenshot.putpixel((x, y), (185, 62, 44))

        point = self.adapter._navigation_arrow_point(screenshot, direction="right")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertGreater(point[0], 980)
        self.assertGreater(point[1], 650)

    def test_reward_card_point_detects_middle_card(self) -> None:
        screenshot = Image.open(self.root / "captures" / "manual_direct_bash_after.png")

        point = self.adapter._reward_card_point(screenshot, "Card option 2")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertGreater(point[0], 620)
        self.assertLess(point[0], 820)
        self.assertGreater(point[1], 360)
        self.assertLess(point[1], 560)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_continue_uses_navigation_arrow_helper(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.CONTINUE,
            kind=ActionKind.NAVIGATE,
            label="Continue",
            point=(100, 200),
            key="enter",
        )

        self.adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None)
        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=Image.new("RGB", (1200, 800), "black")):
                with patch.object(self.adapter, "_click_navigation_arrow", return_value="window_messages") as mock_click:
                    backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        mock_click.assert_called_once_with(fake_backend, direction="right", fallback_point=(100, 200), backend="window_messages")

    @patch("sts_bot.adapters.windows_stub.press_xbox_sequence")
    def test_execute_action_continue_uses_gamepad_y_when_requested(self, mock_press) -> None:
        action = ActionDefinition(
            screen=ScreenKind.CONTINUE,
            kind=ActionKind.NAVIGATE,
            label="Continue",
            point=(100, 200),
            key="enter",
            buttons=["y"],
            hold_ms=80,
        )

        backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        mock_press.assert_called_once_with(["y"], settle_ms=0, hold_ms=80, gap_ms=250)

    def test_generic_event_choice_capture_classifies_event(self) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_event_pick_that3.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.EVENT)
        self.assertEqual([action.label for action in state.available_actions], ["Event option 1", "Event option 2"])

    def test_single_option_event_capture_offers_proceed(self) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_event_proceed_start.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.EVENT)
        self.assertEqual([action.label for action in state.available_actions], ["Proceed event"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_generic_event_option_clicks_detected_choice(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.EVENT,
            kind=ActionKind.NAVIGATE,
            label="Event option 2",
            point=(0, 0),
            payload={"target": "generic_event_option", "option_index": 1},
        )
        screenshot = Image.open(self.root / "captures" / "continue_event_pick_that3.png")

        self.adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None)
        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot):
                backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertGreaterEqual(len(fake_backend.clicked), 1)
        click_x, click_y = fake_backend.clicked[0]
        self.assertGreater(click_x, 520)
        self.assertGreater(click_y, 330)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_generic_event_option_uses_window_messages_click_fallback_for_gamepad(self, _mock_sleep) -> None:
        class FakeGamepadBackend:
            def diagnostics(self):
                return SimpleNamespace(backend="gamepad")

            def close(self) -> None:
                return None

        fallback_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.EVENT,
            kind=ActionKind.NAVIGATE,
            label="Event option 2",
            point=(0, 0),
            payload={"target": "generic_event_option", "option_index": 1},
        )
        screenshot = Image.open(self.root / "captures" / "continue_event_pick_that3.png")
        self.adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None)

        def resolve_backend(name: str | None):
            if name == "gamepad":
                return FakeGamepadBackend()
            self.assertEqual(name, "window_messages")
            return fallback_backend

        with patch.object(self.adapter, "_resolve_input_backend", side_effect=resolve_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", return_value=screenshot):
                backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "window_messages")
        self.assertGreaterEqual(len(fallback_backend.clicked), 1)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_reward_pick_uses_reward_card_helper(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.REWARD_CARDS,
            kind=ActionKind.PICK_CARD,
            label="Card option 2",
            point=(780, 530),
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_click_reward_card", return_value="window_messages") as mock_click:
                backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        mock_click.assert_called_once_with(fake_backend, action)

    def test_detect_reward_variant_classifies_cards(self) -> None:
        screenshot = Image.open(self.root / "captures" / "reward_menu_enter_after.png")

        screen = self.adapter.inspect_image(screenshot, read_metrics=False).screen

        self.assertEqual(screen, ScreenKind.REWARD_CARDS)

    def test_partial_live_reward_capture_still_classifies_reward_cards(self) -> None:
        screenshot = Image.open(self.root / "captures" / "act1_battle2_postturn_result.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.REWARD_CARDS)
        self.assertIn("Card option 1", [action.label for action in state.available_actions])
        self.assertIn("Card option 2", [action.label for action in state.available_actions])

    def test_live_loot_menu_capture_classifies_reward_menu(self) -> None:
        screenshot = Image.open(self.root / "captures" / "reward_resume_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.REWARD_MENU)
        self.assertIn("Take gold", [action.label for action in state.available_actions])
        self.assertIn("Open card reward", [action.label for action in state.available_actions])

    @patch.object(WindowsStsAdapter, "_event_option_text", side_effect=["Lose 6 HP. Gain 100 Gold.", "Heal 20 HP."])
    def test_generic_event_choice_capture_includes_option_text_payloads(self, _mock_text) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_event_pick_that3.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.EVENT)
        self.assertEqual(state.available_actions[0].payload["option_text"], "Lose 6 HP. Gain 100 Gold.")
        self.assertEqual(state.available_actions[1].payload["option_text"], "Heal 20 HP.")

    @patch.object(
        WindowsStsAdapter,
        "_reward_menu_option_points",
        return_value=[
            ((520, 220), "gold"),
            ((520, 275), "card_reward"),
            ((520, 330), "relic_reward"),
            ((520, 385), "potion_reward"),
        ],
    )
    @patch.object(
        WindowsStsAdapter,
        "_reward_menu_row_kind",
        side_effect=lambda _screenshot, point: {
            (520, 220): "gold",
            (520, 275): "card_reward",
            (520, 330): "relic_reward",
            (520, 385): "potion_reward",
        }[point],
    )
    @patch.object(WindowsStsAdapter, "_reward_relic_actions", return_value=[])
    @patch.object(WindowsStsAdapter, "_reward_potion_actions", return_value=[])
    def test_reward_menu_dynamic_actions_surface_gold_card_relic_and_potion_rows(
        self,
        _mock_potion,
        _mock_relic,
        _mock_kind,
        _mock_points,
    ) -> None:
        screenshot = Image.open(self.root / "captures" / "reward_resume_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.REWARD_MENU)
        labels = [action.label for action in state.available_actions]
        self.assertIn("Take gold", labels)
        self.assertIn("Open card reward", labels)
        self.assertIn("Open relic reward", labels)
        self.assertIn("Open potion reward", labels)

    @patch.object(WindowsStsAdapter, "_reward_center_name", return_value="Anchor")
    def test_reward_relic_screen_classifies_and_surfaces_take_and_skip_actions(self, _mock_name) -> None:
        screenshot = Image.open(self.root / "captures" / "reward_resume_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.REWARD_RELIC)
        self.assertEqual([action.label for action in state.available_actions], ["Take Anchor", "Skip reward"])

    @patch.object(WindowsStsAdapter, "_reward_relic_actions", return_value=[])
    @patch.object(WindowsStsAdapter, "_reward_potion_actions", return_value=[])
    @patch.object(WindowsStsAdapter, "_boss_relic_actions", return_value=[])
    @patch.object(
        WindowsStsAdapter,
        "_shop_offer_definitions",
        return_value=[
            {
                "item_type": "card",
                "point": (240, 200),
                "name_region": Rect(0, 0, 10, 10),
                "price_region": Rect(0, 0, 10, 10),
            },
            {
                "item_type": "remove",
                "point": (860, 700),
                "name_region": Rect(0, 0, 10, 10),
                "price_region": Rect(0, 0, 10, 10),
            },
        ],
    )
    @patch.object(
        WindowsStsAdapter,
        "_extract_text_safe",
        side_effect=lambda _screenshot, name, _region, **_kwargs: {
            "shop_card_name": "Shrug It Off",
            "shop_card_price": "49",
            "shop_remove_name": "Remove card",
            "shop_remove_price": "75",
        }.get(name, ""),
    )
    def test_shop_screen_surfaces_buy_actions_with_item_type_and_price_payloads(
        self,
        _mock_extract,
        _mock_defs,
        _mock_boss,
        _mock_potion,
        _mock_relic,
    ) -> None:
        screenshot = Image.open(self.root / "captures" / "shop_loop_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        buy_actions = [action for action in state.available_actions if action.kind == ActionKind.BUY]
        self.assertEqual(len(buy_actions), 2)
        self.assertEqual(buy_actions[0].payload["shop_item_type"], "card")
        self.assertEqual(buy_actions[0].payload["price"], 49)
        self.assertEqual(buy_actions[0].payload["card"], "Shrug It Off")
        self.assertEqual(buy_actions[1].payload["shop_item_type"], "remove")
        self.assertEqual(buy_actions[1].payload["price"], 75)

    @patch.object(WindowsStsAdapter, "_reward_relic_actions", return_value=[])
    @patch.object(WindowsStsAdapter, "_reward_potion_actions", return_value=[])
    @patch.object(
        WindowsStsAdapter,
        "_boss_relic_name",
        side_effect=lambda _screenshot, option_index: ["Black Blood", "Coffee Dripper", "Sozu"][option_index],
    )
    def test_boss_relic_screen_classifies_and_surfaces_relic_choices(self, _mock_name, _mock_potion, _mock_relic) -> None:
        screenshot = Image.open(self.root / "captures" / "shop_loop_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.BOSS_RELIC)
        self.assertEqual(
            [action.label for action in state.available_actions],
            ["Take Black Blood", "Take Coffee Dripper", "Take Sozu", "Skip reward"],
        )

    def test_detect_reward_variant_classifies_gold_only(self) -> None:
        screenshot = Image.open(self.root / "captures" / "gold_only_after_gamepad_a.png")

        screen = self.adapter.inspect_image(screenshot, read_metrics=False).screen

        self.assertEqual(screen, ScreenKind.REWARD_GOLD_ONLY)

    def test_reward_gold_only_focus_detects_highlighted_state(self) -> None:
        focused = Image.open(self.root / "captures" / "gold_menu_after_dpad_down_a.png")
        unfocused = Image.open(self.root / "captures" / "gold_only_after_gamepad_a.png")

        self.assertTrue(self.adapter._reward_gold_only_focused(focused))
        self.assertFalse(self.adapter._reward_gold_only_focused(unfocused))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_reward_gold_only_focuses_then_confirms(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.REWARD_GOLD_ONLY,
            kind=ActionKind.NAVIGATE,
            label="Take gold and continue",
            point=(522, 228),
            payload={"target": "gold_only"},
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(
                self.adapter,
                "_capture_window_image",
                side_effect=[
                    Image.open(self.root / "captures" / "gold_only_after_gamepad_a.png"),
                    Image.open(self.root / "captures" / "gold_menu_after_dpad_down_a.png"),
                ],
            ):
                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                    backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        press_gamepad.assert_called_once_with(["dpad_down"], hold_ms=90, gap_ms=90)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_confirm_modal_tries_enter_then_click_until_screen_changes(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.CONFIRM_POPUP,
            kind=ActionKind.NAVIGATE,
            label="Confirm modal",
            point=(1432, 690),
            payload={"target": "confirm_modal"},
        )
        confirm_state = GameState(screen=ScreenKind.CONFIRM_POPUP, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        next_state = GameState(screen=ScreenKind.MAP, act=1, floor=1, hp=64, max_hp=80, energy=0, gold=99, character="Ironclad")
        screenshot = Image.open(self.root / "captures" / "live_seq3_1_confirm_popup.png")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_scale_reference_point", return_value=(1432, 690)):
                with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy(), screenshot.copy()]):
                    with patch.object(self.adapter, "inspect_image", side_effect=[confirm_state, next_state]):
                        backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertEqual(fake_backend.pressed, ["down", "enter"])
        self.assertEqual(len(fake_backend.clicked), 1)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_confirm_modal_prefers_gamepad_when_progress_is_detected(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.CONFIRM_POPUP,
            kind=ActionKind.NAVIGATE,
            label="Confirm modal",
            point=(1432, 690),
            payload={"target": "confirm_modal"},
        )
        screenshot = Image.open(self.root / "captures" / "live_seq3_1_confirm_popup.png")
        next_state = GameState(screen=ScreenKind.MAP, act=1, floor=1, hp=64, max_hp=80, energy=0, gold=99, character="Ironclad")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "inspect_image", return_value=next_state):
                    with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                        backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        self.assertEqual(fake_backend.clicked, [])
        self.assertEqual(fake_backend.pressed, [])
        press_gamepad.assert_called_once_with(["a"], hold_ms=90, gap_ms=90)

    def test_title_capture_is_not_misclassified_as_battle(self) -> None:
        screenshot = Image.open(self.root / "captures" / "title_current_state_before_gamepad_test.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertNotEqual(state.screen, ScreenKind.BATTLE)
        self.assertEqual(state.screen, ScreenKind.MENU)

    def test_live_title_capture_classifies_menu(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_title_after_raise.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MENU)

    def test_resized_live_title_capture_classifies_menu(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_probe_after_event_fix.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MENU)

    def test_resized_live_mode_select_capture_is_not_misclassified_as_event(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_probe_after_menu_fix.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MODE_SELECT)

    def test_resized_proceed_capture_classifies_continue(self) -> None:
        screenshot = Image.open(self.root / "captures" / "run_live_stopped_unknown.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CONTINUE)
        self.assertEqual([action.label for action in state.available_actions], ["Continue"])

    def test_shop_card_popup_classifies_shop_with_close_action(self) -> None:
        screenshot = Image.open(self.root / "captures" / "auto_after_zero_fix_unknown_13.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.SHOP)
        self.assertEqual([action.label for action in state.available_actions], ["Close detail popup"])

    def test_regular_shop_screen_classifies_shop_with_leave_action(self) -> None:
        screenshot = Image.open(self.root / "captures" / "shop_loop_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.SHOP)
        self.assertIn("Leave shop", [action.label for action in state.available_actions])

    def test_shop_continue_room_uses_room_heuristic(self) -> None:
        screenshot = Image.open(self.root / "captures" / "shop_continue_loop_screen.png")

        self.assertTrue(self.adapter._looks_like_shop_continue_room(screenshot))

    def test_live_character_select_capture_classifies_character_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_title_unknown_after_tests.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_resized_character_select_capture_classifies_character_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "title_resized_standard_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_live_character_select_capture_overrides_continue_false_positive(self) -> None:
        screenshot = Image.open(self.root / "captures" / "restart_after_standard_run_unexpected.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_partially_occluded_resized_character_select_still_classifies_character_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "title_resized_current_unknown.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_current_ironclad_select_capture_is_not_misclassified_as_continue(self) -> None:
        screenshot = Image.open(self.root / "captures" / "act1_restart_after_standard_notfound.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_live_map_capture_classifies_map(self) -> None:
        screenshot = Image.open(self.root / "captures" / "neow_dialog_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MAP)

    def test_detect_screen_prefers_map_over_confirm_popup_when_map_legend_is_visible(self) -> None:
        screenshot = Image.new("RGB", (1086, 630), "black")
        self.adapter.profile.anchors = []

        with patch.object(self.adapter, "_looks_like_map_legend", return_value=True):
            with patch.object(self.adapter, "_looks_like_card_grid", return_value=True):
                with patch.object(self.adapter, "_looks_like_confirm_popup", return_value=True):
                    with patch.object(self.adapter, "_looks_like_transform_confirm_popup", return_value=False):
                        screen = self.adapter._detect_screen(screenshot)

        self.assertEqual(screen, ScreenKind.MAP)

    def test_live_rest_capture_classifies_rest_and_surfaces_rest_actions(self) -> None:
        screenshot = Image.open(self.root / "observations" / "rest_unknown_debug.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.REST)
        self.assertEqual([action.label for action in state.available_actions], ["Rest", "Smith"])

    def test_resized_map_capture_classifies_map(self) -> None:
        screenshot = Image.open(self.root / "captures" / "resized_proceed_gamepad2_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MAP)

    def test_unknown_proceed_capture_classifies_continue(self) -> None:
        screenshot = Image.open(self.root / "captures" / "runlive_unknown_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CONTINUE)
        self.assertTrue(any(action.label == "Continue" for action in state.available_actions))

    def test_resized_battle_capture_overrides_card_grid_false_positive(self) -> None:
        screenshot = Image.open(self.root / "captures" / "resized_battle_misclassified.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.BATTLE)

    def test_partially_occluded_resized_battle_still_classifies_battle(self) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_more_turn4_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.BATTLE)

    def test_small_window_battle_metrics_use_live_fallback_regions(self) -> None:
        screenshot = Image.open(self.root / "captures" / "after_loop3_current.png")
        self.adapter._path = ["safe"]

        state = self.adapter.inspect_image(screenshot, read_metrics=True)

        self.assertEqual(state.screen, ScreenKind.BATTLE)
        self.assertEqual((state.hp, state.max_hp), (64, 80))
        self.assertEqual(state.gold, 99)
        self.assertEqual(state.energy, 3)
        self.assertGreaterEqual(state.floor, 1)

    def test_medium_window_battle_metrics_use_live_fallback_regions(self) -> None:
        screenshot = Image.open(self.root / "captures" / "battle_continue_current.png")
        self.adapter._path = ["safe", "safe"]

        state = self.adapter.inspect_image(screenshot, read_metrics=True)

        self.assertEqual(state.screen, ScreenKind.BATTLE)
        self.assertEqual((state.hp, state.max_hp), (17, 80))
        self.assertEqual(state.gold, 96)
        self.assertGreaterEqual(state.floor, 2)

    def test_memory_metrics_override_valid_ocr_metrics(self) -> None:
        snapshot = MemoryReadSnapshot(
            pid=123,
            module="sts2.dll",
            values={"hp": 61, "max_hp": 80, "gold": 111, "energy": 2, "floor": 7},
            fields={
                "hp": MemoryFieldResult(name="hp", value=61, source="memory"),
                "max_hp": MemoryFieldResult(name="max_hp", value=80, source="memory"),
                "gold": MemoryFieldResult(name="gold", value=111, source="memory"),
                "energy": MemoryFieldResult(name="energy", value=2, source="memory"),
                "floor": MemoryFieldResult(name="floor", value=7, source="memory"),
            },
        )
        metrics = {"hp": (55, 80), "gold": 99, "energy": 1, "floor": 6}
        sources = {"hp": "ocr", "max_hp": "ocr", "gold": "ocr", "energy": "ocr", "floor": "ocr"}

        with patch.object(self.adapter, "_probe_memory_snapshot_for_screen", return_value=snapshot):
            merged, merged_sources = self.adapter._merge_memory_metrics(ScreenKind.BATTLE, metrics, sources)

        self.assertEqual(merged["hp"], (61, 80))
        self.assertEqual(merged["gold"], 111)
        self.assertEqual(merged["energy"], 2)
        self.assertEqual(merged["floor"], 7)
        self.assertEqual(merged_sources["hp"], "memory")
        self.assertEqual(merged_sources["gold"], "memory")

    def test_invalid_memory_metrics_fall_back_to_ocr_values(self) -> None:
        snapshot = MemoryReadSnapshot(
            pid=123,
            module="sts2.dll",
            values={"gold": 10001, "energy": 7},
            fields={
                "gold": MemoryFieldResult(name="gold", value=10001, source="memory"),
                "energy": MemoryFieldResult(name="energy", value=7, source="memory"),
            },
        )
        metrics = {"gold": 99, "energy": 1}
        sources = {"gold": "ocr", "energy": "ocr"}

        with patch.object(self.adapter, "_probe_memory_snapshot_for_screen", return_value=snapshot):
            merged, merged_sources = self.adapter._merge_memory_metrics(ScreenKind.BATTLE, metrics, sources)

        self.assertEqual(merged["gold"], 99)
        self.assertEqual(merged["energy"], 1)
        self.assertEqual(merged_sources["gold"], "ocr")
        self.assertEqual(merged_sources["energy"], "ocr")

    def test_memory_floor_source_drives_act_derivation(self) -> None:
        snapshot = MemoryReadSnapshot(
            pid=123,
            module="sts2.dll",
            values={"floor": 20},
            fields={"floor": MemoryFieldResult(name="floor", value=20, source="memory")},
        )

        with patch.object(self.adapter, "_probe_memory_snapshot_for_screen", return_value=snapshot):
            merged, merged_sources = self.adapter._merge_memory_metrics(ScreenKind.MAP, {}, {})

        self.assertEqual(merged["floor"], 20)
        self.assertEqual(merged["act"], 2)
        self.assertEqual(merged_sources["floor"], "memory")
        self.assertEqual(merged_sources["act"], "memory")

    @patch("sts_bot.adapters.windows_stub.time.time", return_value=10.0)
    def test_probe_memory_uses_managed_fallback_when_raw_fields_are_not_configured(self, _mock_time) -> None:
        self.adapter._runtime = SimpleNamespace(target=SimpleNamespace(pid=4321))
        self.adapter.profile.memory_read.enabled = True
        self.adapter.profile.memory_read.fields = []
        managed_snapshot = ManagedProbeSnapshot(
            pid=4321,
            runtime_version="test",
            floor=7,
            ascension=4,
            gold=111,
            hp=61,
            max_hp=80,
            block=9,
            energy=2,
            max_energy=3,
            player_powers=[ManagedPowerSnapshot("0x1", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 2)],
            enemies=[
                ManagedEnemySnapshot(
                    address="0xenemy",
                    current_hp=33,
                    max_hp=40,
                    block=12,
                    powers=[ManagedPowerSnapshot("0x2", "MegaCrit.Sts2.Core.Models.Powers.WeakPower", 1)],
                )
            ],
        )

        with patch.object(self.adapter, "_managed_probe_for_runtime", return_value=SimpleNamespace(probe_pid=lambda pid: managed_snapshot)):
            snapshot = self.adapter._probe_memory_snapshot_for_screen(ScreenKind.BATTLE)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.module, "managed_probe")
        self.assertEqual(snapshot.values["hp"], 61)
        self.assertEqual(snapshot.values["gold"], 111)
        self.assertEqual(snapshot.values["max_energy"], 3)
        self.assertEqual(snapshot.values["block"], 9)
        self.assertEqual(self.adapter.last_memory_probe()["provider"], "managed")
        self.assertEqual(self.adapter.last_memory_probe()["values"]["energy"], 2)
        self.assertEqual(len(self.adapter.last_memory_probe()["player_powers"]), 1)
        self.assertEqual(len(self.adapter.last_memory_probe()["enemies"]), 1)

    def test_metrics_for_screen_prefers_memory_when_snapshot_covers_required_fields(self) -> None:
        screenshot = Image.new("RGB", (1086, 630), "black")
        snapshot = MemoryReadSnapshot(
            pid=123,
            module="managed_probe",
            values={
                "hp": 61,
                "max_hp": 80,
                "gold": 111,
                "energy": 2,
                "max_energy": 3,
                "block": 9,
                "floor": 7,
                "ascension": 4,
            },
            fields={},
        )

        with patch.object(self.adapter, "_probe_memory_snapshot_for_screen", return_value=snapshot):
            with patch.object(self.adapter, "_read_metrics", side_effect=AssertionError("ocr should not run")):
                metrics = self.adapter._metrics_for_screen(screenshot, ScreenKind.BATTLE, read_metrics=True)

        self.assertEqual(metrics["hp"], (61, 80))
        self.assertEqual(metrics["energy"], 2)
        self.assertEqual(metrics["max_energy"], 3)
        self.assertEqual(metrics["block"], 9)
        self.assertEqual(self.adapter.last_metric_sources()["hp"], "memory")

    def test_inspect_image_exposes_managed_energy_block_and_power_details(self) -> None:
        screenshot = Image.new("RGB", (1086, 630), "black")
        self.adapter._last_metric_sources = {"hp": "memory", "energy": "memory", "gold": "memory", "floor": "memory"}
        self.adapter._last_managed_snapshot = ManagedProbeSnapshot(
            pid=4321,
            runtime_version="test",
            floor=7,
            ascension=4,
            gold=111,
            hp=61,
            max_hp=80,
            block=9,
            energy=4,
            max_energy=6,
            player_powers=[ManagedPowerSnapshot("0x1", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 2)],
            enemies=[
                ManagedEnemySnapshot(
                    address="0xenemy",
                    current_hp=33,
                    max_hp=40,
                    block=12,
                    powers=[ManagedPowerSnapshot("0x2", "MegaCrit.Sts2.Core.Models.Powers.WeakPower", 1)],
                )
            ],
        )

        with patch.object(self.adapter, "_detect_screen", return_value=ScreenKind.BATTLE):
            with patch.object(self.adapter, "_metrics_for_screen", return_value={"hp": (61, 80), "gold": 111, "energy": 4, "floor": 7, "act": 1}):
                with patch.object(self.adapter, "_actions_for_screen", return_value=[]):
                    with patch.object(
                        self.adapter,
                        "_extract_battle_enemies",
                        return_value=[EnemyState(x=800, y=300, width=80, height=20, hp=30, max_hp=40, intent_damage=7)],
                    ):
                        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.max_energy, 6)
        self.assertEqual(state.block, 9)
        self.assertEqual(state.player_powers, {"Strength": 2})
        self.assertEqual(state.enemies[0].block, 12)
        self.assertEqual(state.enemies[0].powers, {"Weak": 1})

    def test_map_reachable_node_points_detect_bottom_row_candidates(self) -> None:
        screenshot = Image.open(self.root / "captures" / "map_before_step.png")

        points = self.adapter._map_reachable_node_points(screenshot)

        self.assertGreaterEqual(len(points), 3)
        self.assertEqual(points, sorted(points, key=lambda point: point[0]))
        self.assertGreater(points[0][1], 430)
        self.assertLess(points[-1][1], 560)

    def test_map_choice_point_prefers_candidate_near_reference_target(self) -> None:
        screenshot = Image.open(self.root / "captures" / "map_before_step.png")

        left_choice = self.adapter._map_choice_point(screenshot, (404, 495))
        middle_choice = self.adapter._map_choice_point(screenshot, (830, 489))
        right_choice = self.adapter._map_choice_point(screenshot, (1048, 495))

        self.assertIsNotNone(left_choice)
        self.assertIsNotNone(middle_choice)
        self.assertIsNotNone(right_choice)
        assert left_choice is not None and middle_choice is not None and right_choice is not None
        self.assertLess(left_choice[0], middle_choice[0])
        self.assertLess(middle_choice[0], right_choice[0])

    def test_target_candidate_points_prioritize_center_right_battle_targets(self) -> None:
        screenshot = Image.open(self.root / "captures" / "battle_live_before_turn.png")

        points = self.adapter._target_candidate_points(screenshot)

        self.assertGreaterEqual(len(points), 1)
        self.assertGreater(points[0][0], 1000)
        self.assertGreater(points[0][1], 420)
        self.assertLess(points[0][1], 520)

    def test_target_candidate_points_prioritize_lethal_enemy_from_state(self) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            gold=432,
            character="Ironclad",
            enemies=[
                EnemyState(x=760, y=350, width=100, height=10, hp=18, max_hp=18, intent_damage=5),
                EnemyState(x=1080, y=350, width=100, height=10, hp=6, max_hp=6, intent_damage=7),
            ],
        )

        with patch.object(self.adapter, "_battle_enemy_body_target_points", return_value=[(810, 450), (1130, 450)]):
            with patch.object(self.adapter, "_infer_enemy_target_points", return_value=[]):
                with patch.object(self.adapter, "_selected_card_name", return_value="Strike"):
                    points = self.adapter._target_candidate_points(screenshot, selected_reference=screenshot)

        self.assertGreaterEqual(len(points), 2)
        self.assertEqual(points[0], (1130, 450))

    def test_target_candidate_points_consider_enemy_block_in_lethal_priority(self) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            gold=432,
            character="Ironclad",
            enemies=[
                EnemyState(x=760, y=350, width=100, height=10, hp=8, max_hp=18, block=4, intent_damage=5),
                EnemyState(x=1080, y=350, width=100, height=10, hp=6, max_hp=6, block=0, intent_damage=7),
            ],
        )

        with patch.object(self.adapter, "_battle_enemy_body_target_points", return_value=[(810, 450), (1130, 450)]):
            with patch.object(self.adapter, "_infer_enemy_target_points", return_value=[]):
                with patch.object(self.adapter, "_selected_card_name", return_value="Strike"):
                    points = self.adapter._target_candidate_points(screenshot, selected_reference=screenshot)

        self.assertGreaterEqual(len(points), 2)
        self.assertEqual(points[0], (1130, 450))

    def test_target_navigation_sequences_prioritize_lethal_enemy_direction(self) -> None:
        screenshot = Image.new("RGB", (1047, 588), "black")
        self.adapter._last_state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=4,
            hp=43,
            max_hp=80,
            energy=2,
            gold=432,
            character="Ironclad",
            enemies=[
                EnemyState(x=680, y=350, width=100, height=10, hp=18, max_hp=18, intent_damage=5),
                EnemyState(x=900, y=350, width=100, height=10, hp=16, max_hp=16, intent_damage=6),
                EnemyState(x=1120, y=350, width=100, height=10, hp=6, max_hp=6, intent_damage=7),
            ],
        )

        with patch.object(self.adapter, "_selected_card_name", return_value="Strike"):
            sequences = self.adapter._target_navigation_sequences(screenshot, use_gamepad=True)

        self.assertGreaterEqual(len(sequences), 1)
        self.assertEqual(sequences[0], ["dpad_up", "dpad_right", "a"])

    def test_live_character_select_capture_classifies_character_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_title_unknown_after_tests.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CHARACTER_SELECT)

    def test_live_mode_select_capture_classifies_mode_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "after_title_focus_step.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MODE_SELECT)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_current_state_retries_when_first_observation_is_unknown(self, _mock_sleep) -> None:
        adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None)
        adapter._started = True
        screenshot = Image.new("RGB", (910, 530), "black")
        unknown = GameState(screen=ScreenKind.UNKNOWN, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        battle = GameState(screen=ScreenKind.BATTLE, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")

        with patch.object(adapter, "_capture_window_image_with_retry", return_value=screenshot):
            with patch.object(adapter, "inspect_image", side_effect=[unknown, battle]) as inspect_image:
                state = adapter.current_state()

        self.assertEqual(state.screen, ScreenKind.BATTLE)
        self.assertEqual(inspect_image.call_count, 2)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_probe_fast_retries_when_first_observation_is_unknown(self, _mock_sleep) -> None:
        adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None)
        adapter._started = True
        screenshot = Image.new("RGB", (910, 530), "black")
        unknown = GameState(screen=ScreenKind.UNKNOWN, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        continue_state = GameState(screen=ScreenKind.CONTINUE, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")

        with patch.object(adapter, "_capture_window_image_with_retry", return_value=screenshot):
            with patch.object(adapter, "inspect_image", side_effect=[unknown, continue_state]) as inspect_image:
                state = adapter.probe_fast()

        self.assertEqual(state.screen, ScreenKind.CONTINUE)
        self.assertEqual(inspect_image.call_count, 2)

    def test_probe_fast_does_not_trigger_memory_reads(self) -> None:
        adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        adapter._runtime = SimpleNamespace(capture_backend=SimpleNamespace(), input_backend=None, target=SimpleNamespace(pid=123))
        adapter._started = True
        screenshot = Image.new("RGB", (910, 530), "black")

        with patch.object(adapter, "_capture_window_image_with_retry", return_value=screenshot):
            with patch.object(adapter, "_probe_memory_snapshot_for_screen", side_effect=AssertionError("memory read called")):
                adapter.probe_fast()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_wait_for_change_ignores_unknown_transition_frames(self, _mock_sleep) -> None:
        adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        previous_state = GameState(screen=ScreenKind.BATTLE, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        unknown = GameState(screen=ScreenKind.UNKNOWN, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        stable = GameState(screen=ScreenKind.BATTLE, act=0, floor=0, hp=0, max_hp=0, energy=0, gold=0, character="Ironclad")
        previous_screenshot = Image.new("RGB", (910, 530), "black")
        current_screenshot = Image.new("RGB", (910, 530), "white")

        with patch("sts_bot.adapters.windows_stub.time.time", side_effect=[0.0, 0.1, 0.2, 0.3]):
            with patch.object(adapter, "_capture_window_image", side_effect=[current_screenshot, current_screenshot]):
                with patch.object(adapter, "inspect_image", side_effect=[unknown, stable]) as inspect_image:
                    with patch.object(adapter, "_frame_diff_score", return_value=10.0):
                        adapter._wait_for_change(previous_state, previous_screenshot)

        self.assertEqual(inspect_image.call_count, 2)

    def test_resized_mode_select_capture_classifies_mode_select(self) -> None:
        screenshot = Image.open(self.root / "captures" / "title_resized_single_play_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.MODE_SELECT)

    def test_live_neow_choice_capture_classifies_neow_choice(self) -> None:
        screenshot = Image.open(self.root / "captures" / "after_character_step.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.NEOW_CHOICE)
        self.assertIn("Neow option 1", [action.label for action in state.available_actions])
        self.assertIn("Neow option 2", [action.label for action in state.available_actions])
        self.assertIn("Neow option 3", [action.label for action in state.available_actions])

    def test_resized_neow_choice_capture_classifies_neow_choice(self) -> None:
        screenshot = Image.open(self.root / "captures" / "title_resized_after_ironclad_current.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.NEOW_CHOICE)

    def test_resized_card_grid_capture_classifies_card_grid(self) -> None:
        screenshot = Image.open(self.root / "captures" / "resized_neow_choice_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CARD_GRID)

    def test_neow_confirm_capture_classifies_confirm_popup(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_seq3_1_confirm_popup.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CONFIRM_POPUP)
        self.assertIn("Confirm modal", [action.label for action in state.available_actions])

    def test_neow_remove_confirm_capture_overrides_neow_dialog(self) -> None:
        screenshot = Image.open(self.root / "captures" / "act1_after_remove_confirm2.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CONFIRM_POPUP)

    def test_transform_result_confirm_capture_classifies_confirm_popup(self) -> None:
        screenshot = Image.open(self.root / "captures" / "after_neow_transform_smoke.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.CONFIRM_POPUP)

    def test_confirm_modal_point_targets_neow_proceed_bar(self) -> None:
        screenshot = Image.open(self.root / "captures" / "current_confirm_state.png")

        point = self.adapter._confirm_modal_point(screenshot)

        self.assertEqual(point, (543, 570))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_generic_neow_option_clicks_choice(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = GameAction(
            kind=ActionKind.NAVIGATE,
            label="Neow option 2",
            payload={"target": "generic_neow_option", "option_index": 1, "option_text": "gold"},
            tags=["start", "neow", "progress", "gold"],
        )
        screenshot = Image.open(self.root / "captures" / "after_character_step.png")
        self.adapter._last_state = GameState(screen=ScreenKind.NEOW_CHOICE, act=0, floor=0, hp=64, max_hp=80, energy=0, gold=99, character="Ironclad")
        definition = self.adapter._find_action_definition(action)

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "inspect_image", return_value=GameState(screen=ScreenKind.CONFIRM_POPUP, act=0, floor=0, hp=64, max_hp=80, energy=0, gold=99, character="Ironclad")):
                    backend = self.adapter._execute_action(definition, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertGreaterEqual(len(fake_backend.clicked), 1)

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_generic_neow_option_prefers_gamepad_when_requested(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = GameAction(
            kind=ActionKind.NAVIGATE,
            label="Neow option 2",
            payload={"target": "generic_neow_option", "option_index": 1, "option_text": "gold"},
            tags=["start", "neow", "progress", "gold"],
        )
        screenshot = Image.open(self.root / "captures" / "after_character_step.png")
        self.adapter._last_state = GameState(screen=ScreenKind.NEOW_CHOICE, act=0, floor=0, hp=64, max_hp=80, energy=0, gold=99, character="Ironclad")
        definition = self.adapter._find_action_definition(action)

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "_try_gamepad_progress_sequences", return_value=(True, screenshot.copy())) as try_gamepad:
                    backend = self.adapter._execute_action(definition, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        self.assertEqual(fake_backend.clicked, [])
        try_gamepad.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_reward_relic_prefers_gamepad_when_requested(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.REWARD_RELIC,
            kind=ActionKind.TAKE_RELIC,
            label="Take Anchor",
            point=(760, 590),
            payload={"relic": "Anchor"},
        )
        screenshot = Image.open(self.root / "captures" / "reward_resume_current.png")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "_try_gamepad_progress_sequences", return_value=(True, screenshot.copy())) as try_gamepad:
                    backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        self.assertEqual(fake_backend.clicked, [])
        try_gamepad.assert_called_once()

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_rest_prefers_gamepad_when_requested(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.REST,
            kind=ActionKind.SMITH,
            label="Smith",
            point=(1180, 525),
            payload={},
        )
        screenshot = Image.open(self.root / "captures" / "neow_dialog_after.png")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "_try_gamepad_progress_sequences", return_value=(True, screenshot.copy())) as try_gamepad:
                    backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        self.assertEqual(fake_backend.clicked, [])
        try_gamepad.assert_called_once()
        sequences = try_gamepad.call_args.args[1]
        self.assertEqual(sequences[0], ["dpad_left", "dpad_right", "a"])
        self.assertTrue(all(sequence[-1] == "a" for sequence in sequences))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_boss_relic_prefers_gamepad_when_requested(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.BOSS_RELIC,
            kind=ActionKind.TAKE_RELIC,
            label="Take Coffee Dripper",
            point=(760, 590),
            payload={"boss_relic": "Coffee Dripper", "option_index": 1},
        )
        screenshot = Image.open(self.root / "captures" / "shop_loop_current.png")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image_with_retry", side_effect=[screenshot.copy(), screenshot.copy()]):
                with patch.object(self.adapter, "_try_gamepad_progress_sequences", return_value=(True, screenshot.copy())) as try_gamepad:
                    backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        self.assertEqual(fake_backend.clicked, [])
        try_gamepad.assert_called_once()

    def test_occluded_visible_region_capture_is_not_misclassified_as_battle(self) -> None:
        screenshot = Image.open(self.root / "captures" / "live_battle_before.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertNotEqual(state.screen, ScreenKind.BATTLE)


class WindowsGameOverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.adapter = WindowsStsAdapter(self.root / "profiles" / "windows.example.json")
        self.adapter._runtime = SimpleNamespace(input_backend=None)

    def _fake_backend(self) -> object:
        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []
                self.clicked: list[tuple[int, int]] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def click(self, x: int, y: int, *, button: str = "left", double: bool = False) -> None:
                del button, double
                self.clicked.append((x, y))

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        return FakeBackend()

    def test_probe_game_over_capture_classifies_game_over(self) -> None:
        screenshot = Image.open(self.root / "captures" / "game_over_wait3.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.GAME_OVER)

    def test_resized_game_over_capture_classifies_game_over(self) -> None:
        screenshot = Image.open(self.root / "captures" / "continue_reward_gold_after.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.GAME_OVER)

    def test_dark_resized_game_over_summary_classifies_game_over(self) -> None:
        screenshot = Image.open(self.root / "captures" / "act1_post_death_probe.png")

        state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.GAME_OVER)

    def test_game_over_offers_main_menu_action(self) -> None:
        screenshot = Image.open(self.root / "captures" / "game_over_wait3.png")

        actions = self.adapter.inspect_image(screenshot, read_metrics=False).available_actions

        self.assertTrue(any(action.label == "Main menu" for action in actions))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_game_over_main_menu_uses_key_sequence(self, _mock_sleep) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.pressed: list[str] = []

            def key_press(self, key: str, *, hold_ms: int = 40) -> None:
                del hold_ms
                self.pressed.append(key)

            def diagnostics(self):
                return SimpleNamespace(backend="window_messages")

            def close(self) -> None:
                return None

        fake_backend = FakeBackend()
        action = ActionDefinition(
            screen=ScreenKind.GAME_OVER,
            kind=ActionKind.NAVIGATE,
            label="Main menu",
            point=(0, 0),
            keys=["down", "enter"],
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertEqual(fake_backend.pressed, ["down", "enter"])

    @patch("sts_bot.adapters.windows_stub.extract_text", return_value="Shrug It Off")
    def test_selected_card_name_uses_ocr_and_normalizes_known_card(self, _mock_extract) -> None:
        screenshot = Image.open(self.root / "captures" / "battle_macro_before.png")
        state = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=3,
            hp=64,
            max_hp=80,
            energy=2,
            gold=99,
            character="Ironclad",
            deck=[DeckCard("Shrug It Off", tags=["block", "draw"])],
        )

        card_name = self.adapter._selected_card_name(screenshot, state)

        self.assertEqual(card_name, "Shrug It Off")

    def test_low_confidence_battle_scores_stay_unknown(self) -> None:
        screenshot = Image.new("RGB", (1440, 810), "black")

        def fake_match(*args, **kwargs):
            del args, kwargs
            return TemplateMatch(score=0.36, point=(0, 0), found=False)

        with patch("sts_bot.adapters.windows_stub.match_template", side_effect=fake_match):
            state = self.adapter.inspect_image(screenshot, read_metrics=False)

        self.assertEqual(state.screen, ScreenKind.UNKNOWN)

    def test_map_has_highlighted_node_detects_white_outline(self) -> None:
        screenshot = Image.new("RGB", (1200, 800), "black")
        for x in range(260, 320):
            for y in range(240, 300):
                if x in {260, 261, 318, 319} or y in {240, 241, 298, 299}:
                    screenshot.putpixel((x, y), (240, 240, 240))

        self.assertTrue(self.adapter._map_has_highlighted_node(screenshot))

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_map_highlighted_node_uses_enter(self, _mock_sleep) -> None:
        fake_backend = self._fake_backend()
        action = ActionDefinition(
            screen=ScreenKind.MAP,
            kind=ActionKind.CHOOSE_PATH,
            label="Take highlighted node",
            point=(404, 495),
            payload={"path": "highlighted"},
        )

        with patch.object(self.adapter, "_resolve_input_backend", return_value=fake_backend):
            with patch.object(self.adapter, "_capture_window_image", return_value=Image.new("RGB", (1200, 800), "black")):
                with patch.object(self.adapter, "_map_has_highlighted_node", return_value=True):
                    backend = self.adapter._execute_action(action, backend="window_messages")

        self.assertEqual(backend, "window_messages")
        self.assertEqual(fake_backend.pressed, ["enter"])

    @patch("sts_bot.adapters.windows_stub.time.sleep", return_value=None)
    def test_execute_action_map_uses_gamepad_sequence_for_single_reachable_node(self, _mock_sleep) -> None:
        class FakeGamepadBackend:
            def diagnostics(self):
                return SimpleNamespace(backend="gamepad")

            def close(self) -> None:
                return None

        action = ActionDefinition(
            screen=ScreenKind.MAP,
            kind=ActionKind.CHOOSE_PATH,
            label="Take highlighted node",
            point=(404, 495),
            payload={"path": "highlighted"},
        )
        screenshot = Image.open(self.root / "captures" / "act1_after_proceed_click.png")

        with patch.object(self.adapter, "_resolve_input_backend", return_value=FakeGamepadBackend()):
            with patch.object(self.adapter, "_capture_window_image", return_value=screenshot):
                with patch.object(self.adapter, "_press_gamepad_sequence") as press_gamepad:
                    backend = self.adapter._execute_action(action, backend="gamepad")

        self.assertEqual(backend, "gamepad")
        press_gamepad.assert_called_once_with(["dpad_up", "a", "a"], hold_ms=110, gap_ms=130)

    def test_map_choice_buttons_scroll_then_move_sideways_then_confirm(self) -> None:
        screenshot = Image.new("RGB", (1200, 800), "black")
        action = ActionDefinition(
            screen=ScreenKind.MAP,
            kind=ActionKind.CHOOSE_PATH,
            label="Elite path",
            point=(1048, 495),
            payload={"path": "elite"},
        )

        with patch.object(self.adapter, "_map_has_highlighted_node", return_value=False):
            with patch.object(self.adapter, "_map_reachable_node_points", return_value=[(300, 230), (520, 230), (740, 230)]):
                with patch.object(self.adapter, "_map_choice_point", return_value=(740, 230)):
                    with patch.object(self.adapter, "_map_default_gamepad_index", return_value=0):
                        buttons = self.adapter._map_choice_buttons(screenshot, action)

        self.assertEqual(buttons, ["dpad_up", "dpad_right", "dpad_right", "a", "a"])


if __name__ == "__main__":
    unittest.main()
