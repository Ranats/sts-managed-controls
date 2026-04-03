from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sts_bot.cli import (
    _build_decision_provider,
    _run_live_marathon,
    _point_focus_box,
    _render_live_loop_tick,
    _stable_live_state,
    main,
)
from sts_bot.models import ExecutionExpectation, ExecutionObservation, LiveLoopTick
from sts_bot.models import ScreenKind, StateSource
from sts_bot.decision_provider import _default_expectation
from sts_bot.models import ActionKind, GameAction, GameState
from sts_bot.managed_probe import ManagedEnemySnapshot, ManagedPowerSnapshot


class CliHelpersTest(unittest.TestCase):
    def test_point_focus_box_clamps_point_inside_image(self) -> None:
        box = _point_focus_box((1432, 690), (643, 362))

        self.assertEqual(box, (502, 281, 643, 362))

    def test_point_focus_box_keeps_non_empty_box(self) -> None:
        left, top, right, bottom = _point_focus_box((9999, 9999), (10, 10))

        self.assertLess(left, right)
        self.assertLess(top, bottom)

    @patch("sts_bot.cli.time.sleep", return_value=None)
    def test_stable_live_state_retries_until_actions_are_available(self, _mock_sleep) -> None:
        unknown = SimpleNamespace(screen=ScreenKind.UNKNOWN, available_actions=[])
        continue_state = SimpleNamespace(screen=ScreenKind.CONTINUE, available_actions=[SimpleNamespace(label="Continue")])
        adapter = SimpleNamespace(probe_fast=lambda: None)
        calls = iter([unknown, continue_state])
        adapter.probe_fast = lambda: next(calls)

        state = _stable_live_state(adapter, fast=True, attempts=3)

        self.assertEqual(state.screen, ScreenKind.CONTINUE)

    @patch("sts_bot.cli.time.sleep", return_value=None)
    def test_stable_live_state_keeps_last_non_unknown_state_when_actions_stay_empty(self, _mock_sleep) -> None:
        unknown = SimpleNamespace(screen=ScreenKind.UNKNOWN, available_actions=[])
        battle = SimpleNamespace(screen=ScreenKind.BATTLE, available_actions=[])
        calls = iter([unknown, battle, battle])
        adapter = SimpleNamespace(probe_fast=lambda: next(calls))

        state = _stable_live_state(adapter, fast=True, attempts=3)

        self.assertEqual(state.screen, ScreenKind.BATTLE)

    def test_build_decision_provider_heuristic_mode(self) -> None:
        provider = _build_decision_provider(
            mode="heuristic",
            profile_path=__import__("pathlib").Path("profiles") / "windows.example.json",
            model="gpt-5.4",
            timeout_seconds=20.0,
        )

        self.assertEqual(provider.__class__.__name__, "HeuristicDecisionProvider")

    def test_render_live_loop_tick_includes_expected_and_observed(self) -> None:
        tick = LiveLoopTick(
            step_index=4,
            screen="battle",
            floor=6,
            hp=27,
            max_hp=80,
            energy=1,
            gold=102,
            action_label="Uppercut",
            provider_name="codex",
            max_energy=3,
            block=5,
            state_source="hybrid",
            state_metric_sources={"hp": "memory", "gold": "ocr"},
            reasoning="screen=battle | floor=6 | hp=27/80 | energy=1 -> Uppercut because enemy is threatening lethal soon, so apply vulnerable now.",
            fallback_note="codex_fallback=TimeoutExpired:demo",
            expected_outcome=ExecutionExpectation(next_screen="battle", change_summary="enemy hp should drop"),
            observed_outcome=ExecutionObservation(
                screen="battle",
                hp=27,
                max_hp=80,
                energy=0,
                gold=102,
                floor=6,
                max_energy=3,
                block=9,
                actions=["End turn"],
                state_source="memory",
                metric_sources={"hp": "memory", "gold": "memory"},
                note="source=memory | codex_fallback=TimeoutExpired:demo",
            ),
            verification_status="matched",
        )

        rendered = _render_live_loop_tick(tick)

        self.assertIn("============ Tick 004 ============", rendered)
        self.assertIn("[State]", rendered)
        self.assertIn("[Action]  Uppercut  provider=codex  verify=matched", rendered)
        self.assertIn("energy=1/3 block=5", rendered)
        self.assertIn("source=hybrid", rendered)
        self.assertIn("[Source]  hp:memory", rendered)
        self.assertIn("[Reason]  enemy is threatening lethal soon, so apply vulnerable now.", rendered)
        self.assertIn("[Fallback] codex_fallback=TimeoutExpired:demo", rendered)
        self.assertIn("[Expect]  next=battle", rendered)
        self.assertIn("[Observe] screen=battle", rendered)
        self.assertIn("energy=0/3 block=9", rendered)
        self.assertIn("[ObserveSrc] hp:memory", rendered)
        self.assertIn("[ObserveNote] source=memory | codex_fallback=TimeoutExpired:demo", rendered)
        self.assertFalse(rendered.splitlines()[-1].startswith("="))

    def test_render_live_loop_tick_adds_warning_for_suspicious_energy_and_partial_verify(self) -> None:
        tick = LiveLoopTick(
            step_index=7,
            screen="battle",
            floor=1,
            hp=64,
            max_hp=80,
            energy=7,
            gold=99,
            action_label="Play basic turn",
            provider_name="heuristic",
            reasoning="screen=battle -> Play basic turn because prefer_playing_cards_before_end_turn",
            observed_outcome=ExecutionObservation(
                screen="battle",
                hp=64,
                max_hp=80,
                energy=7,
                gold=99,
                floor=1,
            ),
            verification_status="partial",
        )

        rendered = _render_live_loop_tick(tick)

        self.assertIn("[Warn]    suspicious battle energy reading", rendered)
        self.assertIn("[Warn]    expected and observed state did not fully align", rendered)

    def test_load_profile_for_live_keeps_scene_backends_when_input_backend_is_explicit(self) -> None:
        from sts_bot.cli import _load_profile_for_live

        profile_path = __import__("pathlib").Path("profiles") / "windows.example.json"
        args = SimpleNamespace(
            capture_backend=None,
            input_backend="window_messages",
            window_message_delivery=None,
            window_message_activation=None,
            dry_run=False,
            allow_foreground_fallback=False,
        )

        profile = _load_profile_for_live(profile_path, args)

        self.assertEqual(profile.input_backend_name, "window_messages")
        self.assertEqual(profile.scene_input_backends.get("battle"), "gamepad")
        self.assertEqual(profile.scene_input_backends.get("reward_cards"), "window_messages")

    def test_default_expectation_for_card_grid_pick_does_not_claim_continue_screen(self) -> None:
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
        action = GameAction(ActionKind.PICK_CARD, "Card slot 1", {"card": "slot_1"}, ["attack"])

        expected = _default_expectation(state, action)

        self.assertIsNone(expected.next_screen)
        self.assertIn("confirm should appear", expected.change_summary)

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.WindowsStsAdapter")
    def test_main_probe_memory_renders_memory_payload(self, mock_adapter_cls, _mock_load_profile) -> None:
        adapter = mock_adapter_cls.return_value
        adapter.probe_fast.return_value = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=6,
            hp=55,
            max_hp=80,
            energy=1,
            gold=99,
            character="Ironclad",
        )
        adapter.probe_memory.return_value = {
            "module": "sts2.dll",
            "cached": False,
            "values": {"gold": 111, "hp": 61, "max_hp": 80},
            "fields": {
                "gold": {"source": "memory", "value": 111, "error": None},
                "hp": {"source": "memory", "value": 61, "error": None},
            },
            "player_powers": [{"type": "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", "amount": 2, "address": "0x1"}],
            "enemies": [
                {
                    "address": "0xenemy",
                    "current_hp": 33,
                    "max_hp": 40,
                    "block": 12,
                    "powers": [{"type": "MegaCrit.Sts2.Core.Models.Powers.WeakPower", "amount": 1, "address": "0x2"}],
                }
            ],
            "errors": [],
        }
        with patch("sys.argv", ["sts-lab", "probe-memory", "--profile", "profiles\\windows.example.json"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("screen=battle", rendered)
        self.assertIn("memory_module=sts2.dll", rendered)
        self.assertIn("memory_value=gold:111", rendered)
        self.assertIn("memory_field=hp source=memory value=61 error=None", rendered)
        self.assertIn("player_power=MegaCrit.Sts2.Core.Models.Powers.StrengthPower amount=2 address=0x1", rendered)
        self.assertIn("enemy=0xenemy hp=33/40 block=12", rendered)
        self.assertIn("enemy_power=0xenemy type=MegaCrit.Sts2.Core.Models.Powers.WeakPower amount=1 address=0x2", rendered)
        adapter.probe_memory.assert_called_once_with(screen=ScreenKind.BATTLE)

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.WindowsStsAdapter")
    def test_main_probe_live_shows_metric_sources(self, mock_adapter_cls, _mock_load_profile) -> None:
        adapter = mock_adapter_cls.return_value
        adapter.probe.return_value = GameState(
            screen=ScreenKind.BATTLE,
            act=1,
            floor=6,
            hp=55,
            max_hp=80,
            energy=2,
            max_energy=3,
            block=5,
            gold=100,
            character="Ironclad",
            player_powers={"Strength": 2},
            enemies=[SimpleNamespace(hp=18, max_hp=24, block=7, intent_damage=9, powers={"Weak": 1})],
            state_source=StateSource.HYBRID,
        )
        adapter.last_metric_sources.return_value = {"energy": "memory", "gold": "ocr"}
        adapter.last_anchor_scores.return_value = {}
        with patch("sys.argv", ["sts-lab", "probe-live", "--profile", "profiles\\windows.example.json", "--show-metric-sources"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("screen=battle act=1 floor=6 hp=55/80 gold=100 energy=2/3 block=5 source=hybrid", rendered)
        self.assertIn("player_powers={'Strength': 2}", rendered)
        self.assertIn("enemy[0] hp=18/24 block=7 intent=9 powers={'Weak': 1}", rendered)
        self.assertIn("metric_source=energy:memory", rendered)
        self.assertIn("metric_source=gold:ocr", rendered)

    def test_run_live_marathon_counts_only_finished_runs(self) -> None:
        logger = SimpleNamespace()
        results = iter(
            [
                SimpleNamespace(run_id="r1", status="max_steps", finished=False, steps=15, screen="battle", floor=3),
                SimpleNamespace(run_id="r2", status="run_over", finished=True, steps=40, screen="game_over", floor=7),
                SimpleNamespace(run_id="r3", status="run_over", finished=True, steps=22, screen="game_over", floor=1),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            summary_jsonl = Path(temp_dir) / "summary.jsonl"
            args = SimpleNamespace(
                runs=2,
                no_tick_log=True,
                between_sessions_seconds=0.0,
                summary_jsonl=summary_jsonl,
                stream_jsonl=Path(temp_dir) / "live_loop.jsonl",
            )
            with patch("sts_bot.cli._run_live_loop_session", side_effect=lambda args, logger, emit_ticks: next(results)) as run_session:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    _run_live_marathon(args, logger)

            rendered = stdout.getvalue()
            self.assertEqual(run_session.call_count, 3)
            self.assertIn("completed_runs=2/2", rendered)
            self.assertIn("partial_sessions=1", rendered)
            rows = [line for line in summary_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 3)
            self.assertIn('"status": "max_steps"', rows[0])
            self.assertIn('"status": "run_over"', rows[-1])

    @patch("sts_bot.cli._run_live_marathon")
    @patch("sts_bot.cli.RunLogger")
    def test_main_run_live_marathon_invokes_helper(self, mock_logger_cls, mock_run_live_marathon) -> None:
        logger = mock_logger_cls.return_value
        with patch(
            "sys.argv",
            [
                "sts-lab",
                "run-live-marathon",
                "--profile",
                "profiles\\windows.example.json",
                "--runs",
                "2",
            ],
        ):
            main()

        logger.init_db.assert_called_once()
        mock_run_live_marathon.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_player_block")
    def test_main_set_managed_block_writes_and_verifies(
        self,
        mock_set_block,
        mock_probe_managed,
        mock_create_runtime,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_block.return_value = SimpleNamespace(
            field="player_block",
            address="0x857de2ac",
            previous=5,
            requested=9,
            to_dict=lambda: {
                "pid": 2348,
                "block": 9,
                "write": {
                    "field": "player_block",
                    "address": "0x857de2ac",
                    "previous": 5,
                    "requested": 9,
                },
            },
        )
        mock_probe_managed.return_value = SimpleNamespace(
            floor=6,
            ascension=4,
            hp=22,
            max_hp=80,
            block=9,
            gold=453,
            energy=0,
            max_energy=3,
            player_powers=[],
            enemies=[],
            to_dict=lambda: {"block": 9},
        )

        with patch("sys.argv", ["sts-lab", "set-managed-block", "--profile", "profiles\\windows.example.json", "--value", "9"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("write_field=player_block address=0x857de2ac previous=5 requested=9", rendered)
        self.assertIn("verified_block=9 hp=22/80 gold=453 energy=0/3", rendered)
        mock_set_block.assert_called_once()
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_player_energy")
    def test_main_set_managed_energy_writes_and_verifies(
        self,
        mock_set_energy,
        mock_probe_managed,
        mock_create_runtime,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_energy.return_value = SimpleNamespace(
            field="player_energy",
            previous_energy=1,
            requested_energy=100,
            previous_max_energy=3,
            requested_max_energy=100,
            wrote_max_energy=True,
            to_dict=lambda: {},
        )
        mock_probe_managed.return_value = SimpleNamespace(
            floor=11,
            ascension=4,
            hp=23,
            max_hp=80,
            block=0,
            gold=538,
            energy=100,
            max_energy=100,
            player_powers=[],
            enemies=[],
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "set-managed-energy",
                "--profile",
                "profiles\\windows.example.json",
                "--value",
                "100",
                "--max-value",
                "100",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("write_field=player_energy previous_energy=1 requested_energy=100", rendered)
        self.assertIn("verified_energy=100/100 hp=23/80 gold=538 block=0", rendered)
        mock_set_energy.assert_called_once()
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.time.sleep", return_value=None)
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_player_block")
    def test_main_maintain_managed_block_loops(
        self,
        mock_set_block,
        mock_probe_managed,
        mock_create_runtime,
        _mock_sleep,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_block.side_effect = [
            SimpleNamespace(previous=0),
            SimpleNamespace(previous=100),
        ]
        mock_probe_managed.side_effect = [
            SimpleNamespace(block=100, hp=22, max_hp=80, gold=453, energy=0, max_energy=3),
            SimpleNamespace(block=100, hp=22, max_hp=80, gold=453, energy=0, max_energy=3),
        ]

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "maintain-managed-block",
                "--profile",
                "profiles\\windows.example.json",
                "--value",
                "100",
                "--iterations",
                "2",
                "--interval",
                "0.01",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("tick=1 requested=100 previous=0 verified_block=100", rendered)
        self.assertIn("tick=2 requested=100 previous=100 verified_block=100", rendered)
        self.assertEqual(mock_set_block.call_count, 2)
        self.assertEqual(mock_probe_managed.call_count, 2)

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.time.sleep", return_value=None)
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_player_energy")
    def test_main_maintain_managed_energy_loops(
        self,
        mock_set_energy,
        mock_probe_managed,
        mock_create_runtime,
        _mock_sleep,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_energy.side_effect = [
            SimpleNamespace(previous_energy=1, previous_max_energy=3),
            SimpleNamespace(previous_energy=100, previous_max_energy=100),
        ]
        mock_probe_managed.side_effect = [
            SimpleNamespace(energy=100, max_energy=100, hp=23, max_hp=80, gold=538, block=0),
            SimpleNamespace(energy=100, max_energy=100, hp=23, max_hp=80, gold=538, block=0),
        ]

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "maintain-managed-energy",
                "--profile",
                "profiles\\windows.example.json",
                "--value",
                "100",
                "--max-value",
                "100",
                "--iterations",
                "2",
                "--interval",
                "0.01",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("tick=1 requested_energy=100 requested_max_energy=100 previous_energy=1 previous_max_energy=3 verified_energy=100/100", rendered)
        self.assertIn("tick=2 requested_energy=100 requested_max_energy=100 previous_energy=100 previous_max_energy=100 verified_energy=100/100", rendered)
        self.assertEqual(mock_set_energy.call_count, 2)
        self.assertEqual(mock_probe_managed.call_count, 2)

    @patch("sts_bot.cli.enable_full_console")
    def test_main_enable_dev_console_updates_settings(self, mock_enable_console) -> None:
        mock_enable_console.return_value = SimpleNamespace(
            searched_root="C:\\Users\\sopur\\AppData\\Roaming\\SlayTheSpire2",
            updated_paths=["C:\\Users\\sopur\\AppData\\Roaming\\SlayTheSpire2\\steam\\123\\settings.save"],
            unchanged_paths=["C:\\Users\\sopur\\AppData\\Roaming\\SlayTheSpire2\\default\\1\\settings.save"],
            to_dict=lambda: {"updated_paths": [], "unchanged_paths": []},
        )

        with patch("sys.argv", ["sts-lab", "enable-dev-console"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("updated=1 unchanged=1", rendered)
        self.assertIn("updated_settings=C:\\Users\\sopur\\AppData\\Roaming\\SlayTheSpire2\\steam\\123\\settings.save", rendered)
        mock_enable_console.assert_called_once()

    @patch("sts_bot.cli.run_dev_console_command")
    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    def test_main_run_console_command_executes(self, _mock_load_profile, mock_run_console_command) -> None:
        mock_run_console_command.return_value = SimpleNamespace(
            command="help power",
            pid=4321,
            hwnd=0x1234,
            backend="sendinput_scan",
            close_console=False,
            settings=SimpleNamespace(updated_paths=["C:\\settings.save"], unchanged_paths=[]),
            to_dict=lambda: {"command": "help power"},
        )

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "run-console-command",
                "--profile",
                "profiles\\windows.example.json",
                "--command-text",
                "help power",
                "--leave-open",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("console_command=help power pid=4321 hwnd=0x1234 backend=sendinput_scan close_console=False", rendered)
        self.assertIn("settings_updated=1 settings_unchanged=0", rendered)
        mock_run_console_command.assert_called_once()

    @patch("sts_bot.cli._launch_managed_control_ui")
    def test_main_managed_control_ui_launches_window(self, mock_launch_ui) -> None:
        with patch("sys.argv", ["sts-lab", "managed-control-ui", "--profile", "profiles\\windows.example.json"]):
            main()

        mock_launch_ui.assert_called_once_with((Path(__file__).resolve().parents[1] / "profiles" / "windows.example.json").resolve())

    @patch("sts_bot.cli.install_bridge_mod")
    def test_main_install_bridge_mod_renders_paths(self, mock_install_bridge_mod) -> None:
        mock_install_bridge_mod.return_value = SimpleNamespace(
            mod_dir="C:\\Program Files (x86)\\Steam\\steamapps\\common\\Slay the Spire 2\\mods\\CodexBridge",
            dll_path="C:\\Program Files (x86)\\Steam\\steamapps\\common\\Slay the Spire 2\\mods\\CodexBridge\\CodexBridge.dll",
            manifest_path="C:\\Program Files (x86)\\Steam\\steamapps\\common\\Slay the Spire 2\\mods\\CodexBridge\\CodexBridge.json",
            to_dict=lambda: {},
        )

        with patch("sys.argv", ["sts-lab", "install-bridge-mod"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("bridge_mod_dir=C:\\Program Files (x86)\\Steam\\steamapps\\common\\Slay the Spire 2\\mods\\CodexBridge", rendered)
        self.assertIn("bridge_note=restart the game once to load the bridge mod", rendered)
        mock_install_bridge_mod.assert_called_once()

    @patch("sts_bot.cli.send_bridge_apply_power")
    def test_main_bridge_apply_power_renders_request_and_response(self, mock_send_bridge_apply_power) -> None:
        mock_send_bridge_apply_power.return_value = SimpleNamespace(
            request={"action": "apply_power", "target": "player", "power_type": "StrengthPower", "amount": 100, "enemy_index": 0},
            response={"ok": True, "status": "queued"},
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            ["sts-lab", "bridge-apply-power", "--power-type", "StrengthPower", "--value", "100"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn('"power_type": "StrengthPower"', rendered)
        self.assertIn('"status": "queued"', rendered)
        mock_send_bridge_apply_power.assert_called_once()

    @patch("sts_bot.cli.send_bridge_add_card")
    def test_main_bridge_add_card_renders_request_and_response(self, mock_send_bridge_add_card) -> None:
        mock_send_bridge_add_card.return_value = SimpleNamespace(
            request={"action": "add_card_to_hand", "card_type": "Whirlwind", "count": 2},
            response={"ok": True, "status": "queued"},
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            ["sts-lab", "bridge-add-card", "--card-type", "Whirlwind", "--destination", "hand", "--count", "2"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn('"action": "add_card_to_hand"', rendered)
        self.assertIn('"card_type": "Whirlwind"', rendered)
        mock_send_bridge_add_card.assert_called_once()

    @patch("sts_bot.cli.send_bridge_replace_master_deck")
    def test_main_bridge_replace_master_deck_renders_request_and_response(self, mock_send_bridge_replace_master_deck) -> None:
        mock_send_bridge_replace_master_deck.return_value = SimpleNamespace(
            request={"action": "replace_master_deck", "card_type": "Whirlwind", "count": 10},
            response={"ok": True, "status": "queued"},
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            ["sts-lab", "bridge-replace-master-deck", "--card-type", "Whirlwind", "--count", "10"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn('"action": "replace_master_deck"', rendered)
        self.assertIn('"count": 10', rendered)
        mock_send_bridge_replace_master_deck.assert_called_once()

    @patch("sts_bot.cli.send_bridge_obtain_relic")
    def test_main_bridge_obtain_relic_renders_request_and_response(self, mock_send_bridge_obtain_relic) -> None:
        mock_send_bridge_obtain_relic.return_value = SimpleNamespace(
            request={"action": "obtain_relic", "relic_type": "Anchor", "count": 2},
            response={"ok": True, "status": "queued"},
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            ["sts-lab", "bridge-obtain-relic", "--relic-type", "Anchor", "--count", "2"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn('"action": "obtain_relic"', rendered)
        self.assertIn('"relic_type": "Anchor"', rendered)
        mock_send_bridge_obtain_relic.assert_called_once()

    @patch("sts_bot.cli.load_card_catalog")
    def test_main_list_game_catalog_renders_filtered_entries(self, mock_load_card_catalog) -> None:
        from sts_bot.game_catalog import CatalogEntry

        mock_load_card_catalog.return_value = (
            CatalogEntry(
                kind="card",
                type_name="MegaCrit.Sts2.Core.Models.Cards.Whirlwind",
                short_name="Whirlwind",
                display_name="Whirlwind",
                has_parameterless_constructor=True,
            ),
        )

        with patch(
            "sys.argv",
            ["sts-lab", "list-game-catalog", "--kind", "cards", "--query", "whirl"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("catalog_kind=cards count=1", rendered)
        self.assertIn("catalog_entry=Whirlwind|Whirlwind|MegaCrit.Sts2.Core.Models.Cards.Whirlwind|default_ctor=True", rendered)
        mock_load_card_catalog.assert_called_once()

    @patch("sts_bot.cli.send_bridge_apply_power")
    def test_main_bridge_apply_power_renders_friendly_error_when_pipe_is_unavailable(self, mock_send_bridge_apply_power) -> None:
        from sts_bot.managed_probe import ManagedProbeError

        mock_send_bridge_apply_power.side_effect = ManagedProbeError("bridge pipe unavailable")

        with patch(
            "sys.argv",
            ["sts-lab", "bridge-apply-power", "--power-type", "StrengthPower", "--value", "100"],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("bridge_error=bridge pipe unavailable", rendered)
        mock_send_bridge_apply_power.assert_called_once()

    @patch("sts_bot.cli.install_bridge_mod")
    def test_main_install_bridge_mod_renders_friendly_error_when_locked(self, mock_install_bridge_mod) -> None:
        from sts_bot.managed_probe import ManagedProbeError

        mock_install_bridge_mod.side_effect = ManagedProbeError("bridge dll is locked")

        with patch("sys.argv", ["sts-lab", "install-bridge-mod"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("bridge_error=bridge dll is locked", rendered)
        mock_install_bridge_mod.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_power_amount")
    def test_main_set_managed_power_writes_and_verifies(
        self,
        mock_set_power,
        mock_probe_managed,
        mock_create_runtime,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_power.return_value = SimpleNamespace(
            field="power_amount",
            target="enemy",
            power_type="MegaCrit.Sts2.Core.Models.Powers.VulnerablePower",
            power_address="0x8582b720",
            address="0x8582b748",
            previous=2,
            requested=4,
            to_dict=lambda: {
                "write": {
                    "field": "power_amount",
                    "target": "enemy",
                    "power_type": "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower",
                    "power_address": "0x8582b720",
                    "address": "0x8582b748",
                    "previous": 2,
                    "requested": 4,
                }
            },
        )
        mock_probe_managed.return_value = SimpleNamespace(
            floor=6,
            ascension=4,
            hp=22,
            max_hp=80,
            block=9,
            gold=453,
            energy=0,
            max_energy=3,
            player_powers=[],
            enemies=[
                ManagedEnemySnapshot(
                    "0x82be34a0",
                    30,
                    61,
                    0,
                    [ManagedPowerSnapshot("0x8582b720", "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower", 4)],
                )
            ],
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "set-managed-power",
                "--profile",
                "profiles\\windows.example.json",
                "--target",
                "enemy",
                "--power-type",
                "VulnerablePower",
                "--value",
                "4",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("write_field=power_amount target=enemy power_type=MegaCrit.Sts2.Core.Models.Powers.VulnerablePower", rendered)
        self.assertIn("previous=2 requested=4", rendered)
        self.assertIn("enemy_power=0x82be34a0 type=MegaCrit.Sts2.Core.Models.Powers.VulnerablePower amount=4 address=0x8582b720", rendered)
        mock_set_power.assert_called_once()
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.send_bridge_apply_power")
    @patch("sts_bot.cli.set_managed_power_amount")
    def test_main_set_managed_power_falls_back_to_bridge_when_missing(
        self,
        mock_set_power,
        mock_send_bridge_apply_power,
        mock_probe_managed,
        mock_create_runtime,
        _mock_load_profile,
    ) -> None:
        from sts_bot.managed_probe import ManagedProbeError

        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_power.side_effect = ManagedProbeError("power not found: player/StrengthPower; available=[]")
        mock_send_bridge_apply_power.return_value = SimpleNamespace(
            request={"action": "apply_power", "power_type": "StrengthPower", "amount": 100, "target": "player"},
            response={"ok": True, "status": "queued"},
        )
        mock_probe_managed.return_value = SimpleNamespace(
            floor=7,
            ascension=4,
            hp=28,
            max_hp=80,
            block=0,
            gold=460,
            energy=3,
            max_energy=3,
            player_powers=[ManagedPowerSnapshot("0x8583cde0", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 100)],
            enemies=[],
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "set-managed-power",
                "--profile",
                "profiles\\windows.example.json",
                "--target",
                "player",
                "--power-type",
                "StrengthPower",
                "--value",
                "100",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("bridge_fallback=power_not_found", rendered)
        self.assertIn('"power_type": "StrengthPower"', rendered)
        self.assertIn('"status": "queued"', rendered)
        mock_send_bridge_apply_power.assert_called_once_with(power_type="StrengthPower", amount=100, target="player")
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.alias_managed_powers")
    def test_main_alias_managed_powers_writes_and_verifies(
        self,
        mock_alias_powers,
        mock_probe_managed,
        mock_create_runtime,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_alias_powers.return_value = SimpleNamespace(
            field="power_list_alias",
            source="enemy",
            dest="player",
            previous="0x857de2d0",
            requested="0x82bda360",
            address="0x857de288",
            to_dict=lambda: {},
        )
        mock_probe_managed.return_value = SimpleNamespace(
            floor=7,
            ascension=4,
            hp=28,
            max_hp=80,
            block=0,
            gold=460,
            energy=3,
            max_energy=3,
            player_powers=[ManagedPowerSnapshot("0x8583cde0", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 10)],
            enemies=[],
            to_dict=lambda: {},
        )

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "alias-managed-powers",
                "--profile",
                "profiles\\windows.example.json",
                "--source",
                "enemy",
                "--dest",
                "player",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("write_field=power_list_alias source=enemy dest=player", rendered)
        self.assertIn("player_power=MegaCrit.Sts2.Core.Models.Powers.StrengthPower amount=10 address=0x8583cde0", rendered)
        mock_alias_powers.assert_called_once()
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.time.sleep", return_value=None)
    @patch("sts_bot.cli.time.time", side_effect=[100.0, 100.1, 100.2, 100.31])
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    @patch("sts_bot.cli.set_managed_player_block")
    def test_main_maintain_managed_block_loops_and_verifies(
        self,
        mock_set_block,
        mock_probe_managed,
        mock_create_runtime,
        _mock_time,
        _mock_sleep,
        _mock_load_profile,
    ) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_set_block.side_effect = [
            SimpleNamespace(previous=0),
            SimpleNamespace(previous=100),
        ]
        mock_probe_managed.side_effect = [
            SimpleNamespace(block=100, hp=22, max_hp=80, gold=453, energy=0, max_energy=3),
            SimpleNamespace(block=100, hp=22, max_hp=80, gold=453, energy=0, max_energy=3),
        ]

        with patch(
            "sys.argv",
            [
                "sts-lab",
                "maintain-managed-block",
                "--profile",
                "profiles\\windows.example.json",
                "--value",
                "100",
                "--seconds",
                "0.25",
                "--interval",
                "0.05",
            ],
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("tick=1 requested=100 previous=0 verified_block=100", rendered)
        self.assertIn("tick=2 requested=100 previous=100 verified_block=100", rendered)
        self.assertEqual(mock_set_block.call_count, 2)
        self.assertEqual(mock_probe_managed.call_count, 2)

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    def test_main_probe_managed_renders_summary(self, mock_probe_managed, mock_create_runtime, _mock_load_profile) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_probe_managed.return_value = SimpleNamespace(
            floor=6,
            ascension=4,
            hp=29,
            max_hp=80,
            block=5,
            gold=453,
            energy=2,
            max_energy=3,
            player_powers=[],
            enemies=[],
            to_dict=lambda: {
                "pid": 2348,
                "floor": 6,
                "ascension": 4,
                "hp": 29,
                "max_hp": 80,
                "block": 5,
                "gold": 453,
                "energy": 2,
                "max_energy": 3,
            },
        )

        with patch("sys.argv", ["sts-lab", "probe-managed", "--profile", "profiles\\windows.example.json"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("floor=6 ascension=4 hp=29/80 gold=453 energy=2/3", rendered)
        mock_probe_managed.assert_called_once()

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    def test_main_probe_managed_writes_json(self, mock_probe_managed, mock_create_runtime, _mock_load_profile) -> None:
        from pathlib import Path
        import tempfile

        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_probe_managed.return_value = SimpleNamespace(
            floor=6,
            ascension=4,
            hp=29,
            max_hp=80,
            block=5,
            gold=453,
            energy=2,
            max_energy=3,
            player_powers=[],
            enemies=[],
            to_dict=lambda: {
                "pid": 2348,
                "floor": 6,
                "ascension": 4,
                "hp": 29,
                "max_hp": 80,
                "block": 5,
                "gold": 453,
                "energy": 2,
                "max_energy": 3,
                "player_powers": [],
                "enemies": [],
            },
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            json_out = Path(tmp_dir) / "managed.json"
            with patch("sys.argv", ["sts-lab", "probe-managed", "--profile", "profiles\\windows.example.json", "--json-out", str(json_out)]):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    main()

            rendered = stdout.getvalue()
            self.assertIn("Saved managed probe to", rendered)
            self.assertTrue(json_out.exists())
            self.assertIn("\"gold\": 453", json_out.read_text(encoding="utf-8"))

    @patch("sts_bot.cli._load_profile_for_live", return_value=SimpleNamespace())
    @patch("sts_bot.cli.create_runtime")
    @patch("sts_bot.cli.probe_managed_numeric")
    def test_main_probe_managed_renders_enemy_and_power_lines(self, mock_probe_managed, mock_create_runtime, _mock_load_profile) -> None:
        mock_create_runtime.return_value = SimpleNamespace(target=SimpleNamespace(pid=2348), close=lambda: None)
        mock_probe_managed.return_value = SimpleNamespace(
            floor=6,
            ascension=4,
            hp=29,
            max_hp=80,
            block=5,
            gold=453,
            energy=2,
            max_energy=3,
            player_powers=[ManagedPowerSnapshot("0x82bf0000", "MegaCrit.Sts2.Core.Models.Powers.StrengthPower", 2)],
            enemies=[
                ManagedEnemySnapshot(
                    "0x82be34a0",
                    53,
                    61,
                    0,
                    [ManagedPowerSnapshot("0x82bf0100", "MegaCrit.Sts2.Core.Models.Powers.VulnerablePower", 1)],
                )
            ],
            to_dict=lambda: {},
        )

        with patch("sys.argv", ["sts-lab", "probe-managed", "--profile", "profiles\\windows.example.json"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main()

        rendered = stdout.getvalue()
        self.assertIn("player_block=5", rendered)
        self.assertIn("player_power=MegaCrit.Sts2.Core.Models.Powers.StrengthPower amount=2 address=0x82bf0000", rendered)
        self.assertIn("enemy=0x82be34a0 hp=53/61 block=0", rendered)
        self.assertIn("enemy_power=0x82be34a0 type=MegaCrit.Sts2.Core.Models.Powers.VulnerablePower amount=1 address=0x82bf0100", rendered)


if __name__ == "__main__":
    unittest.main()
