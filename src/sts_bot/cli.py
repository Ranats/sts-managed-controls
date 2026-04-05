from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from PIL import ImageChops, ImageStat

from sts_bot.adapters.mock import MockAdapter
from sts_bot.analysis import (
    analyze_builds,
    build_fix_request_markdown,
    export_report_json,
    export_fix_request_markdown,
    export_run_trace_json,
    load_run_trace,
    render_report,
    render_run_trace,
)
from sts_bot.calibration import annotate_profile, crop_to_file, parse_rect
from sts_bot.config import CalibrationProfile, write_example_profile
from sts_bot.decision_provider import CodexDecisionProvider, HeuristicDecisionProvider
from sts_bot.dev_console import enable_full_console, run_dev_console_command
from sts_bot.game_catalog import filter_catalog, load_card_catalog, load_power_catalog, load_relic_catalog
from sts_bot.engine import AutoplayEngine
from sts_bot.input import backend_candidates
from sts_bot.io_runtime import create_runtime
from sts_bot.kb_learning import CodexKBLearner
from sts_bot.knowledge import set_active_kb_overlay_path
from sts_bot.live_runner import LiveLoopRunner
from sts_bot.logging import DEFAULT_DB_PATH, RunLogger
from sts_bot.managed_controls_commerce import (
    load_managed_controls_commerce_config,
    open_activation_guide,
    open_purchase_page,
)
from sts_bot.managed_controls_fulfillment_service import ManagedControlsFulfillmentServiceConfig, run_fulfillment_service
from sts_bot.managed_controls_issuer_service import ManagedControlsIssuerConfig, run_issuer_service
from sts_bot.managed_controls_license import (
    activate_managed_controls,
    ensure_managed_controls_access,
    get_managed_controls_license_status,
    issue_managed_controls_license,
    ManagedControlsLicenseError,
)
from sts_bot.managed_probe import (
    alias_managed_powers,
    ManagedProbeError,
    probe_managed_numeric,
    set_managed_player_block,
    set_managed_player_energy,
    set_managed_player_gold,
    set_managed_power_amount,
)
from sts_bot.mod_bridge import (
    install_bridge_mod,
    send_bridge_add_card,
    send_bridge_apply_power,
    send_bridge_clear_auto_power_on_combat_start,
    send_bridge_jump_to_map_coord,
    send_bridge_replace_master_deck,
    send_bridge_obtain_relic,
    send_bridge_set_auto_power_on_combat_start,
    send_bridge_tune_card_var,
    send_bridge_tune_relic_var,
)
from sts_bot.models import ScreenKind
from sts_bot.observe import append_jsonl, state_to_record
from sts_bot.policy import HeuristicPolicy

try:
    from sts_bot.adapters.windows_stub import WindowsStsAdapter
    from sts_bot.input_backends import WindowMessageInputBackend
    from sts_bot.windowing import (
        CoordinateTransform,
        WindowLocator,
        WindowSelector,
        cursor_position,
        enumerate_child_windows,
        foreground_window_title,
        gui_thread_state,
    )
    from sts_bot.windows_api import focus_window
    _WINDOWS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised on non-Windows hosts
    WindowsStsAdapter = None  # type: ignore[assignment]
    WindowMessageInputBackend = None  # type: ignore[assignment]
    CoordinateTransform = None  # type: ignore[assignment]
    WindowLocator = None  # type: ignore[assignment]
    WindowSelector = None  # type: ignore[assignment]
    _WINDOWS_IMPORT_ERROR = exc

    def _raise_windows_only() -> None:
        message = "This command is only available on Windows."
        if _WINDOWS_IMPORT_ERROR is not None:
            message = f"{message} ({_WINDOWS_IMPORT_ERROR})"
        raise RuntimeError(message)

    def cursor_position():  # type: ignore[no-redef]
        _raise_windows_only()

    def enumerate_child_windows(*_args, **_kwargs):  # type: ignore[no-redef]
        _raise_windows_only()

    def foreground_window_title():  # type: ignore[no-redef]
        _raise_windows_only()

    def gui_thread_state(*_args, **_kwargs):  # type: ignore[no-redef]
        _raise_windows_only()

    def focus_window(*_args, **_kwargs):  # type: ignore[no-redef]
        _raise_windows_only()


def _stable_live_state(adapter: WindowsStsAdapter, *, fast: bool = True, attempts: int = 3, delay_seconds: float = 0.18):
    observe = adapter.probe_fast if fast else adapter.probe
    state = observe()
    best_state = state
    for _attempt in range(max(0, attempts - 1)):
        if state.screen != ScreenKind.UNKNOWN and state.available_actions:
            return state
        time.sleep(max(0.0, delay_seconds))
        state = observe()
        if state.screen != ScreenKind.UNKNOWN:
            best_state = state
        if state.available_actions:
            return state
    return best_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="STS autoplay research lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize sqlite database")
    init_db.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    simulate = subparsers.add_parser("simulate", help="Run mock autoplay episodes")
    simulate.add_argument("--episodes", type=int, default=10)
    simulate.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    write_profile = subparsers.add_parser("write-example-profile", help="Write a starter Windows calibration profile")
    write_profile.add_argument("--path", type=Path, default=Path("profiles") / "windows.example.json")
    write_profile.add_argument("--input-backend", type=str, default=None)
    write_profile.add_argument("--capture-backend", type=str, default=None)
    write_profile.add_argument("--allow-foreground-fallback", action="store_true")

    capability_report = subparsers.add_parser("capability-report", help="Show target window and backend capability diagnostics")
    capability_report.add_argument("--profile", type=Path, required=True)
    capability_report.add_argument("--capture-backend", type=str, default=None)
    capability_report.add_argument("--input-backend", type=str, default=None)
    capability_report.add_argument("--window-message-delivery", type=str, choices=["send", "post"], default=None)
    capability_report.add_argument("--window-message-activation", type=str, choices=["none", "key", "click", "all"], default=None)
    capability_report.add_argument("--allow-foreground-fallback", action="store_true")
    capability_report.add_argument("--dry-run", action="store_true")

    bg_capture = subparsers.add_parser("bg-capture-smoke", help="Capture several background frames and report timing/foreground stability")
    bg_capture.add_argument("--profile", type=Path, required=True)
    bg_capture.add_argument("--frames", type=int, default=120)
    bg_capture.add_argument("--timeout-ms", type=int, default=250)
    bg_capture.add_argument("--capture-backend", type=str, default=None)
    bg_capture.add_argument("--input-backend", type=str, default=None)
    bg_capture.add_argument("--window-message-delivery", type=str, choices=["send", "post"], default=None)
    bg_capture.add_argument("--window-message-activation", type=str, choices=["none", "key", "click", "all"], default=None)
    bg_capture.add_argument("--allow-foreground-fallback", action="store_true")
    bg_capture.add_argument("--dry-run", action="store_true")
    bg_capture.add_argument("--save-dir", type=Path, default=None)

    bg_input = subparsers.add_parser("bg-input-smoke", help="Validate background message input against a local test window")
    bg_input.add_argument("--timeout-ms", type=int, default=15000)

    inspect_window = subparsers.add_parser("inspect-window", help="Inspect the target window, GUI thread state, and child HWND tree")
    inspect_window.add_argument("--profile", type=Path, required=True)
    inspect_window.add_argument("--max-depth", type=int, default=3)
    inspect_window.add_argument("--capture-backend", type=str, default=None)
    inspect_window.add_argument("--input-backend", type=str, default=None)
    inspect_window.add_argument("--window-message-delivery", type=str, choices=["send", "post"], default=None)
    inspect_window.add_argument("--window-message-activation", type=str, choices=["none", "key", "click", "all"], default=None)
    inspect_window.add_argument("--allow-foreground-fallback", action="store_true")
    inspect_window.add_argument("--dry-run", action="store_true")

    game_dry_run = subparsers.add_parser("game-dry-run", help="Resolve the current live action without sending input")
    game_dry_run.add_argument("--profile", type=Path, required=True)
    game_dry_run.add_argument("--label", type=str, default=None)
    game_dry_run.add_argument("--capture-backend", type=str, default=None)
    game_dry_run.add_argument("--input-backend", type=str, default=None)
    game_dry_run.add_argument("--window-message-delivery", type=str, choices=["send", "post"], default=None)
    game_dry_run.add_argument("--window-message-activation", type=str, choices=["none", "key", "click", "all"], default=None)
    game_dry_run.add_argument("--allow-foreground-fallback", action="store_true")
    game_dry_run.add_argument("--dry-run", action="store_true")

    bg_game_input = subparsers.add_parser("bg-game-input-probe", help="Send one background input to the live game and report foreground/capture effects")
    bg_game_input.add_argument("--profile", type=Path, required=True)
    bg_game_input.add_argument("--key", type=str, default="enter")
    bg_game_input.add_argument("--point", type=str, default=None, help="Reference-space client point x,y")
    bg_game_input.add_argument("--label", type=str, default=None, help="Profile action label to resolve to a click point")
    bg_game_input.add_argument("--double-click", action="store_true")
    bg_game_input.add_argument("--sleep", type=float, default=0.5)
    bg_game_input.add_argument("--capture-backend", type=str, default=None)
    bg_game_input.add_argument("--input-backend", type=str, default=None)
    bg_game_input.add_argument("--window-message-delivery", type=str, choices=["send", "post"], default=None)
    bg_game_input.add_argument("--window-message-activation", type=str, choices=["none", "key", "click", "all"], default=None)
    bg_game_input.add_argument("--allow-foreground-fallback", action="store_true")
    bg_game_input.add_argument("--save-before", type=Path, default=None)
    bg_game_input.add_argument("--save-after", type=Path, default=None)

    bg_game_matrix = subparsers.add_parser("bg-game-input-matrix", help="Run a matrix of background input probes against the live game")
    bg_game_matrix.add_argument("--profile", type=Path, required=True)
    bg_game_matrix.add_argument("--key", type=str, default="enter")
    bg_game_matrix.add_argument("--point", type=str, default=None, help="Reference-space client point x,y")
    bg_game_matrix.add_argument("--label", type=str, default=None, help="Profile action label to resolve to a click point")
    bg_game_matrix.add_argument("--double-click", action="store_true")
    bg_game_matrix.add_argument("--sleep", type=float, default=0.5)
    bg_game_matrix.add_argument("--capture-backend", type=str, default=None)
    bg_game_matrix.add_argument("--input-backend", type=str, default=None)
    bg_game_matrix.add_argument("--deliveries", type=str, default="send,post")
    bg_game_matrix.add_argument("--activations", type=str, default="none,key,click,all")
    bg_game_matrix.add_argument("--allow-foreground-fallback", action="store_true")

    probe_live = subparsers.add_parser("probe-live", help="Capture and inspect current live game state")
    probe_live.add_argument("--profile", type=Path, required=True)
    probe_live.add_argument("--save-screenshot", type=Path, default=None)
    probe_live.add_argument("--show-anchor-scores", action="store_true")
    probe_live.add_argument("--show-metric-sources", action="store_true")
    probe_live.add_argument("--fast", action="store_true", help="Skip OCR and use cached/default metrics for a faster probe")
    probe_live.add_argument("--focus", action="store_true", help="Focus the game window before probing")

    probe_memory = subparsers.add_parser("probe-memory", help="Read configured memory-backed numeric state without OCR metrics")
    probe_memory.add_argument("--profile", type=Path, required=True)
    probe_memory.add_argument("--focus", action="store_true", help="Focus the game window before probing")
    probe_memory.add_argument("--json-out", type=Path, default=None)

    probe_managed = subparsers.add_parser("probe-managed", help="Read live numeric state through the managed CLR snapshot probe")
    probe_managed.add_argument("--profile", type=Path, required=True)
    probe_managed.add_argument("--focus", action="store_true", help="Focus the game window before probing")
    probe_managed.add_argument("--json-out", type=Path, default=None)

    set_managed_block = subparsers.add_parser("set-managed-block", help="Experimentally write the player's managed block value")
    set_managed_block.add_argument("--profile", type=Path, required=True)
    set_managed_block.add_argument("--value", type=int, required=True)
    set_managed_block.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    set_managed_block.add_argument("--json-out", type=Path, default=None)

    set_managed_gold = subparsers.add_parser("set-managed-gold", help="Experimentally write the player's managed gold value")
    set_managed_gold.add_argument("--profile", type=Path, required=True)
    set_managed_gold.add_argument("--value", type=int, required=True)
    set_managed_gold.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    set_managed_gold.add_argument("--json-out", type=Path, default=None)

    set_managed_energy = subparsers.add_parser("set-managed-energy", help="Experimentally write the player's current and optional max energy")
    set_managed_energy.add_argument("--profile", type=Path, required=True)
    set_managed_energy.add_argument("--value", type=int, required=True, help="Current energy value")
    set_managed_energy.add_argument("--max-value", type=int, default=None, help="Optional max energy value")
    set_managed_energy.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    set_managed_energy.add_argument("--json-out", type=Path, default=None)

    maintain_managed_energy = subparsers.add_parser("maintain-managed-energy", help="Periodically rewrite the player's current and optional max energy")
    maintain_managed_energy.add_argument("--profile", type=Path, required=True)
    maintain_managed_energy.add_argument("--value", type=int, required=True, help="Current energy value")
    maintain_managed_energy.add_argument("--max-value", type=int, default=None, help="Optional max energy value")
    maintain_managed_energy.add_argument("--interval", type=float, default=0.25, help="Seconds between writes")
    maintain_managed_energy.add_argument("--seconds", type=float, default=30.0, help="Maximum duration when --iterations is not set")
    maintain_managed_energy.add_argument("--iterations", type=int, default=None, help="Optional fixed number of write cycles")
    maintain_managed_energy.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    maintain_managed_energy.add_argument("--json-out", type=Path, default=None)

    maintain_managed_block = subparsers.add_parser("maintain-managed-block", help="Periodically rewrite the player's managed block value")
    maintain_managed_block.add_argument("--profile", type=Path, required=True)
    maintain_managed_block.add_argument("--value", type=int, required=True)
    maintain_managed_block.add_argument("--interval", type=float, default=0.25, help="Seconds between writes")
    maintain_managed_block.add_argument("--seconds", type=float, default=30.0, help="Maximum duration when --iterations is not set")
    maintain_managed_block.add_argument("--iterations", type=int, default=None, help="Optional fixed number of write cycles")
    maintain_managed_block.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    maintain_managed_block.add_argument("--json-out", type=Path, default=None)

    set_managed_power = subparsers.add_parser("set-managed-power", help="Experimentally write an existing managed power amount")
    set_managed_power.add_argument("--profile", type=Path, required=True)
    set_managed_power.add_argument("--target", type=str, choices=["player", "enemy"], required=True)
    set_managed_power.add_argument("--power-type", type=str, required=True, help="Concrete power type suffix such as VulnerablePower")
    set_managed_power.add_argument("--value", type=int, required=True)
    set_managed_power.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    set_managed_power.add_argument("--json-out", type=Path, default=None)

    alias_managed_power = subparsers.add_parser("alias-managed-powers", help="Experimentally point one target's power list at another target's existing power list")
    alias_managed_power.add_argument("--profile", type=Path, required=True)
    alias_managed_power.add_argument("--source", type=str, choices=["player", "enemy"], required=True)
    alias_managed_power.add_argument("--dest", type=str, choices=["player", "enemy"], required=True)
    alias_managed_power.add_argument("--focus", action="store_true", help="Focus the game window before writing")
    alias_managed_power.add_argument("--json-out", type=Path, default=None)

    enable_dev_console = subparsers.add_parser("enable-dev-console", help="Set full_console=true in the game's settings.save files")
    enable_dev_console.add_argument("--settings-root", type=Path, default=None, help="Optional settings root or explicit settings.save path")
    enable_dev_console.add_argument("--json-out", type=Path, default=None)

    run_console_command = subparsers.add_parser("run-console-command", help="Focus the game, open the dev console, and type a command")
    run_console_command.add_argument("--profile", type=Path, required=True)
    run_console_command.add_argument("--command-text", type=str, required=True)
    run_console_command.add_argument("--backend", type=str, default="sendinput_scan")
    run_console_command.add_argument("--open-key", type=str, default="backtick")
    run_console_command.add_argument("--typing-interval", type=float, default=0.01)
    run_console_command.add_argument("--leave-open", action="store_true", help="Leave the dev console open after submit")
    run_console_command.add_argument("--skip-enable-full-console", action="store_true", help="Do not modify settings.save before sending the command")
    run_console_command.add_argument("--settings-root", type=Path, default=None, help="Optional settings root or explicit settings.save path")
    run_console_command.add_argument("--json-out", type=Path, default=None)

    managed_control_ui = subparsers.add_parser("managed-control-ui", help="Open the local control panel for managed writes and dev console commands")
    managed_control_ui.add_argument("--profile", type=Path, required=True)

    managed_controls_license_status = subparsers.add_parser("managed-controls-license-status", help="Show local trial and activation status for STS Managed Controls")
    managed_controls_license_status.add_argument("--json-out", type=Path, default=None)

    activate_managed_controls_cmd = subparsers.add_parser("activate-managed-controls", help="Activate STS Managed Controls access with a signed activation key")
    activate_managed_controls_cmd.add_argument("--license-key", type=str, required=True)
    activate_managed_controls_cmd.add_argument("--json-out", type=Path, default=None)

    open_purchase_page_cmd = subparsers.add_parser("open-managed-controls-purchase", help="Open the configured purchase page for this install id")
    open_purchase_page_cmd.add_argument("--json-out", type=Path, default=None)

    open_activation_guide_cmd = subparsers.add_parser("open-managed-controls-activation-guide", help="Open the activation guide for STS Managed Controls")
    open_activation_guide_cmd.add_argument("--json-out", type=Path, default=None)

    issue_managed_controls_cmd = subparsers.add_parser("issue-managed-controls-license", help="Issue a signed activation key for a specific install id")
    issue_managed_controls_cmd.add_argument("--install-id", type=str, required=True)
    issue_managed_controls_cmd.add_argument("--licensee", type=str, required=True)
    issue_managed_controls_cmd.add_argument("--private-key-file", type=Path, required=True)
    issue_managed_controls_cmd.add_argument("--plan", type=str, default="standard")
    issue_managed_controls_cmd.add_argument("--days", type=int, default=365)
    issue_managed_controls_cmd.add_argument("--no-expiry", action="store_true")
    issue_managed_controls_cmd.add_argument("--json-out", type=Path, default=None)

    serve_managed_controls_issuer = subparsers.add_parser("serve-managed-controls-issuer", help="Run a small issuer service for automatic activation-key generation")
    serve_managed_controls_issuer.add_argument("--host", type=str, default="127.0.0.1")
    serve_managed_controls_issuer.add_argument("--port", type=int, default=8787)
    serve_managed_controls_issuer.add_argument("--private-key-file", type=Path, required=True)
    serve_managed_controls_issuer.add_argument("--admin-token", type=str, required=True)
    serve_managed_controls_issuer.add_argument("--default-plan", type=str, default="standard")
    serve_managed_controls_issuer.add_argument("--default-days", type=int, default=365)

    serve_managed_controls_fulfillment = subparsers.add_parser("serve-managed-controls-fulfillment", help="Run the hosted fulfillment service for checkout webhooks and activation email delivery")
    serve_managed_controls_fulfillment.add_argument("--host", type=str, default="127.0.0.1")
    serve_managed_controls_fulfillment.add_argument("--port", type=int, default=8787)
    serve_managed_controls_fulfillment.add_argument("--private-key-file", type=Path, required=True)
    serve_managed_controls_fulfillment.add_argument("--admin-token", type=str, required=True)
    serve_managed_controls_fulfillment.add_argument("--default-plan", type=str, default="standard")
    serve_managed_controls_fulfillment.add_argument("--default-days", type=int, default=365)
    serve_managed_controls_fulfillment.add_argument("--storage-dir", type=Path, default=None)

    install_bridge_mod_parser = subparsers.add_parser("install-bridge-mod", help="Build and install the in-process Codex bridge mod into the game mods folder")
    install_bridge_mod_parser.add_argument("--game-dir", type=Path, default=None)
    install_bridge_mod_parser.add_argument("--json-out", type=Path, default=None)

    bridge_apply_power = subparsers.add_parser("bridge-apply-power", help="Send a power application request to the installed Codex bridge mod")
    bridge_apply_power.add_argument("--power-type", type=str, required=True)
    bridge_apply_power.add_argument("--value", type=int, required=True)
    bridge_apply_power.add_argument("--target", type=str, choices=["player", "enemy"], default="player")
    bridge_apply_power.add_argument("--enemy-index", type=int, default=0)
    bridge_apply_power.add_argument("--json-out", type=Path, default=None)

    bridge_add_card = subparsers.add_parser("bridge-add-card", help="Send a card creation request to the installed Codex bridge mod")
    bridge_add_card.add_argument("--card-type", type=str, required=True)
    bridge_add_card.add_argument("--destination", type=str, choices=["deck", "hand"], required=True)
    bridge_add_card.add_argument("--count", type=int, default=1)
    bridge_add_card.add_argument("--upgrade-count", type=int, default=0)
    bridge_add_card.add_argument("--json-out", type=Path, default=None)

    bridge_replace_master_deck = subparsers.add_parser("bridge-replace-master-deck", help="Replace the player's master deck through the installed Codex bridge mod")
    bridge_replace_master_deck.add_argument("--card-type", type=str, required=True)
    bridge_replace_master_deck.add_argument("--count", type=int, default=None)
    bridge_replace_master_deck.add_argument("--upgrade-count", type=int, default=0)
    bridge_replace_master_deck.add_argument("--json-out", type=Path, default=None)

    bridge_obtain_relic = subparsers.add_parser("bridge-obtain-relic", help="Obtain a relic through the installed Codex bridge mod")
    bridge_obtain_relic.add_argument("--relic-type", type=str, required=True)
    bridge_obtain_relic.add_argument("--count", type=int, default=1)
    bridge_obtain_relic.add_argument("--json-out", type=Path, default=None)

    bridge_set_auto_power = subparsers.add_parser("bridge-set-auto-power", help="Configure a combat-start power rule in the installed Codex bridge mod")
    bridge_set_auto_power.add_argument("--power-type", type=str, required=True)
    bridge_set_auto_power.add_argument("--value", type=int, required=True)
    bridge_set_auto_power.add_argument("--target", type=str, choices=["player", "enemy"], default="player")
    bridge_set_auto_power.add_argument("--enemy-index", type=int, default=0)
    bridge_set_auto_power.add_argument("--json-out", type=Path, default=None)

    bridge_clear_auto_power = subparsers.add_parser("bridge-clear-auto-power", help="Clear combat-start power rules in the installed Codex bridge mod")
    bridge_clear_auto_power.add_argument("--power-type", type=str, default="")
    bridge_clear_auto_power.add_argument("--target", type=str, default="")
    bridge_clear_auto_power.add_argument("--enemy-index", type=int, default=0)
    bridge_clear_auto_power.add_argument("--json-out", type=Path, default=None)

    bridge_jump_map = subparsers.add_parser("bridge-jump-map", help="Jump directly to a map coord through the installed Codex bridge mod")
    bridge_jump_map.add_argument("--col", type=int, required=True)
    bridge_jump_map.add_argument("--row", type=int, required=True)
    bridge_jump_map.add_argument("--json-out", type=Path, default=None)

    bridge_tune_card = subparsers.add_parser("bridge-tune-card", help="Tune a card dynamic var through the installed Codex bridge mod")
    bridge_tune_card.add_argument("--card-type", type=str, required=True)
    bridge_tune_card.add_argument("--var-name", type=str, required=True)
    bridge_tune_card.add_argument("--value", type=int, required=True)
    bridge_tune_card.add_argument("--scope", type=str, choices=["deck", "master_deck", "hand", "draw", "discard", "exhaust", "play", "all_piles"], required=True)
    bridge_tune_card.add_argument("--mode", type=str, choices=["set", "add"], default="set")
    bridge_tune_card.add_argument("--json-out", type=Path, default=None)

    bridge_tune_relic = subparsers.add_parser("bridge-tune-relic", help="Tune an owned relic dynamic var through the installed Codex bridge mod")
    bridge_tune_relic.add_argument("--relic-type", type=str, required=True)
    bridge_tune_relic.add_argument("--var-name", type=str, required=True)
    bridge_tune_relic.add_argument("--value", type=int, required=True)
    bridge_tune_relic.add_argument("--mode", type=str, choices=["set", "add"], default="set")
    bridge_tune_relic.add_argument("--json-out", type=Path, default=None)

    list_game_catalog = subparsers.add_parser("list-game-catalog", help="List available cards, powers, or relics from sts2.dll")
    list_game_catalog.add_argument("--kind", type=str, choices=["cards", "powers", "relics"], required=True)
    list_game_catalog.add_argument("--query", type=str, default=None)
    list_game_catalog.add_argument("--json-out", type=Path, default=None)

    probe_image = subparsers.add_parser("probe-image", help="Inspect a saved screenshot with the live profile")
    probe_image.add_argument("--profile", type=Path, required=True)
    probe_image.add_argument("--input", type=Path, required=True)
    probe_image.add_argument("--show-anchor-scores", action="store_true")
    probe_image.add_argument("--fast", action="store_true", help="Skip OCR and use cached/default metrics for a faster probe")

    capture_live = subparsers.add_parser("capture-live", help="Save a screenshot of the live game window")
    capture_live.add_argument("--profile", type=Path, required=True)
    capture_live.add_argument("--output", type=Path, required=True)
    capture_live.add_argument("--focus", action="store_true", help="Focus the game window before capturing")

    step_live = subparsers.add_parser("step-live", help="Perform a single live action")
    step_live.add_argument("--profile", type=Path, required=True)
    step_live.add_argument("--label", type=str, default=None, help="Action label to apply. Defaults to heuristic selection.")
    step_live.add_argument("--save-before", type=Path, default=None)
    step_live.add_argument("--save-after", type=Path, default=None)
    step_live.add_argument("--backend", type=str, default=None)
    step_live.add_argument("--mode", type=str, choices=["auto", "key", "click"], default="auto")
    step_live.add_argument("--dry-run", action="store_true")
    step_live.add_argument("--allow-foreground-fallback", action="store_true")

    exercise_input = subparsers.add_parser("exercise-input-live", help="Try multiple input backends against the current live screen")
    exercise_input.add_argument("--profile", type=Path, required=True)
    exercise_input.add_argument("--label", type=str, default=None, help="Action label to apply. Defaults to heuristic selection.")
    exercise_input.add_argument("--backends", type=str, default="all", help="Comma-separated backends or 'all'")
    exercise_input.add_argument("--modes", type=str, default="auto,key,click", help="Comma-separated modes from auto,key,click")
    exercise_input.add_argument("--save-dir", type=Path, default=None)
    exercise_input.add_argument("--sleep", type=float, default=0.6)
    exercise_input.add_argument("--allow-foreground-fallback", action="store_true")

    inject_live = subparsers.add_parser("inject-live", help="Send a raw key press or click to the live window")
    inject_live.add_argument("--profile", type=Path, required=True)
    inject_live.add_argument("--backend", type=str, default="combined")
    inject_live.add_argument("--key", type=str, default=None)
    inject_live.add_argument("--point", type=str, default=None, help="Reference-space point as x,y")
    inject_live.add_argument("--sleep", type=float, default=0.6)
    inject_live.add_argument("--repeat", type=int, default=1)
    inject_live.add_argument("--hold-ms", type=int, default=40)
    inject_live.add_argument("--save-before", type=Path, default=None)
    inject_live.add_argument("--save-after", type=Path, default=None)
    inject_live.add_argument("--dry-run", action="store_true")
    inject_live.add_argument("--allow-foreground-fallback", action="store_true")

    play_card_live = subparsers.add_parser("play-card-live", help="Select a battle card slot and resolve it automatically")
    play_card_live.add_argument("--profile", type=Path, required=True)
    play_card_live.add_argument("--slot", type=int, required=True)
    play_card_live.add_argument("--backend", type=str, default=None)
    play_card_live.add_argument("--save-before", type=Path, default=None)
    play_card_live.add_argument("--save-after", type=Path, default=None)
    play_card_live.add_argument("--dry-run", action="store_true")
    play_card_live.add_argument("--allow-foreground-fallback", action="store_true")

    play_turn_live = subparsers.add_parser("play-turn-live", help="Play a simple battle turn by trying card slots then ending the turn")
    play_turn_live.add_argument("--profile", type=Path, required=True)
    play_turn_live.add_argument("--backend", type=str, default=None)
    play_turn_live.add_argument("--max-slots", type=int, default=5)
    play_turn_live.add_argument("--time-budget-seconds", type=float, default=2.8)
    play_turn_live.add_argument("--save-before", type=Path, default=None)
    play_turn_live.add_argument("--save-after", type=Path, default=None)
    play_turn_live.add_argument("--dry-run", action="store_true")
    play_turn_live.add_argument("--allow-foreground-fallback", action="store_true")

    inject_gamepad = subparsers.add_parser("inject-gamepad-live", help="Send virtual Xbox controller buttons to the live game")
    inject_gamepad.add_argument("--profile", type=Path, required=True)
    inject_gamepad.add_argument("--buttons", type=str, required=True, help="Comma-separated button names such as a,start,dpad_down")
    inject_gamepad.add_argument("--settle-ms", type=int, default=0)
    inject_gamepad.add_argument("--hold-ms", type=int, default=120)
    inject_gamepad.add_argument("--gap-ms", type=int, default=120)
    inject_gamepad.add_argument("--sleep", type=float, default=0.25)
    inject_gamepad.add_argument("--save-before", type=Path, default=None)
    inject_gamepad.add_argument("--save-after", type=Path, default=None)

    annotate_live = subparsers.add_parser("annotate-live", help="Annotate a live screenshot with profile regions")
    annotate_live.add_argument("--profile", type=Path, required=True)
    annotate_live.add_argument("--output", type=Path, required=True)
    annotate_live.add_argument("--input", type=Path, default=None, help="Optional existing screenshot to annotate")

    watch_live = subparsers.add_parser("watch-live", help="Passively observe the live game and log screen changes")
    watch_live.add_argument("--profile", type=Path, required=True)
    watch_live.add_argument("--seconds", type=int, default=120)
    watch_live.add_argument("--interval", type=float, default=1.0)
    watch_live.add_argument("--jsonl-out", type=Path, default=Path("observations") / "live.jsonl")
    watch_live.add_argument("--capture-dir", type=Path, default=None)
    watch_live.add_argument("--only-on-change", action="store_true")
    watch_live.add_argument("--focus", action="store_true", help="Focus the game window before each observation loop starts")

    summarize_obs = subparsers.add_parser("summarize-observations", help="Summarize watch-live jsonl output")
    summarize_obs.add_argument("--input", type=Path, required=True)

    crop_template = subparsers.add_parser("crop-template", help="Crop a template image from a screenshot")
    crop_template.add_argument("--input", type=Path, required=True)
    crop_template.add_argument("--rect", type=str, required=True, help="left,top,width,height")
    crop_template.add_argument("--output", type=Path, required=True)

    run_live = subparsers.add_parser("run-live", help="Run the live Windows adapter")
    run_live.add_argument("--profile", type=Path, required=True)
    run_live.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    run_live.add_argument("--episodes", type=int, default=1)
    run_live.add_argument("--max-steps", type=int, default=300)
    run_live.add_argument("--bootstrap", action="store_true", help="Execute start_actions before the first observation")
    run_live.add_argument("--capture-backend", type=str, default=None)
    run_live.add_argument("--input-backend", type=str, default=None)
    run_live.add_argument("--allow-foreground-fallback", action="store_true")
    run_live.add_argument("--dry-run", action="store_true")

    run_live_loop = subparsers.add_parser("run-live-loop", help="Run a persistent live observe-decide-act loop with streamed reasoning")
    run_live_loop.add_argument("--profile", type=Path, required=True)
    run_live_loop.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    run_live_loop.add_argument("--max-steps", type=int, default=300)
    run_live_loop.add_argument("--max-seconds", type=float, default=None)
    run_live_loop.add_argument("--decision-provider", type=str, choices=["heuristic", "codex", "auto"], default="auto")
    run_live_loop.add_argument("--codex-model", type=str, default="gpt-5.4")
    run_live_loop.add_argument("--codex-timeout-seconds", type=float, default=45.0)
    run_live_loop.add_argument("--stream-jsonl", type=Path, default=Path("observations") / "live_loop.jsonl")
    run_live_loop.add_argument("--kb-overlay", type=Path, default=Path("data") / "kb_overlay.json")
    run_live_loop.add_argument("--harvest-dir", type=Path, default=Path("data") / "harvest")
    run_live_loop.add_argument("--harvest-confidence-threshold", type=float, default=0.55)
    run_live_loop.add_argument("--auto-learn-kb", action="store_true")
    run_live_loop.add_argument("--kb-learn-model", type=str, default="gpt-5.4")
    run_live_loop.add_argument("--kb-learn-timeout-seconds", type=float, default=60.0)
    run_live_loop.add_argument("--kb-learn-min-cases", type=int, default=3)
    run_live_loop.add_argument("--kb-learn-max-cases", type=int, default=8)
    run_live_loop.add_argument("--kb-learn-cooldown-seconds", type=float, default=90.0)
    run_live_loop.add_argument("--capture-backend", type=str, default=None)
    run_live_loop.add_argument("--input-backend", type=str, default=None)
    run_live_loop.add_argument("--allow-foreground-fallback", action="store_true")
    run_live_loop.add_argument("--dry-run", action="store_true")

    run_live_marathon = subparsers.add_parser("run-live-marathon", help="Run repeated live sessions until a target number of full runs completes")
    run_live_marathon.add_argument("--profile", type=Path, required=True)
    run_live_marathon.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    run_live_marathon.add_argument("--runs", type=int, default=100, help="Number of completed runs to finish")
    run_live_marathon.add_argument("--max-steps", type=int, default=1200, help="Per-session step budget before restarting the loop")
    run_live_marathon.add_argument("--max-seconds", type=float, default=1800.0, help="Per-session wall-clock budget before restarting the loop")
    run_live_marathon.add_argument("--decision-provider", type=str, choices=["heuristic", "codex", "auto"], default="heuristic")
    run_live_marathon.add_argument("--codex-model", type=str, default="gpt-5.4")
    run_live_marathon.add_argument("--codex-timeout-seconds", type=float, default=45.0)
    run_live_marathon.add_argument("--stream-jsonl", type=Path, default=Path("observations") / "live_loop.jsonl")
    run_live_marathon.add_argument("--summary-jsonl", type=Path, default=Path("observations") / "live_marathon.jsonl")
    run_live_marathon.add_argument("--kb-overlay", type=Path, default=Path("data") / "kb_overlay.json")
    run_live_marathon.add_argument("--harvest-dir", type=Path, default=Path("data") / "harvest")
    run_live_marathon.add_argument("--harvest-confidence-threshold", type=float, default=0.55)
    run_live_marathon.add_argument("--auto-learn-kb", action="store_true")
    run_live_marathon.add_argument("--kb-learn-model", type=str, default="gpt-5.4")
    run_live_marathon.add_argument("--kb-learn-timeout-seconds", type=float, default=60.0)
    run_live_marathon.add_argument("--kb-learn-min-cases", type=int, default=3)
    run_live_marathon.add_argument("--kb-learn-max-cases", type=int, default=8)
    run_live_marathon.add_argument("--kb-learn-cooldown-seconds", type=float, default=90.0)
    run_live_marathon.add_argument("--capture-backend", type=str, default=None)
    run_live_marathon.add_argument("--input-backend", type=str, default=None)
    run_live_marathon.add_argument("--allow-foreground-fallback", action="store_true")
    run_live_marathon.add_argument("--between-sessions-seconds", type=float, default=1.0)
    run_live_marathon.add_argument("--no-tick-log", action="store_true", help="Suppress per-tick stdout and only print per-session summaries")
    run_live_marathon.add_argument("--dry-run", action="store_true")

    bootstrap_live = subparsers.add_parser("bootstrap-live", help="Explicitly use opt-in foreground fallback to move past startup scenes")
    bootstrap_live.add_argument("--profile", type=Path, required=True)
    bootstrap_live.add_argument("--backend", type=str, default="legacy")
    bootstrap_live.add_argument("--max-steps", type=int, default=30)
    bootstrap_live.add_argument("--stop-screen", type=str, choices=[screen.value for screen in ScreenKind], default=ScreenKind.BATTLE.value)
    bootstrap_live.add_argument("--min-floor", type=int, default=0)
    bootstrap_live.add_argument("--focus-window", action="store_true")
    bootstrap_live.add_argument("--capture-backend", type=str, default=None)
    bootstrap_live.add_argument("--input-backend", type=str, default="legacy")
    bootstrap_live.add_argument("--allow-foreground-fallback", action="store_true")
    bootstrap_live.add_argument("--dry-run", action="store_true")

    startup_sequence = subparsers.add_parser("startup-sequence-live", help="Run a scripted startup sequence with explicit foreground focus")
    startup_sequence.add_argument("--profile", type=Path, required=True)
    startup_sequence.add_argument(
        "--labels",
        type=str,
        default="Single Play,Standard run,Select Ironclad,Take default whale option,Advance whale dialog,Close relic popup",
        help="Comma-separated action labels to execute in order",
    )
    startup_sequence.add_argument("--backend", type=str, default="legacy")
    startup_sequence.add_argument("--focus-window", action="store_true")
    startup_sequence.add_argument("--capture-backend", type=str, default=None)
    startup_sequence.add_argument("--input-backend", type=str, default="legacy")
    startup_sequence.add_argument("--allow-foreground-fallback", action="store_true")
    startup_sequence.add_argument("--dry-run", action="store_true")

    hybrid_run = subparsers.add_parser("hybrid-run-live", help="Run startup with explicit foreground fallback, then continue with scene-aware backends")
    hybrid_run.add_argument("--profile", type=Path, required=True)
    hybrid_run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    hybrid_run.add_argument("--max-steps", type=int, default=120)
    hybrid_run.add_argument("--focus-window", action="store_true")
    hybrid_run.add_argument("--allow-foreground-fallback", action="store_true")
    hybrid_run.add_argument("--dry-run", action="store_true")
    hybrid_run.add_argument("--startup-only", action="store_true")
    hybrid_run.add_argument("--skip-startup", action="store_true")

    analyze = subparsers.add_parser("analyze", help="Generate build insight report")
    analyze.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    analyze.add_argument("--min-samples", type=int, default=2)
    analyze.add_argument("--json-out", type=Path, default=Path("reports") / "builds.json")

    trace_run = subparsers.add_parser("trace-run", help="Render the decision trace for the latest or specified run")
    trace_run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    trace_run.add_argument("--run-id", type=str, default=None)
    trace_run.add_argument("--json-out", type=Path, default=None)

    fix_request = subparsers.add_parser("draft-fix-request", help="Draft a markdown fix request from the latest or specified run trace")
    fix_request.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    fix_request.add_argument("--run-id", type=str, default=None)
    fix_request.add_argument("--output", type=Path, default=Path("reports") / "fix_request.md")

    learn_kb = subparsers.add_parser("learn-kb", help="Apply AI-generated overlay KB updates from pending harvest cases")
    learn_kb.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    learn_kb.add_argument("--kb-overlay", type=Path, default=Path("data") / "kb_overlay.json")
    learn_kb.add_argument("--model", type=str, default="gpt-5.4")
    learn_kb.add_argument("--timeout-seconds", type=float, default=60.0)
    learn_kb.add_argument("--min-cases", type=int, default=3)
    learn_kb.add_argument("--max-cases", type=int, default=8)
    learn_kb.add_argument("--cooldown-seconds", type=float, default=0.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if hasattr(args, "profile") and isinstance(getattr(args, "profile"), Path):
        args.profile = _resolve_repo_path(getattr(args, "profile"))

    if args.command == "managed-controls-license-status":
        status = get_managed_controls_license_status(args.command, storage_dir=_repo_root() / ".managed_controls")
        print(_render_managed_controls_license_status(status))
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(status.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed controls license status to {args.json_out}")
        return

    if args.command == "activate-managed-controls":
        try:
            status = activate_managed_controls(args.license_key, storage_dir=_repo_root() / ".managed_controls")
        except ManagedControlsLicenseError as exc:
            print(f"license_error={_safe_console_text(str(exc))}")
            return
        print(_render_managed_controls_license_status(status))
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(status.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed controls activation result to {args.json_out}")
        return

    if args.command == "open-managed-controls-purchase":
        status = get_managed_controls_license_status(args.command, storage_dir=_repo_root() / ".managed_controls")
        config = load_managed_controls_commerce_config(storage_dir=_repo_root() / ".managed_controls")
        purchase_url = open_purchase_page(install_id=status.install_id, config=config)
        if not purchase_url:
            print("purchase_error=STS_MANAGED_CONTROLS_PURCHASE_URL is not configured")
            print(_render_managed_controls_license_status(status))
            return
        print(f"purchase_url={purchase_url}")
        if args.json_out is not None:
            payload = {"install_id": status.install_id, "purchase_url": purchase_url}
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed controls purchase link to {args.json_out}")
        return

    if args.command == "open-managed-controls-activation-guide":
        config = load_managed_controls_commerce_config(storage_dir=_repo_root() / ".managed_controls")
        guide_url = open_activation_guide(config=config)
        if not guide_url:
            print("activation_guide_error=No activation guide URL is configured")
            return
        print(f"activation_guide_url={guide_url}")
        if args.json_out is not None:
            payload = {"activation_guide_url": guide_url}
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed controls activation guide URL to {args.json_out}")
        return

    if args.command == "issue-managed-controls-license":
        private_key_file = _resolve_repo_path(args.private_key_file)
        expires_at = None if args.no_expiry else datetime.now(timezone.utc) + timedelta(days=max(1, args.days))
        try:
            token = issue_managed_controls_license(
                install_id=args.install_id,
                licensee=args.licensee,
                private_key_pem=private_key_file.read_text(encoding="utf-8"),
                plan=args.plan,
                expires_at=expires_at,
            )
        except (ManagedControlsLicenseError, OSError) as exc:
            print(f"license_error={_safe_console_text(str(exc))}")
            return
        result = {
            "install_id": args.install_id,
            "licensee": args.licensee,
            "plan": args.plan,
            "expires_at": "" if expires_at is None else expires_at.isoformat().replace("+00:00", "Z"),
            "license_key": token,
        }
        print(f"license_key={token}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed controls issued license to {args.json_out}")
        return

    if args.command == "serve-managed-controls-issuer":
        private_key_file = _resolve_repo_path(args.private_key_file)
        config = ManagedControlsIssuerConfig(
            host=args.host,
            port=args.port,
            private_key_file=private_key_file,
            admin_token=args.admin_token,
            default_plan=args.default_plan,
            default_days=args.default_days,
        )
        print(f"issuer_service listening=http://{config.host}:{config.port} plan={config.default_plan} days={config.default_days}")
        run_issuer_service(config)
        return

    if args.command == "serve-managed-controls-fulfillment":
        private_key_file = _resolve_repo_path(args.private_key_file)
        storage_dir = _resolve_repo_path(args.storage_dir) if args.storage_dir is not None else (_repo_root() / ".managed_controls")
        commerce = load_managed_controls_commerce_config(storage_dir=storage_dir)
        config = ManagedControlsFulfillmentServiceConfig(
            host=args.host,
            port=args.port,
            private_key_file=private_key_file,
            admin_token=args.admin_token,
            default_plan=args.default_plan,
            default_days=args.default_days,
            storage_dir=storage_dir,
            commerce=commerce,
        )
        print(
            f"fulfillment_service listening=http://{config.host}:{config.port} provider={commerce.provider} "
            f"purchase_url={'configured' if commerce.purchase_url else 'missing'}"
        )
        run_fulfillment_service(config)
        return

    if args.command != "managed-control-ui":
        blocked = _require_managed_controls_license_if_needed(args.command)
        if blocked:
            return

    if args.command == "init-db":
        logger = RunLogger(args.db)
        logger.init_db()
        print(f"Initialized database at {args.db}")
        return

    if args.command == "simulate":
        logger = RunLogger(args.db)
        logger.init_db()
        engine = AutoplayEngine(adapter=MockAdapter(), policy=HeuristicPolicy(), logger=logger)
        for index in range(args.episodes):
            result = engine.run_episode()
            outcome = "win" if result.summary.won else "loss"
            print(
                f"episode={index + 1} run_id={result.run_id} outcome={outcome} "
                f"tags={','.join(result.summary.strategy_tags)}"
            )
        return

    if args.command == "write-example-profile":
        path = write_example_profile(args.path)
        profile = CalibrationProfile.load(path)
        if args.input_backend is not None:
            profile.input_backend_name = args.input_backend
        if args.capture_backend is not None:
            profile.capture_backend_name = args.capture_backend
        if args.allow_foreground_fallback:
            profile.allow_foreground_fallback = True
        path.write_text(
            __import__("json").dumps(profile.to_dict(base_dir=path.parent), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        print(f"Wrote example profile to {path}")
        return

    if args.command == "capability-report":
        profile = _load_profile_for_live(args.profile, args)
        runtime = create_runtime(profile)
        try:
            report = runtime.capability_report()
            print(_render_capability_report(report))
            try:
                frame = runtime.capture_backend.read_latest_frame(timeout_ms=250)
                print("sample_capture=ok")
                print(f"sample_blank={_image_is_blank(frame)}")
                print(f"sample_frame_size={frame.size[0]}x{frame.size[1]}")
            except Exception as exc:
                print("sample_capture=error")
                print(f"sample_capture_error={exc}")
        finally:
            runtime.close()
        return

    if args.command == "bg-capture-smoke":
        profile = _load_profile_for_live(args.profile, args)
        runtime = create_runtime(profile)
        try:
            report = runtime.capability_report()
            before_title = foreground_window_title()
            before_cursor = cursor_position()
            timestamps: list[float] = []
            frame_sizes: list[tuple[int, int]] = []
            blank_frames = 0
            if args.save_dir is not None:
                args.save_dir.mkdir(parents=True, exist_ok=True)
            for index in range(args.frames):
                frame = runtime.capture_backend.read_latest_frame(timeout_ms=args.timeout_ms)
                timestamps.append(time.perf_counter())
                frame_sizes.append(frame.size)
                if _image_is_blank(frame):
                    blank_frames += 1
                if args.save_dir is not None and index in {0, args.frames // 2, args.frames - 1}:
                    frame.save(args.save_dir / f"frame_{index:04d}.png")
            after_title = foreground_window_title()
            after_cursor = cursor_position()
            intervals = [curr - prev for prev, curr in zip(timestamps, timestamps[1:])]
            mean_interval_ms = statistics.mean(intervals) * 1000 if intervals else 0.0
            print(_render_capability_report(report))
            print(f"frames={len(frame_sizes)}")
            print(f"frame_size={frame_sizes[0][0]}x{frame_sizes[0][1]}")
            print(f"avg_interval_ms={mean_interval_ms:.2f}")
            print(f"blank_frames={blank_frames}")
            print(f"foreground_before={_safe_console_text(before_title)}")
            print(f"foreground_after={_safe_console_text(after_title)}")
            print(f"cursor_before={before_cursor[0]},{before_cursor[1]}")
            print(f"cursor_after={after_cursor[0]},{after_cursor[1]}")
            print(f"foreground_unchanged={before_title == after_title}")
            print(f"cursor_unchanged={before_cursor == after_cursor}")
        finally:
            runtime.close()
        return

    if args.command == "bg-input-smoke":
        _run_bg_input_smoke(timeout_ms=args.timeout_ms)
        return

    if args.command == "inspect-window":
        profile = _load_profile_for_live(args.profile, args)
        runtime = create_runtime(profile)
        try:
            report = runtime.capability_report()
            print(_render_capability_report(report))
            print(_render_window_inspection(runtime.target.hwnd, max_depth=args.max_depth))
        finally:
            runtime.close()
        return

    if args.command == "game-dry-run":
        profile = _load_profile_for_live(args.profile, args)
        profile.dry_run = True
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = profile
        adapter.start_run(focus=False)
        report = adapter.capability_report()
        print(_render_capability_report(report))
        try:
            state = adapter.probe_fast()
        except Exception as exc:
            print(f"probe_error={exc}")
            return
        actions = state.available_actions
        if not actions:
            print("No actions available on the current screen.")
            return
        action = next((item for item in actions if item.label == args.label), None) if args.label is not None else HeuristicPolicy().choose_action(state, actions)
        if action is None:
            print(f"Action not found: {args.label}")
            return
        print(f"screen={state.screen.value}")
        print(f"action={action.kind.value}:{action.label}")
        definition = next(item for item in adapter.profile.actions if item.screen == state.screen and item.label == action.label)
        if definition.point != (0, 0):
            point = adapter._scale_reference_point(definition.point)
            print(f"client_point={point[0]},{point[1]}")
        print(f"backend={report.selected_input_backend}")
        print("dry_run=true")
        return

    if args.command == "bg-game-input-probe":
        profile = _load_profile_for_live(args.profile, args)
        result = _run_bg_game_input_probe(
            profile=profile,
            key=args.key,
            point=args.point,
            label=args.label,
            double_click=args.double_click,
            sleep_seconds=args.sleep,
            save_before=args.save_before,
            save_after=args.save_after,
        )
        print(_render_capability_report(result["report"]))
        for line in _render_probe_result_lines(result):
            print(line)
        return

    if args.command == "bg-game-input-matrix":
        base_profile = _load_profile_for_live(args.profile, args)
        deliveries = [item.strip() for item in args.deliveries.split(",") if item.strip()]
        activations = [item.strip() for item in args.activations.split(",") if item.strip()]
        results = []
        for delivery in deliveries:
            for activation in activations:
                profile = CalibrationProfile.load(args.profile)
                profile.capture_backend_name = base_profile.capture_backend_name
                profile.input_backend_name = base_profile.input_backend_name
                profile.allow_foreground_fallback = base_profile.allow_foreground_fallback
                profile.window_message_delivery = delivery
                profile.window_message_activation = activation
                result = _run_bg_game_input_probe(
                    profile=profile,
                    key=args.key,
                    point=args.point,
                    label=args.label,
                    double_click=args.double_click,
                    sleep_seconds=args.sleep,
                )
                result["delivery"] = delivery
                result["activation"] = activation
                results.append(result)
                frame_diff = result.get("frame_diff")
                observed_effect = result.get("observed_effect")
                print(
                    f"delivery={delivery} activation={activation} "
                    f"observed_effect={observed_effect} frame_diff={frame_diff if frame_diff is not None else 'n/a'}"
                )
        print("matrix_summary:")
        for result in sorted(results, key=lambda item: item.get("frame_diff") or 0.0, reverse=True):
            print(
                f"- delivery={result['delivery']} activation={result['activation']} "
                f"observed_effect={result.get('observed_effect')} frame_diff={result.get('frame_diff')}"
            )
        return

    if args.command == "probe-live":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            adapter.start_run(focus=args.focus)
            state = adapter.probe_fast() if args.fast else adapter.probe()
            print(
                f"screen={state.screen.value} act={state.act} floor={state.floor} "
                f"hp={state.hp}/{state.max_hp} gold={state.gold} "
                f"energy={state.energy}/{state.max_energy or state.energy} block={state.block} "
                f"source={state.state_source.value}"
            )
            if state.player_powers:
                print(f"player_powers={state.player_powers}")
            if state.enemies:
                for index, enemy in enumerate(state.enemies):
                    print(
                        f"enemy[{index}] hp={enemy.hp}/{enemy.max_hp} block={enemy.block or 0} "
                        f"intent={enemy.intent_damage} powers={enemy.powers}"
                    )
            if state.available_actions:
                for action in state.available_actions:
                    print(f"- {action.kind.value}: {action.label} [{','.join(action.tags)}]")
            if args.show_anchor_scores:
                for name, score in sorted(adapter.last_anchor_scores().items()):
                    print(f"anchor={name} score={score:.4f}")
            if args.show_metric_sources:
                for line in _render_metric_source_lines(adapter.last_metric_sources()):
                    print(line)
            if args.save_screenshot is not None:
                screenshot = adapter.capture_image()
                args.save_screenshot.parent.mkdir(parents=True, exist_ok=True)
                screenshot.save(args.save_screenshot)
                print(f"Saved screenshot to {args.save_screenshot}")
        finally:
            adapter.close()
        return

    if args.command == "probe-memory":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            adapter.start_run(focus=args.focus)
            state = adapter.probe_fast()
            payload = adapter.probe_memory(screen=state.screen)
            for line in _render_memory_probe_lines(state.screen.value, payload):
                print(line)
            if args.json_out is not None:
                args.json_out.parent.mkdir(parents=True, exist_ok=True)
                args.json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
                print(f"Saved memory probe to {args.json_out}")
        finally:
            adapter.close()
        return

    if args.command == "probe-managed":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        try:
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        print(
            f"floor={summary.floor} ascension={summary.ascension} "
            f"hp={summary.hp}/{summary.max_hp} gold={summary.gold} energy={summary.energy}/{summary.max_energy}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(summary.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved managed probe to {args.json_out}")
        return

    if args.command == "set-managed-block":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        try:
            result = set_managed_player_block(runtime.target.pid, args.value, workspace_dir=_repo_root())
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        print(f"write_field={result.field} address={result.address} previous={result.previous} requested={result.requested}")
        print(
            f"verified_block={summary.block} hp={summary.hp}/{summary.max_hp} "
            f"gold={summary.gold} energy={summary.energy}/{summary.max_energy}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "write": result.to_dict(),
                        "verified": summary.to_dict(),
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed write to {args.json_out}")
        return

    if args.command == "set-managed-gold":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        try:
            result = set_managed_player_gold(runtime.target.pid, gold=args.value, workspace_dir=_repo_root())
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        print(
            f"write_field={result.field} previous_gold={result.previous_gold} requested_gold={result.requested_gold} "
            f"previous_ui_gold={result.previous_ui_gold} wrote_ui_gold={result.wrote_ui_gold}"
        )
        print(
            f"verified_gold={summary.gold} hp={summary.hp}/{summary.max_hp} "
            f"block={summary.block} energy={summary.energy}/{summary.max_energy}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "write": result.to_dict(),
                        "verified": summary.to_dict(),
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed gold write to {args.json_out}")
        return

    if args.command == "set-managed-energy":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        try:
            result = set_managed_player_energy(
                runtime.target.pid,
                energy=args.value,
                max_energy=args.max_value,
                workspace_dir=_repo_root(),
            )
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        print(
            f"write_field={result.field} previous_energy={result.previous_energy} requested_energy={result.requested_energy} "
            f"previous_max_energy={result.previous_max_energy} requested_max_energy={result.requested_max_energy} "
            f"wrote_max_energy={result.wrote_max_energy}"
        )
        print(
            f"verified_energy={summary.energy}/{summary.max_energy} hp={summary.hp}/{summary.max_hp} "
            f"gold={summary.gold} block={summary.block}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "write": result.to_dict(),
                        "verified": summary.to_dict(),
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed energy write to {args.json_out}")
        return

    if args.command == "maintain-managed-energy":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        history: list[dict[str, object]] = []
        try:
            pid = runtime.target.pid
            iteration = 0
            deadline = time.time() + max(0.0, args.seconds)
            while True:
                if args.iterations is not None and iteration >= max(0, args.iterations):
                    break
                if args.iterations is None and iteration > 0 and time.time() >= deadline:
                    break

                result = set_managed_player_energy(
                    pid,
                    energy=args.value,
                    max_energy=args.max_value,
                    workspace_dir=_repo_root(),
                )
                summary = probe_managed_numeric(pid, workspace_dir=_repo_root())
                iteration += 1
                tick = {
                    "iteration": iteration,
                    "requested_energy": args.value,
                    "requested_max_energy": args.max_value,
                    "previous_energy": result.previous_energy,
                    "previous_max_energy": result.previous_max_energy,
                    "verified_energy": summary.energy,
                    "verified_max_energy": summary.max_energy,
                    "hp": summary.hp,
                    "max_hp": summary.max_hp,
                    "gold": summary.gold,
                    "block": summary.block,
                }
                history.append(tick)
                print(
                    f"tick={iteration} requested_energy={args.value} requested_max_energy={args.max_value} "
                    f"previous_energy={result.previous_energy} previous_max_energy={result.previous_max_energy} "
                    f"verified_energy={summary.energy}/{summary.max_energy} "
                    f"hp={summary.hp}/{summary.max_hp} gold={summary.gold} block={summary.block}"
                )
                if args.iterations is not None and iteration >= max(0, args.iterations):
                    break
                if args.iterations is None and time.time() >= deadline:
                    break
                time.sleep(max(0.0, args.interval))
        finally:
            runtime.close()
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "value": args.value,
                        "max_value": args.max_value,
                        "interval": args.interval,
                        "seconds": args.seconds,
                        "iterations": args.iterations,
                        "history": history,
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed energy maintenance to {args.json_out}")
        return

    if args.command == "maintain-managed-block":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        history: list[dict[str, object]] = []
        try:
            pid = runtime.target.pid
            iteration = 0
            deadline = time.time() + max(0.0, args.seconds)
            while True:
                if args.iterations is not None and iteration >= max(0, args.iterations):
                    break
                if args.iterations is None and iteration > 0 and time.time() >= deadline:
                    break

                result = set_managed_player_block(pid, args.value, workspace_dir=_repo_root())
                summary = probe_managed_numeric(pid, workspace_dir=_repo_root())
                iteration += 1
                tick = {
                    "iteration": iteration,
                    "requested": args.value,
                    "previous": result.previous,
                    "verified_block": summary.block,
                    "hp": summary.hp,
                    "max_hp": summary.max_hp,
                    "gold": summary.gold,
                    "energy": summary.energy,
                    "max_energy": summary.max_energy,
                }
                history.append(tick)
                print(
                    f"tick={iteration} requested={args.value} previous={result.previous} "
                    f"verified_block={summary.block} hp={summary.hp}/{summary.max_hp} "
                    f"gold={summary.gold} energy={summary.energy}/{summary.max_energy}"
                )
                if args.iterations is not None and iteration >= max(0, args.iterations):
                    break
                if args.iterations is None and time.time() >= deadline:
                    break
                time.sleep(max(0.0, args.interval))
        finally:
            runtime.close()
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "value": args.value,
                        "interval": args.interval,
                        "seconds": args.seconds,
                        "iterations": args.iterations,
                        "history": history,
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed block maintenance to {args.json_out}")
        return

    if args.command == "set-managed-power":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        bridge_result = None
        try:
            try:
                result = set_managed_power_amount(
                    runtime.target.pid,
                    target=args.target,
                    power_type=args.power_type,
                    value=args.value,
                    workspace_dir=_repo_root(),
                )
            except ManagedProbeError as exc:
                if "power not found:" not in str(exc):
                    raise
                result = None
                bridge_result = send_bridge_apply_power(
                    power_type=args.power_type,
                    amount=args.value,
                    target=args.target,
                )
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        if bridge_result is None:
            print(
                f"write_field={result.field} target={result.target} power_type={result.power_type} "
                f"power_address={result.power_address} address={result.address} "
                f"previous={result.previous} requested={result.requested}"
            )
        else:
            print("bridge_fallback=power_not_found")
            print(f"bridge_request={json.dumps(bridge_result.request, ensure_ascii=True)}")
            print(f"bridge_response={json.dumps(bridge_result.response, ensure_ascii=True)}")
        print(
            f"verified_block={summary.block} hp={summary.hp}/{summary.max_hp} "
            f"gold={summary.gold} energy={summary.energy}/{summary.max_energy}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "write": result.to_dict(),
                        "verified": summary.to_dict(),
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed power write to {args.json_out}")
        return

    if args.command == "alias-managed-powers":
        profile = _load_profile_for_live(args.profile, args)
        if args.focus:
            _focus_profile_window(profile)
            time.sleep(0.10)
        runtime = create_runtime(profile)
        try:
            result = alias_managed_powers(runtime.target.pid, source=args.source, dest=args.dest, workspace_dir=_repo_root())
            summary = probe_managed_numeric(runtime.target.pid, workspace_dir=_repo_root())
        finally:
            runtime.close()
        print(
            f"write_field={result.field} source={result.source} dest={result.dest} "
            f"previous={result.previous} requested={result.requested} address={result.address}"
        )
        print(
            f"verified_block={summary.block} hp={summary.hp}/{summary.max_hp} "
            f"gold={summary.gold} energy={summary.energy}/{summary.max_energy}"
        )
        for line in _render_managed_probe_lines(summary):
            print(line)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "write": result.to_dict(),
                        "verified": summary.to_dict(),
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved managed power alias to {args.json_out}")
        return

    if args.command == "enable-dev-console":
        result = enable_full_console(settings_root=args.settings_root)
        print(
            f"searched_root={result.searched_root} updated={len(result.updated_paths)} unchanged={len(result.unchanged_paths)}"
        )
        for path in result.updated_paths:
            print(f"updated_settings={path}")
        for path in result.unchanged_paths:
            print(f"unchanged_settings={path}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved dev console settings result to {args.json_out}")
        return

    if args.command == "run-console-command":
        profile = _load_profile_for_live(args.profile, args)
        result = run_dev_console_command(
            profile,
            args.command_text,
            backend=args.backend,
            open_key=args.open_key,
            typing_interval=args.typing_interval,
            close_console=not args.leave_open,
            ensure_full_console_enabled=not args.skip_enable_full_console,
            settings_root=args.settings_root,
        )
        print(
            f"console_command={_safe_console_text(result.command)} pid={result.pid} hwnd=0x{result.hwnd:x} "
            f"backend={result.backend} close_console={result.close_console}"
        )
        print(
            f"settings_updated={len(result.settings.updated_paths)} "
            f"settings_unchanged={len(result.settings.unchanged_paths)}"
        )
        for path in result.settings.updated_paths:
            print(f"updated_settings={path}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved console command result to {args.json_out}")
        return

    if args.command == "managed-control-ui":
        _launch_managed_control_ui(args.profile)
        return

    if args.command == "install-bridge-mod":
        try:
            result = install_bridge_mod(game_dir=args.game_dir, workspace_dir=_repo_root())
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_mod_dir={result.mod_dir}")
        print(f"bridge_dll={result.dll_path}")
        print(f"bridge_manifest={result.manifest_path}")
        print("bridge_note=restart the game once to load the bridge mod")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge install result to {args.json_out}")
        return

    if args.command == "bridge-apply-power":
        try:
            result = send_bridge_apply_power(
                power_type=args.power_type,
                amount=args.value,
                target=args.target,
                enemy_index=args.enemy_index,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge power result to {args.json_out}")
        return

    if args.command == "bridge-add-card":
        try:
            result = send_bridge_add_card(
                card_type=args.card_type,
                destination=args.destination,
                count=args.count,
                upgrade_count=args.upgrade_count,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge card result to {args.json_out}")
        return

    if args.command == "bridge-replace-master-deck":
        try:
            result = send_bridge_replace_master_deck(
                card_type=args.card_type,
                count=args.count,
                upgrade_count=args.upgrade_count,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge deck result to {args.json_out}")
        return

    if args.command == "bridge-obtain-relic":
        try:
            result = send_bridge_obtain_relic(
                relic_type=args.relic_type,
                count=args.count,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge relic result to {args.json_out}")
        return

    if args.command == "bridge-set-auto-power":
        try:
            result = send_bridge_set_auto_power_on_combat_start(
                power_type=args.power_type,
                amount=args.value,
                target=args.target,
                enemy_index=args.enemy_index,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge auto-power result to {args.json_out}")
        return

    if args.command == "bridge-clear-auto-power":
        try:
            result = send_bridge_clear_auto_power_on_combat_start(
                power_type=args.power_type,
                target=args.target,
                enemy_index=args.enemy_index,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge auto-power clear result to {args.json_out}")
        return

    if args.command == "bridge-jump-map":
        try:
            result = send_bridge_jump_to_map_coord(col=args.col, row=args.row)
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge map jump result to {args.json_out}")
        return

    if args.command == "bridge-tune-card":
        try:
            result = send_bridge_tune_card_var(
                card_type=args.card_type,
                var_name=args.var_name,
                amount=args.value,
                scope=args.scope,
                mode=args.mode,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge card tuning result to {args.json_out}")
        return

    if args.command == "bridge-tune-relic":
        try:
            result = send_bridge_tune_relic_var(
                relic_type=args.relic_type,
                var_name=args.var_name,
                amount=args.value,
                mode=args.mode,
            )
        except ManagedProbeError as exc:
            print(f"bridge_error={_safe_console_text(str(exc))}")
            return
        print(f"bridge_request={json.dumps(result.request, ensure_ascii=True)}")
        print(f"bridge_response={json.dumps(result.response, ensure_ascii=True)}")
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
            print(f"Saved bridge relic tuning result to {args.json_out}")
        return

    if args.command == "list-game-catalog":
        if args.kind == "cards":
            entries = load_card_catalog(workspace_dir=_repo_root())
        elif args.kind == "powers":
            entries = load_power_catalog(workspace_dir=_repo_root())
        else:
            entries = load_relic_catalog(workspace_dir=_repo_root())
        if args.query:
            entries = filter_catalog(entries, args.query)
        print(f"catalog_kind={args.kind} count={len(entries)}")
        for entry in entries[:200]:
            print(
                "catalog_entry="
                f"{entry.display_name}|{entry.short_name}|{entry.type_name}|default_ctor={entry.has_parameterless_constructor}"
            )
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "kind": args.kind,
                        "query": args.query,
                        "count": len(entries),
                        "entries": [entry.to_dict() for entry in entries],
                    },
                    indent=2,
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            print(f"Saved catalog result to {args.json_out}")
        return

    if args.command == "probe-image":
        adapter = WindowsStsAdapter(args.profile)
        screenshot = Image.open(args.input)
        state = adapter.inspect_image(screenshot, read_metrics=not args.fast)
        print(
            f"screen={state.screen.value} act={state.act} floor={state.floor} "
            f"hp={state.hp}/{state.max_hp} gold={state.gold} energy={state.energy}"
        )
        if state.available_actions:
            for action in state.available_actions:
                print(f"- {action.kind.value}: {action.label} [{','.join(action.tags)}]")
        if args.show_anchor_scores:
            for name, score in sorted(adapter.last_anchor_scores().items()):
                print(f"anchor={name} score={score:.4f}")
        return

    if args.command == "capture-live":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            if args.focus:
                adapter.start_run(focus=True)
            screenshot = adapter.capture_image()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            screenshot.save(args.output)
            print(f"Saved screenshot to {args.output}")
            print(f"size={screenshot.size[0]}x{screenshot.size[1]}")
        finally:
            adapter.close()
        return

    if args.command == "step-live":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            if args.backend in {"window_messages", "legacy", "auto"}:
                adapter.profile.input_backend_name = args.backend
            adapter.start_run(focus=False)
            before = adapter.capture_image_retry()
            state = _stable_live_state(adapter, fast=True)
            if args.save_before is not None:
                args.save_before.parent.mkdir(parents=True, exist_ok=True)
                before.save(args.save_before)
                print(f"Saved before screenshot to {args.save_before}")
            actions = state.available_actions
            if not actions:
                print(f"No actions available on the current screen. screen={state.screen.value}")
                return
            if args.label is not None:
                matching = [action for action in actions if action.label == args.label]
                if not matching:
                    print(f"Action not found: {args.label}")
                    return
                action = matching[0]
            else:
                action = HeuristicPolicy().choose_action(state, actions)
            print(f"Applying {action.kind.value}: {action.label}")
            used_backend = adapter.apply_action(action, backend=args.backend, mode=args.mode)
            print(f"backend={used_backend} mode={args.mode}")
            time.sleep(max(0.05, 0.15 if args.mode == 'key' else 0.25))
            next_state = _stable_live_state(adapter, fast=True, attempts=4, delay_seconds=0.18)
            after = adapter.capture_image_retry(attempts=5, backoff_seconds=0.10)
            if args.save_after is not None:
                args.save_after.parent.mkdir(parents=True, exist_ok=True)
                after.save(args.save_after)
                print(f"Saved after screenshot to {args.save_after}")
            print(
                f"next_screen={next_state.screen.value} hp={next_state.hp}/{next_state.max_hp} "
                f"gold={next_state.gold} energy={next_state.energy}"
            )
        finally:
            adapter.close()
        return

    if args.command == "exercise-input-live":
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = _load_profile_for_live(args.profile, args)
        adapter.start_run(focus=True)
        baseline_image = adapter.capture_image()
        baseline_state = adapter.inspect_image(baseline_image)
        actions = baseline_state.available_actions
        if not actions:
            print("No actions available on the current screen.")
            return
        if args.label is not None:
            matching = [action for action in actions if action.label == args.label]
            if not matching:
                print(f"Action not found: {args.label}")
                return
            action = matching[0]
        else:
            action = HeuristicPolicy().choose_action(baseline_state, actions)
        print(f"baseline_screen={baseline_state.screen.value} action={action.kind.value}:{action.label}")
        action_point = _resolve_action_point(adapter, baseline_state.screen.value, action.label)
        requested_modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
        requested_backends = [item.strip() for item in args.backends.split(",") if item.strip()]
        backend_list: list[str] = []
        for backend_name in requested_backends:
            backend_list.extend(backend_candidates(backend_name, include_key_only=True))
        seen: set[tuple[str, str]] = set()
        for mode in requested_modes:
            for backend_name in backend_list:
                if mode == "click" and backend_name == "sendinput_scan":
                    continue
                key = (backend_name, mode)
                if key in seen:
                    continue
                seen.add(key)
                before_image = adapter.capture_image()
                before_state = adapter.inspect_image(before_image)
                if args.save_dir is not None:
                    args.save_dir.mkdir(parents=True, exist_ok=True)
                    before_path = args.save_dir / f"{mode}_{backend_name}_before.png"
                    before_image.save(before_path)
                print(f"trying backend={backend_name} mode={mode} screen={before_state.screen.value}")
                try:
                    used_backend = adapter.apply_action(action, backend=backend_name, mode=mode)
                except Exception as exc:
                    print(f"result backend={backend_name} mode={mode} error={exc}")
                    continue
                time.sleep(max(0.1, args.sleep))
                after_image = adapter.capture_image()
                after_state = adapter.inspect_image(after_image)
                diff_score = _image_diff_score(before_image, after_image)
                focus_score = _image_diff_score(
                    before_image,
                    after_image,
                    focus_box=_point_focus_box(point=action_point, image_size=after_image.size),
                )
                if args.save_dir is not None:
                    after_path = args.save_dir / f"{mode}_{backend_name}_after.png"
                    after_image.save(after_path)
                print(
                    f"result backend={used_backend} mode={mode} next_screen={after_state.screen.value} "
                    f"diff={diff_score:.2f} focus_diff={focus_score:.2f}"
                )
                if after_state.screen != before_state.screen or focus_score >= 2.0:
                    print("Input changed the screen or produced a focused UI delta.")
                    return
        print("No backend produced a confirmed screen change.")
        return

    if args.command == "inject-live":
        if args.key is None and args.point is None:
            print("Specify either --key or --point.")
            return
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            adapter.start_run(focus=False)
            before_image = adapter.capture_image_retry()
            before_state = adapter.inspect_image(before_image, read_metrics=False)
            if args.save_before is not None:
                args.save_before.parent.mkdir(parents=True, exist_ok=True)
                before_image.save(args.save_before)
                print(f"Saved before screenshot to {args.save_before}")
            point = _parse_point(args.point) if args.point is not None else None
            used_backend = adapter.inject_input(
                backend=args.backend,
                key=args.key,
                point=point,
                delay_ms=min(adapter.profile.action_delay_ms, 120),
                hold_ms=args.hold_ms,
                repeat=args.repeat,
            )
            time.sleep(max(0.05, args.sleep))
            after_image = adapter.capture_image_retry(attempts=5, backoff_seconds=0.10)
            after_state = adapter.inspect_image(after_image, read_metrics=False)
            if args.save_after is not None:
                args.save_after.parent.mkdir(parents=True, exist_ok=True)
                after_image.save(args.save_after)
                print(f"Saved after screenshot to {args.save_after}")
            focus_box = None
            if point is not None:
                focus_box = _point_focus_box(point=point, image_size=after_image.size)
            diff_score = _image_diff_score(before_image, after_image)
            focus_score = _image_diff_score(before_image, after_image, focus_box=focus_box)
            print(
                f"backend={used_backend} before_screen={before_state.screen.value} "
                f"next_screen={after_state.screen.value} diff={diff_score:.2f} focus_diff={focus_score:.2f}"
            )
        finally:
            adapter.close()
        return

    if args.command == "play-card-live":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            if args.backend in {"window_messages", "legacy", "auto"}:
                adapter.profile.input_backend_name = args.backend
            adapter.start_run(focus=False)
            before_image = adapter.capture_image_retry()
            before_state = adapter.inspect_image(before_image, read_metrics=False)
            if args.save_before is not None:
                args.save_before.parent.mkdir(parents=True, exist_ok=True)
                before_image.save(args.save_before)
                print(f"Saved before screenshot to {args.save_before}")
            used_backend = adapter.play_card_slot(args.slot, backend=args.backend)
            time.sleep(0.18)
            after_image = adapter.capture_image_retry(attempts=5, backoff_seconds=0.10)
            after_state = adapter.inspect_image(after_image, read_metrics=False)
            if args.save_after is not None:
                args.save_after.parent.mkdir(parents=True, exist_ok=True)
                after_image.save(args.save_after)
                print(f"Saved after screenshot to {args.save_after}")
            diff_score = _image_diff_score(before_image, after_image)
            print(
                f"slot={args.slot} backend={used_backend} before_screen={before_state.screen.value} "
                f"next_screen={after_state.screen.value} diff={diff_score:.2f}"
            )
        finally:
            adapter.close()
        return

    if args.command == "play-turn-live":
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = _load_profile_for_live(args.profile, args)
        if args.backend in {"window_messages", "legacy", "auto"}:
            adapter.profile.input_backend_name = args.backend
        try:
            adapter.start_run(focus=False)
            before_image = adapter.capture_image_retry()
            if args.save_before is not None:
                args.save_before.parent.mkdir(parents=True, exist_ok=True)
                before_image.save(args.save_before)
                print(f"Saved before screenshot to {args.save_before}")
            played = adapter.play_basic_battle_turn(
                backend=args.backend,
                max_slots=args.max_slots,
                time_budget_seconds=args.time_budget_seconds,
            )
            time.sleep(0.10)
            after_image = adapter.capture_image_retry(attempts=4, backoff_seconds=0.08)
            after_state = adapter.inspect_image(after_image, read_metrics=False)
            if (
                not played
                and not adapter._selection_requires_target(after_image)
                and adapter._active_card_selection_origin(after_image) is None
                and adapter._battle_progress_made(before_image, after_image)
            ):
                played = ["progress:inferred"]
            after_screen = after_state.screen
            if after_screen == ScreenKind.UNKNOWN and adapter._looks_like_battle_hud(after_image):
                after_screen = ScreenKind.BATTLE
            if args.save_after is not None:
                args.save_after.parent.mkdir(parents=True, exist_ok=True)
                after_image.save(args.save_after)
                print(f"Saved after screenshot to {args.save_after}")
            print(f"played={played or ['none']} next_screen={after_screen.value}")
        finally:
            adapter.close()
        return

    if args.command == "inject-gamepad-live":
        adapter = WindowsStsAdapter(args.profile)
        try:
            adapter.profile = _load_profile_for_live(args.profile, args)
            adapter.start_run(focus=False)
            before_image = adapter.capture_image_retry()
            before_state = adapter.inspect_image(before_image, read_metrics=False)
            if args.save_before is not None:
                args.save_before.parent.mkdir(parents=True, exist_ok=True)
                before_image.save(args.save_before)
                print(f"Saved before screenshot to {args.save_before}")
            button_names = [item.strip() for item in args.buttons.split(",") if item.strip()]
            adapter.send_gamepad_buttons(
                button_names,
                settle_ms=args.settle_ms,
                hold_ms=args.hold_ms,
                gap_ms=args.gap_ms,
            )
            time.sleep(max(0.05, args.sleep))
            after_image = adapter.capture_image_retry(attempts=5, backoff_seconds=0.10)
            after_state = adapter.inspect_image(after_image, read_metrics=False)
            if args.save_after is not None:
                args.save_after.parent.mkdir(parents=True, exist_ok=True)
                after_image.save(args.save_after)
                print(f"Saved after screenshot to {args.save_after}")
            diff_score = _image_diff_score(before_image, after_image)
            print(
                f"buttons={','.join(button_names)} before_screen={before_state.screen.value} "
                f"next_screen={after_state.screen.value} diff={diff_score:.2f}"
            )
        finally:
            adapter.close()
        return

    if args.command == "annotate-live":
        profile = _load_profile_for_live(args.profile, args)
        input_path = args.input
        if input_path is None:
            screenshot = WindowsStsAdapter(args.profile).capture_image()
            input_path = args.output.parent / f"{args.output.stem}.raw.png"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot.save(input_path)
            print(f"Saved raw screenshot to {input_path}")
        output = annotate_profile(input_path, profile, args.output)
        print(f"Saved annotated screenshot to {output}")
        return

    if args.command == "watch-live":
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = _load_profile_for_live(args.profile, args)
        adapter.start_run(focus=args.focus)
        deadline = time.time() + args.seconds
        previous_signature: tuple[object, ...] | None = None
        sample_index = 0
        while time.time() < deadline:
            state = adapter.probe()
            signature = (
                state.screen.value,
                state.act,
                state.floor,
                state.hp,
                state.max_hp,
                state.gold,
                state.energy,
                tuple(action.label for action in state.available_actions),
            )
            changed = signature != previous_signature
            if changed or not args.only_on_change:
                sample_index += 1
                capture_path = None
                if args.capture_dir is not None and (changed or not args.only_on_change):
                    screenshot = adapter.capture_image()
                    args.capture_dir.mkdir(parents=True, exist_ok=True)
                    capture_path = args.capture_dir / f"{sample_index:04d}_{state.screen.value}.png"
                    screenshot.save(capture_path)
                record = state_to_record(adapter, state, sample_index=sample_index, capture_path=capture_path)
                append_jsonl(args.jsonl_out, record)
                print(
                    f"sample={sample_index} screen={state.screen.value} floor={state.floor} "
                    f"hp={state.hp}/{state.max_hp} gold={state.gold} energy={state.energy}"
                )
                if capture_path is not None:
                    print(f"capture={capture_path}")
            previous_signature = signature
            time.sleep(max(0.1, args.interval))
        print(f"Wrote observations to {args.jsonl_out}")
        return

    if args.command == "summarize-observations":
        from collections import Counter

        rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"samples={len(rows)}")
        print("screens:")
        for screen, count in Counter(row["screen"] for row in rows).most_common():
            print(f"- {screen}: {count}")
        metric_names = sorted({metric for row in rows for metric in row.get("metrics", {}).keys()})
        if metric_names:
            print("metrics:")
            for metric_name in metric_names:
                non_null = sum(1 for row in rows if row.get("metrics", {}).get(metric_name) not in (None, "", 0, [0, 0]))
                print(f"- {metric_name}: {non_null}/{len(rows)} non-null")
        unknown_rows = [row for row in rows if row["screen"] == "unknown"]
        if unknown_rows:
            print("unknown_samples:")
            for row in unknown_rows[:10]:
                print(
                    f"- sample={row.get('sample_index')} capture={row.get('capture_path')} "
                    f"anchors={row.get('anchor_scores', {})}"
                )
        return

    if args.command == "crop-template":
        rect = parse_rect(args.rect)
        output = crop_to_file(args.input, rect, args.output)
        print(f"Cropped template to {output}")
        return

    if args.command == "run-live":
        logger = RunLogger(args.db)
        logger.init_db()
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = _load_profile_for_live(args.profile, args)
        if args.bootstrap:
            adapter.enable_bootstrap_on_start()
        engine = AutoplayEngine(
            adapter=adapter,
            policy=HeuristicPolicy(),
            logger=logger,
        )
        for index in range(args.episodes):
            result = engine.run_episode(max_steps=args.max_steps)
            outcome = "win" if result.summary.won else "loss"
            print(
                f"episode={index + 1} run_id={result.run_id} outcome={outcome} "
                f"floor={result.summary.floor_reached} tags={','.join(result.summary.strategy_tags)}"
            )
        return

    if args.command == "run-live-loop":
        logger = RunLogger(args.db)
        logger.init_db()
        set_active_kb_overlay_path(args.kb_overlay)
        result = _run_live_loop_session(args, logger, emit_ticks=True)
        _print_live_loop_result(result, stream_jsonl=args.stream_jsonl)
        return

    if args.command == "run-live-marathon":
        logger = RunLogger(args.db)
        logger.init_db()
        set_active_kb_overlay_path(args.kb_overlay)
        _run_live_marathon(args, logger)
        return

    if args.command == "bootstrap-live":
        profile = _load_profile_for_live(args.profile, args)
        profile.allow_foreground_fallback = True
        profile.input_backend_name = "legacy"
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = profile
        if args.focus_window:
            _focus_profile_window(profile)
            time.sleep(0.25)
        adapter.start_run(focus=False)
        policy = HeuristicPolicy()
        stop_screen = ScreenKind(args.stop_screen)
        for step_index in range(args.max_steps):
            state = adapter.current_state()
            print(
                f"step={step_index} screen={state.screen.value} floor={state.floor} "
                f"hp={state.hp}/{state.max_hp} gold={state.gold} energy={state.energy}"
            )
            if state.screen == stop_screen and state.floor >= args.min_floor:
                print(f"stopped_at={state.screen.value} floor={state.floor}")
                return
            actions = adapter.available_actions()
            if not actions:
                print("bootstrap_error=no_actions_available")
                return
            evaluations = policy.evaluate_actions(state, actions)
            best = max(evaluations, key=lambda item: (item.score, -len(item.action_label)))
            action = next(action for action in actions if action.label == best.action_label)
            print(f"action={action.kind.value}:{action.label} score={best.score:.2f}")
            if args.focus_window:
                _focus_profile_window(profile)
                time.sleep(0.10)
            backend = None if args.backend == "auto" else args.backend
            adapter.apply_action(action, backend=backend)
        final_state = adapter.current_state()
        print(f"bootstrap_timeout screen={final_state.screen.value} floor={final_state.floor}")
        return

    if args.command == "startup-sequence-live":
        profile = _load_profile_for_live(args.profile, args)
        profile.allow_foreground_fallback = True
        profile.input_backend_name = "legacy"
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = profile
        labels = [item.strip() for item in args.labels.split(",") if item.strip()]
        definitions = []
        for label in labels:
            definition = next((item for item in profile.actions if item.label == label), None)
            if definition is None:
                raise RuntimeError(f"Action label not found in profile: {label}")
            definitions.append(definition)
        if args.focus_window:
            _focus_profile_window(profile)
            time.sleep(0.25)
        adapter.start_run(focus=False)
        for index, definition in enumerate(definitions):
            if args.focus_window:
                _focus_profile_window(profile)
                time.sleep(0.10)
            backend = None if args.backend == "auto" else args.backend
            used_backend = adapter._execute_action(definition, backend=backend)
            print(
                f"step={index} screen={definition.screen.value} action={definition.label} "
                f"used_backend={used_backend}"
            )
            time.sleep(0.20)
        state = adapter.probe_fast()
        print(f"final_screen={state.screen.value} floor={state.floor}")
        return

    if args.command == "hybrid-run-live":
        profile = _load_profile_for_live(args.profile, args)
        profile.allow_foreground_fallback = True
        logger = RunLogger(args.db)
        logger.init_db()
        adapter = WindowsStsAdapter(args.profile)
        adapter.profile = profile
        adapter.start_run(focus=False)
        if not args.skip_startup:
            reached_screen = _execute_startup_sequence(
                adapter=adapter,
                profile=profile,
                focus_window_enabled=args.focus_window,
                backend="legacy",
            )
            print(f"startup_complete_screen={reached_screen.value}")
            if args.startup_only:
                return
        logger.start_run()
        policy = HeuristicPolicy()
        for step_index in range(args.max_steps):
            state, actions = _await_actionable_state(adapter, retries=10, interval_seconds=0.35)
            print(
                f"step={step_index} screen={state.screen.value} floor={state.floor} "
                f"hp={state.hp}/{state.max_hp} gold={state.gold} energy={state.energy}"
            )
            if adapter.is_run_over():
                summary = adapter.run_summary()
                run_id = logger.finish_run(summary)
                print(
                    f"run_complete run_id={run_id} outcome={'win' if summary.won else 'loss'} "
                    f"floor={summary.floor_reached}"
                )
                return
            if not actions:
                summary = adapter.run_summary()
                run_id = logger.finish_run(summary)
                print("hybrid_error=no_actions_available")
                print(f"partial_run_id={run_id} floor={summary.floor_reached}")
                return
            evaluations = policy.evaluate_actions(state, actions)
            best = max(evaluations, key=lambda item: (item.score, -len(item.action_label)))
            action = next(action for action in actions if action.label == best.action_label)
            current_intent = policy.current_run_intent()
            if current_intent is not None:
                state = replace(state, run_intent=current_intent)
            logger.log_decision(state, action, evaluations=evaluations)
            backend = _preferred_backend_for_screen(profile, state.screen)
            if backend == "legacy" and args.focus_window:
                _focus_profile_window(profile)
                time.sleep(0.10)
            used_backend = adapter.apply_action(action, backend=backend)
            print(
                f"action={action.kind.value}:{action.label} score={best.score:.2f} "
                f"backend={used_backend}"
            )
        summary = adapter.run_summary()
        run_id = logger.finish_run(summary)
        print(f"hybrid_timeout max_steps={args.max_steps}")
        print(f"partial_run_id={run_id} floor={summary.floor_reached}")
        return

    if args.command == "analyze":
        insights = analyze_builds(args.db, min_samples=args.min_samples)
        export_report_json(insights, args.json_out)
        print(render_report(insights))
        print(f"\nJSON report written to {args.json_out}")
        return

    if args.command == "trace-run":
        run_id, trace = load_run_trace(args.db, run_id=args.run_id)
        if args.json_out is not None:
            export_run_trace_json(run_id, trace, args.json_out)
            print(f"JSON trace written to {args.json_out}")
        print(render_run_trace(run_id, trace))
        return

    if args.command == "draft-fix-request":
        run_id, trace = load_run_trace(args.db, run_id=args.run_id)
        export_fix_request_markdown(run_id, trace, args.output)
        print(build_fix_request_markdown(run_id, trace))
        print(f"markdown_written={args.output}")
        return

    if args.command == "learn-kb":
        logger = RunLogger(args.db)
        logger.init_db()
        set_active_kb_overlay_path(args.kb_overlay)
        learner = CodexKBLearner(
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            workspace_dir=_repo_root(),
            min_cases=args.min_cases,
            max_cases=args.max_cases,
            cooldown_seconds=args.cooldown_seconds,
        )
        result = learner.maybe_learn(logger)
        if result is None:
            print("kb_learning=noop pending_cases_below_threshold")
            return
        print(
            f"kb_learning=applied processed_cases={result.processed_cases} "
            f"applied_operations={result.applied_operations} ignored_operations={result.ignored_operations}"
        )
        if result.summary:
            print(f"kb_learning_summary={result.summary}")
        print(f"kb_overlay={result.overlay_path}")
        return

    raise ValueError(f"Unsupported command: {args.command}")


def _image_diff_score(before_image, after_image, focus_box: tuple[int, int, int, int] | None = None) -> float:
    before = before_image.convert("RGB")
    after = after_image.convert("RGB")
    if focus_box is not None:
        before = before.crop(focus_box)
        after = after.crop(focus_box)
    diff = ImageChops.difference(before, after)
    stat = ImageStat.Stat(diff)
    return round(sum(stat.mean) / len(stat.mean), 2)


def _image_is_blank(image) -> bool:
    grayscale = image.convert("L")
    extrema = grayscale.getextrema()
    if extrema is None:
        return True
    low, high = extrema
    return high <= 5 or (high - low) <= 3


def _point_focus_box(point: tuple[int, int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = image_size
    clamped_x = min(max(0, point[0]), max(0, width - 1))
    clamped_y = min(max(0, point[1]), max(0, height - 1))
    left = max(0, clamped_x - 140)
    top = max(0, clamped_y - 80)
    right = max(left + 1, min(width, clamped_x + 140))
    bottom = max(top + 1, min(height, clamped_y + 80))
    return (left, top, right, bottom)


def _parse_point(value: str) -> tuple[int, int]:
    left, top = (part.strip() for part in value.split(",", maxsplit=1))
    return (int(left), int(top))


def _resolve_action_point(adapter: WindowsStsAdapter, screen_value: str, label: str) -> tuple[int, int]:
    for definition in adapter.profile.actions:
        if definition.screen.value == screen_value and definition.label == label:
            return definition.point
    raise RuntimeError(f"Action point not found for {screen_value}:{label}")


def _startup_definitions(profile: CalibrationProfile) -> list:
    definitions = []
    for label in profile.startup_sequence_labels:
        definition = next((item for item in profile.actions if item.label == label), None)
        if definition is None:
            raise RuntimeError(f"Startup action label not found in profile: {label}")
        definitions.append(definition)
    return definitions


def _preferred_backend_for_screen(profile: CalibrationProfile, screen: ScreenKind) -> str | None:
    return profile.scene_input_backends.get(screen.value)


def _await_actionable_state(
    adapter: WindowsStsAdapter,
    *,
    retries: int = 10,
    interval_seconds: float = 0.35,
) -> tuple[object, list[object]]:
    last_state = adapter.current_state()
    last_actions = adapter.available_actions()
    if last_actions:
        return last_state, last_actions
    for _ in range(max(0, retries)):
        time.sleep(interval_seconds)
        last_state = adapter.current_state()
        last_actions = adapter.available_actions()
        if last_actions:
            return last_state, last_actions
    return last_state, last_actions


def _execute_startup_sequence(
    *,
    adapter: WindowsStsAdapter,
    profile: CalibrationProfile,
    focus_window_enabled: bool,
    backend: str = "legacy",
) -> ScreenKind:
    definitions = _startup_definitions(profile)
    startup_screens = {definition.screen for definition in definitions}
    while True:
        state, _ = _await_actionable_state(adapter, retries=6, interval_seconds=0.25)
        if state.screen not in startup_screens:
            return state.screen
        definition = next((item for item in definitions if item.screen == state.screen), None)
        if definition is None:
            return state.screen
        if focus_window_enabled:
            _focus_profile_window(profile)
            time.sleep(0.10)
        used_backend = adapter._execute_action(definition, backend=backend)
        print(
            f"startup_screen={state.screen.value} action={definition.label} "
            f"used_backend={used_backend}"
        )
        time.sleep(0.15)


def _focus_profile_window(profile: CalibrationProfile) -> None:
    selector = WindowSelector(
        process_name=profile.target_process_name,
        title_regex=profile.target_title_regex or profile.window_title,
        class_name=profile.target_class_name,
    )
    target = WindowLocator(selector).locate()
    focus_window(hwnd=target.hwnd)


def _load_profile_for_live(path: Path, args: argparse.Namespace) -> CalibrationProfile:
    profile = CalibrationProfile.load(path)
    capture_backend = getattr(args, "capture_backend", None)
    input_backend = getattr(args, "input_backend", None)
    window_message_delivery = getattr(args, "window_message_delivery", None)
    window_message_activation = getattr(args, "window_message_activation", None)
    dry_run = bool(getattr(args, "dry_run", False))
    allow_foreground = bool(getattr(args, "allow_foreground_fallback", False))
    if capture_backend is not None:
        profile.capture_backend_name = capture_backend
    if input_backend is not None:
        profile.input_backend_name = input_backend
    if window_message_delivery is not None:
        profile.window_message_delivery = window_message_delivery
    if window_message_activation is not None:
        profile.window_message_activation = window_message_activation
    if dry_run:
        profile.dry_run = True
    if allow_foreground:
        profile.allow_foreground_fallback = True
    return profile


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_MANAGED_CONTROLS_GATED_COMMANDS = {
    "set-managed-block",
    "set-managed-gold",
    "set-managed-energy",
    "maintain-managed-energy",
    "maintain-managed-block",
    "set-managed-power",
    "alias-managed-powers",
    "install-bridge-mod",
    "bridge-apply-power",
    "bridge-add-card",
    "bridge-replace-master-deck",
    "bridge-obtain-relic",
    "bridge-set-auto-power",
    "bridge-clear-auto-power",
    "bridge-jump-map",
    "bridge-tune-card",
    "bridge-tune-relic",
}


def _require_managed_controls_license_if_needed(command: str) -> bool:
    if command not in _MANAGED_CONTROLS_GATED_COMMANDS:
        return False
    try:
        status = ensure_managed_controls_access(command, storage_dir=_repo_root() / ".managed_controls")
    except ManagedControlsLicenseError as exc:
        status = get_managed_controls_license_status(command, storage_dir=_repo_root() / ".managed_controls")
        print(f"license_error={_safe_console_text(str(exc))}")
        print(_render_managed_controls_license_status(status))
        return True
    if not status.unlocked:
        print(
            f"license_status=trial_active remaining_seconds={status.remaining_seconds} install_id={status.install_id}"
        )
    return False


def _resolve_repo_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    if path.exists():
        return path.resolve()
    candidate = (_repo_root() / path).resolve()
    return candidate if candidate.exists() else path


def _build_optional_kb_learner(args: argparse.Namespace) -> CodexKBLearner | None:
    if not getattr(args, "auto_learn_kb", False):
        return None
    return CodexKBLearner(
        model=args.kb_learn_model,
        timeout_seconds=args.kb_learn_timeout_seconds,
        workspace_dir=args.profile.parent.parent,
        min_cases=args.kb_learn_min_cases,
        max_cases=args.kb_learn_max_cases,
        cooldown_seconds=args.kb_learn_cooldown_seconds,
    )


def _run_live_loop_session(
    args: argparse.Namespace,
    logger: RunLogger,
    *,
    emit_ticks: bool,
):
    adapter = WindowsStsAdapter(args.profile)
    profile = _load_profile_for_live(args.profile, args)
    adapter.profile = profile
    provider = _build_decision_provider(
        mode=args.decision_provider,
        profile_path=args.profile,
        model=args.codex_model,
        timeout_seconds=args.codex_timeout_seconds,
    )
    runner = LiveLoopRunner(
        adapter=adapter,
        provider=provider,
        logger=logger,
        stream_path=args.stream_jsonl,
        harvest_dir=args.harvest_dir,
        harvest_confidence_threshold=args.harvest_confidence_threshold,
        learner=_build_optional_kb_learner(args),
    )
    try:
        return runner.run(
            max_steps=args.max_steps,
            max_seconds=args.max_seconds,
            emit=(lambda tick: print(_render_live_loop_tick(tick), end="\n\n")) if emit_ticks else None,
        )
    finally:
        adapter.close()


def _print_live_loop_result(result, *, stream_jsonl: Path | None) -> None:
    print(
        f"loop_status={result.status} run_id={result.run_id} "
        f"finished={str(result.finished).lower()} steps={result.steps} "
        f"screen={result.screen} floor={result.floor}"
    )
    if stream_jsonl is not None:
        print(f"stream_jsonl={stream_jsonl}")


def _append_plain_jsonl(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True))
        handle.write("\n")


def _run_live_marathon(args: argparse.Namespace, logger: RunLogger) -> None:
    target_runs = max(1, int(args.runs))
    completed_runs = 0
    session_index = 0
    partial_sessions = 0
    while completed_runs < target_runs:
        session_index += 1
        started_at = time.time()
        try:
            result = _run_live_loop_session(args, logger, emit_ticks=not args.no_tick_log)
            if result.finished:
                completed_runs += 1
            else:
                partial_sessions += 1
            _append_plain_jsonl(
                args.summary_jsonl,
                {
                    "session_index": session_index,
                    "completed_runs": completed_runs,
                    "target_runs": target_runs,
                    "run_id": result.run_id,
                    "status": result.status,
                    "finished": result.finished,
                    "steps": result.steps,
                    "screen": result.screen,
                    "floor": result.floor,
                    "duration_seconds": round(time.time() - started_at, 3),
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
            print(
                f"marathon_session={session_index} run_id={result.run_id} "
                f"loop_status={result.status} finished={str(result.finished).lower()} "
                f"completed_runs={completed_runs}/{target_runs} partial_sessions={partial_sessions} "
                f"steps={result.steps} screen={result.screen} floor={result.floor}"
            )
        except Exception as exc:
            partial_sessions += 1
            _append_plain_jsonl(
                args.summary_jsonl,
                {
                    "session_index": session_index,
                    "completed_runs": completed_runs,
                    "target_runs": target_runs,
                    "status": "exception",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "duration_seconds": round(time.time() - started_at, 3),
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
            print(
                f"marathon_session={session_index} loop_status=exception "
                f"error={type(exc).__name__}:{exc} completed_runs={completed_runs}/{target_runs} "
                f"partial_sessions={partial_sessions}"
            )
        if completed_runs >= target_runs:
            break
        time.sleep(max(0.0, args.between_sessions_seconds))
    print(
        f"marathon_complete completed_runs={completed_runs}/{target_runs} "
        f"sessions={session_index} partial_sessions={partial_sessions}"
    )
    if args.stream_jsonl is not None:
        print(f"stream_jsonl={args.stream_jsonl}")
    if args.summary_jsonl is not None:
        print(f"summary_jsonl={args.summary_jsonl}")


def _build_decision_provider(
    *,
    mode: str,
    profile_path: Path,
    model: str,
    timeout_seconds: float,
):
    heuristic = HeuristicDecisionProvider(HeuristicPolicy())
    progress = lambda message: print(f"[Phase]  {message}", flush=True)
    if mode == "heuristic":
        return heuristic
    if mode == "codex":
        return CodexDecisionProvider(
            fallback=heuristic,
            model=model,
            timeout_seconds=timeout_seconds,
            workspace_dir=profile_path.parent.parent,
            strategic_screens=set(screen.value for screen in ScreenKind),
            progress_callback=progress,
        )
    return CodexDecisionProvider(
        fallback=heuristic,
        model=model,
        timeout_seconds=timeout_seconds,
        workspace_dir=profile_path.parent.parent,
        progress_callback=progress,
    )


def _render_live_loop_tick(tick) -> str:
    reason_text = _compact_live_reason(tick.reasoning)
    warning_lines = _live_tick_warnings(tick)
    separator = f"{'=' * 12} Tick {tick.step_index:03d} {'=' * 12}"
    state_line = (
        f"[State]   step={tick.step_index} screen={tick.screen} floor={tick.floor} "
        f"hp={tick.hp}/{tick.max_hp} energy={tick.energy}/{tick.max_energy or tick.energy} "
        f"block={tick.block} gold={tick.gold}"
    )
    if tick.state_source:
        state_line = f"{state_line} source={tick.state_source}"
    lines = [
        separator,
        state_line,
        f"[Action]  {tick.action_label}  provider={tick.provider_name}  verify={tick.verification_status}",
    ]
    metric_source_text = _format_live_metric_sources(tick.state_metric_sources)
    if metric_source_text:
        lines.append(f"[Source]  {metric_source_text}")
    if reason_text:
        lines.append(f"[Reason]  {reason_text}")
    if tick.fallback_note:
        lines.append(f"[Fallback] {tick.fallback_note}")
    expected = tick.expected_outcome
    if expected is not None and (expected.next_screen or expected.change_summary):
        lines.append(
            f"[Expect]  next={expected.next_screen or '-'}  change={expected.change_summary or '-'}"
        )
    observed = tick.observed_outcome
    if observed is not None:
        observe_line = (
            f"[Observe] screen={observed.screen} hp={observed.hp}/{observed.max_hp} "
            f"energy={observed.energy}/{observed.max_energy or observed.energy} "
            f"block={observed.block} gold={observed.gold} floor={observed.floor}"
        )
        if observed.state_source:
            observe_line = f"{observe_line} source={observed.state_source}"
        lines.append(observe_line)
        observed_metric_text = _format_live_metric_sources(observed.metric_sources)
        if observed_metric_text:
            lines.append(f"[ObserveSrc] {observed_metric_text}")
        if observed.note:
            lines.append(f"[ObserveNote] {observed.note}")
    if tick.phase_timings_ms:
        timings = " ".join(f"{name}={value}ms" for name, value in tick.phase_timings_ms.items())
        lines.append(f"[Timing]  {timings}")
    lines.extend(warning_lines)
    return "\n".join(lines)


def _compact_live_reason(reasoning: str, *, max_length: int = 180) -> str:
    text = " ".join(str(reasoning or "").split()).strip()
    if not text:
        return ""
    if " because " in text:
        text = text.split(" because ", 1)[1].strip()
    elif " -> " in text:
        text = text.split(" -> ", 1)[1].strip()
    if len(text) > max_length:
        return f"{text[: max_length - 3].rstrip()}..."
    return text


def _live_tick_warnings(tick) -> list[str]:
    warnings: list[str] = []
    max_energy = tick.max_energy or tick.energy
    if tick.screen == ScreenKind.BATTLE.value and (
        (tick.max_energy <= 0 and tick.energy > 5)
        or tick.energy > max(5, max_energy)
        or max_energy > 10
    ):
        warnings.append("[Warn]    suspicious battle energy reading; metrics may be stale")
    observed = tick.observed_outcome
    observed_max = (observed.max_energy if observed is not None else 0) or (observed.energy if observed is not None else 0)
    if observed is not None and observed.screen == ScreenKind.BATTLE and (
        (observed.max_energy <= 0 and observed.energy > 5)
        or observed.energy > max(5, observed_max)
        or observed_max > 10
    ):
        warnings.append("[Warn]    observed battle energy looks invalid; verify card/end-turn logic")
    if tick.verification_status in {"partial", "unknown", "mismatch"}:
        warnings.append("[Warn]    expected and observed state did not fully align")
    return warnings


def _format_live_metric_sources(metric_sources: object) -> str:
    if not isinstance(metric_sources, dict):
        return ""
    parts = [
        f"{name}:{source}"
        for name, source in metric_sources.items()
        if isinstance(source, str) and source and source != "ocr"
    ]
    if not parts:
        return ""
    return " ".join(parts[:6])


def _render_capability_report(report) -> str:
    lines = [
        f"hwnd={report.hwnd} title={report.title!r} class={report.class_name!r} pid={report.pid} process={report.process_name}",
        f"capture_backend={report.selected_capture_backend} input_backend={report.selected_input_backend}",
        f"background_capture_supported={report.background_capture_supported}",
        f"background_input_supported={report.background_input_supported}",
        f"foreground_only_fallback_available={report.foreground_only_fallback_available}",
        f"dpi={report.dpi} scale={report.scale:.2f} client_size={report.client_size[0]}x{report.client_size[1]} dry_run={report.dry_run}",
    ]
    if report.extra:
        for key, value in report.extra.items():
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def _safe_console_text(value: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        value.encode(encoding)
        return value
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")


def _render_managed_controls_license_status(status) -> str:
    mode = "unlimited" if status.unlocked else "trial"
    return (
        f"license_mode={mode} can_use={status.can_use} expired={status.expired} "
        f"remaining_seconds={status.remaining_seconds} install_id={status.install_id} "
        f"message={_safe_console_text(status.message)}"
    )


def _run_bg_game_input_probe(
    *,
    profile: CalibrationProfile,
    key: str,
    point: str | None,
    label: str | None,
    double_click: bool,
    sleep_seconds: float,
    save_before: Path | None = None,
    save_after: Path | None = None,
) -> dict[str, object]:
    runtime = create_runtime(profile)
    try:
        report = runtime.capability_report()
        before_title = foreground_window_title()
        before_cursor = cursor_position()
        before_frame = None
        before_error = None
        try:
            before_frame = runtime.capture_backend.read_latest_frame(timeout_ms=250)
        except Exception as exc:
            before_error = str(exc)
        if before_frame is not None and save_before is not None:
            save_before.parent.mkdir(parents=True, exist_ok=True)
            before_frame.save(save_before)

        if label is not None:
            definition = next((item for item in profile.actions if item.label == label), None)
            if definition is None:
                raise RuntimeError(f"Action label not found in profile: {label}")
            resolved_point = runtime.transform.reference_to_client(definition.point)
            runtime.input_backend.click(resolved_point[0], resolved_point[1], double=double_click)
            action_desc = f"click:{label}@{resolved_point[0]},{resolved_point[1]}"
        elif point is not None:
            resolved_point = runtime.transform.reference_to_client(_parse_point(point))
            runtime.input_backend.click(resolved_point[0], resolved_point[1], double=double_click)
            action_desc = f"click:{resolved_point[0]},{resolved_point[1]}"
        else:
            runtime.input_backend.key_press(key, hold_ms=80)
            action_desc = f"key:{key}"

        input_diag = runtime.input_backend.diagnostics()
        time.sleep(max(0.1, sleep_seconds))
        after_title = foreground_window_title()
        after_cursor = cursor_position()
        after_frame = None
        after_error = None
        try:
            after_frame = runtime.capture_backend.read_latest_frame(timeout_ms=250)
        except Exception as exc:
            after_error = str(exc)
        if after_frame is not None and save_after is not None:
            save_after.parent.mkdir(parents=True, exist_ok=True)
            after_frame.save(save_after)

        result: dict[str, object] = {
            "report": report,
            "input": action_desc,
            "foreground_before": before_title,
            "foreground_after": after_title,
            "cursor_before": before_cursor,
            "cursor_after": after_cursor,
            "foreground_unchanged": before_title == after_title,
            "cursor_unchanged": before_cursor == after_cursor,
            "input_extra": input_diag.extra,
            "before_capture_ok": before_frame is not None,
            "after_capture_ok": after_frame is not None,
            "before_capture_error": before_error,
            "after_capture_error": after_error,
            "before_blank": _image_is_blank(before_frame) if before_frame is not None else None,
            "after_blank": _image_is_blank(after_frame) if after_frame is not None else None,
        }
        if before_frame is not None and after_frame is not None:
            frame_diff = _image_diff_score(before_frame, after_frame)
            result["frame_diff"] = frame_diff
            result["observed_effect"] = frame_diff >= 3.0
        else:
            result["frame_diff"] = None
            result["observed_effect"] = None
        return result
    finally:
        runtime.close()


def _render_probe_result_lines(result: dict[str, object]) -> list[str]:
    lines = [
        f"input={result['input']}",
        f"foreground_before={result['foreground_before']}",
        f"foreground_after={result['foreground_after']}",
        f"cursor_before={result['cursor_before'][0]},{result['cursor_before'][1]}",
        f"cursor_after={result['cursor_after'][0]},{result['cursor_after'][1]}",
        f"foreground_unchanged={result['foreground_unchanged']}",
        f"cursor_unchanged={result['cursor_unchanged']}",
    ]
    for key, value in dict(result["input_extra"]).items():
        lines.append(f"input_{key}={value}")
    lines.append(f"before_capture={'ok' if result['before_capture_ok'] else 'error'}")
    if result.get("before_capture_error") is not None:
        lines.append(f"before_capture_error={result['before_capture_error']}")
    if result.get("before_blank") is not None:
        lines.append(f"before_blank={result['before_blank']}")
    lines.append(f"after_capture={'ok' if result['after_capture_ok'] else 'error'}")
    if result.get("after_capture_error") is not None:
        lines.append(f"after_capture_error={result['after_capture_error']}")
    if result.get("after_blank") is not None:
        lines.append(f"after_blank={result['after_blank']}")
    if result.get("frame_diff") is not None:
        lines.append(f"frame_diff={result['frame_diff']:.2f}")
        lines.append(f"observed_effect={result['observed_effect']}")
        if not result["observed_effect"]:
            lines.append("observed_effect_reason=no_meaningful_visual_change_detected")
    return lines


def _render_metric_source_lines(sources: dict[str, str]) -> list[str]:
    return [f"metric_source={name}:{source}" for name, source in sorted(sources.items())]


def _render_memory_probe_lines(screen: str, payload: dict[str, object]) -> list[str]:
    lines = [f"screen={screen}"]
    if "module" in payload:
        lines.append(f"memory_module={payload['module']}")
    if "cached" in payload:
        lines.append(f"memory_cached={payload['cached']}")
    for name, value in sorted(dict(payload.get("values", {})).items()):
        lines.append(f"memory_value={name}:{value}")
    for name, result in sorted(dict(payload.get("fields", {})).items()):
        lines.append(
            f"memory_field={name} source={result.get('source')} value={result.get('value')} "
            f"error={result.get('error')}"
        )
    for power in payload.get("player_powers", []):
        if isinstance(power, dict):
            lines.append(
                f"player_power={power.get('type')} amount={power.get('amount')} address={power.get('address')}"
            )
    for enemy in payload.get("enemies", []):
        if not isinstance(enemy, dict):
            continue
        lines.append(
            f"enemy={enemy.get('address')} hp={enemy.get('current_hp')}/{enemy.get('max_hp')} "
            f"block={enemy.get('block')}"
        )
        for power in enemy.get("powers", []):
            if isinstance(power, dict):
                lines.append(
                    f"enemy_power={enemy.get('address')} type={power.get('type')} "
                    f"amount={power.get('amount')} address={power.get('address')}"
                )
    for error in payload.get("errors", []):
        lines.append(f"memory_error={error}")
    return lines


def _render_managed_probe_lines(summary) -> list[str]:
    lines: list[str] = [f"player_block={getattr(summary, 'block', '?')}"]
    for power in getattr(summary, "player_powers", []):
        lines.append(
            f"player_power={power.type_name} amount={power.amount} address={power.address}"
        )
    for enemy in getattr(summary, "enemies", []):
        lines.append(f"enemy={enemy.address} hp={enemy.current_hp}/{enemy.max_hp} block={enemy.block}")
        for power in getattr(enemy, "powers", []):
            lines.append(
                f"enemy_power={enemy.address} type={power.type_name} amount={power.amount} address={power.address}"
            )
    return lines


def _render_window_inspection(hwnd: int, *, max_depth: int) -> str:
    lines = [f"inspect_hwnd={hwnd}"]
    try:
        state = gui_thread_state(hwnd)
        lines.extend(
            [
                f"gui_flags=0x{state.flags:04x}",
                f"gui_active_hwnd={state.active_hwnd}",
                f"gui_focus_hwnd={state.focus_hwnd}",
                f"gui_capture_hwnd={state.capture_hwnd}",
                f"gui_menu_owner_hwnd={state.menu_owner_hwnd}",
                f"gui_move_size_hwnd={state.move_size_hwnd}",
                f"gui_caret_hwnd={state.caret_hwnd}",
            ]
        )
    except Exception as exc:
        lines.append(f"gui_thread_info_error={exc}")
    children = enumerate_child_windows(hwnd, max_depth=max_depth)
    lines.append(f"child_count={len(children)}")
    for child in children:
        rect = child.client_rect
        indent = "  " * max(0, child.depth - 1)
        lines.append(
            f"{indent}- hwnd={child.hwnd} class={child.class_name!r} title={child.title!r} "
            f"visible={child.visible} enabled={child.enabled} "
            f"client_rect=({rect.left},{rect.top},{rect.width},{rect.height})"
        )
    return "\n".join(lines)


def _run_bg_input_smoke(*, timeout_ms: int) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        ready_file = root / "ready.json"
        events_file = root / "events.jsonl"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sts_bot.test_window",
                "--ready-file",
                str(ready_file),
                "--events-file",
                str(events_file),
                "--timeout-ms",
                str(timeout_ms),
            ],
            cwd=Path.cwd(),
        )
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not ready_file.exists():
                time.sleep(0.1)
            if not ready_file.exists():
                raise RuntimeError("Input smoke test window did not become ready.")
            payload = json.loads(ready_file.read_text(encoding="utf-8"))
            selector = WindowSelector(title_regex=payload["title"])
            target = WindowLocator(selector).locate()
            backend = WindowMessageInputBackend(dry_run=False)
            backend.open(target)
            before_title = foreground_window_title()
            before_cursor = cursor_position()
            client_center = (target.client_rect.width // 2, target.client_rect.height // 2)
            backend.click(*client_center)
            backend.text("a")
            time.sleep(0.4)
            after_title = foreground_window_title()
            after_cursor = cursor_position()
            rows = [
                json.loads(line)
                for line in events_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            saw_click = any(row.get("event") == "click" for row in rows)
            saw_key = any(row.get("event") == "key" and row.get("char", "").lower() == "a" for row in rows)
            print(f"target_hwnd={target.hwnd} title={target.title!r}")
            print("backend=window_messages")
            print(f"foreground_before={before_title}")
            print(f"foreground_after={after_title}")
            print(f"cursor_before={before_cursor[0]},{before_cursor[1]}")
            print(f"cursor_after={after_cursor[0]},{after_cursor[1]}")
            print(f"click_received={saw_click}")
            print(f"key_received={saw_key}")
            print(f"foreground_unchanged={before_title == after_title}")
            print(f"cursor_unchanged={before_cursor == after_cursor}")
            if not saw_click or not saw_key:
                print(f"events={rows}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _launch_managed_control_ui(profile_path: Path) -> None:
    from sts_bot.control_ui import launch_managed_control_ui

    launch_managed_control_ui(profile_path=profile_path)


if __name__ == "__main__":
    main()
