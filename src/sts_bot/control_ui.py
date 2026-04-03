from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from sts_bot.config import CalibrationProfile
from sts_bot.dev_console import enable_full_console, run_dev_console_command
from sts_bot.game_catalog import CatalogBundle, CatalogEntry, filter_catalog, load_catalog_bundle
from sts_bot.io_runtime import create_runtime
from sts_bot.managed_probe import (
    alias_managed_powers,
    ManagedProbeError,
    probe_managed_numeric,
    set_managed_player_block,
    set_managed_player_energy,
    set_managed_power_amount,
)
from sts_bot.mod_bridge import (
    install_bridge_mod,
    send_bridge_add_card,
    send_bridge_obtain_relic,
    send_bridge_apply_power,
    send_bridge_replace_master_deck,
)


class ManagedControlWindow:
    def __init__(self, *, profile_path: Path) -> None:
        self._repo_root = Path(__file__).resolve().parents[2]
        self._profile_path = self._resolve_profile_path(profile_path)
        self._root = tk.Tk()
        self._root.title("STS Managed Controls")
        self._root.geometry("1100x980")
        self._queue: queue.Queue[str] = queue.Queue()
        self._block_maintain_stop = threading.Event()
        self._block_maintain_thread: threading.Thread | None = None
        self._energy_maintain_stop = threading.Event()
        self._energy_maintain_thread: threading.Thread | None = None

        self.profile_var = tk.StringVar(value=str(self._profile_path))
        self.block_var = tk.StringVar(value="100")
        self.block_interval_var = tk.StringVar(value="0.2")
        self.energy_var = tk.StringVar(value="100")
        self.max_energy_var = tk.StringVar(value="100")
        self.energy_interval_var = tk.StringVar(value="0.15")
        self.power_target_var = tk.StringVar(value="player")
        self.power_type_var = tk.StringVar(value="StrengthPower")
        self.power_value_var = tk.StringVar(value="100")
        self.card_type_var = tk.StringVar(value="Whirlwind")
        self.card_count_var = tk.StringVar(value="1")
        self.relic_type_var = tk.StringVar(value="Anchor")
        self.relic_count_var = tk.StringVar(value="1")
        self.alias_source_var = tk.StringVar(value="enemy")
        self.alias_dest_var = tk.StringVar(value="player")
        self.catalog_query_var = tk.StringVar(value="")
        self.catalog_status_var = tk.StringVar(value="Catalog: not loaded")
        self.console_backend_var = tk.StringVar(value="sendinput_scan")
        self.console_open_key_var = tk.StringVar(value="backtick")
        self.console_typing_interval_var = tk.StringVar(value="0.01")
        self.console_command_var = tk.StringVar(value="help power")
        self.console_power_template_var = tk.StringVar(value="power {target} {power_type} {amount}")
        self.console_power_target_var = tk.StringVar(value="player")
        self.console_power_type_var = tk.StringVar(value="StrengthPower")
        self.console_power_amount_var = tk.StringVar(value="100")
        self.console_leave_open_var = tk.BooleanVar(value=False)
        self._catalog_bundle = CatalogBundle(cards=(), powers=(), relics=())
        self._card_view: list[CatalogEntry] = []
        self._power_view: list[CatalogEntry] = []
        self._relic_view: list[CatalogEntry] = []

        self._build()
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.after(100, self._drain_logs)
        self._run_async(self._refresh_catalogs)

    def run(self) -> None:
        self._root.mainloop()

    def _build(self) -> None:
        frame = ttk.Frame(self._root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.columnconfigure(5, weight=1)

        ttk.Label(frame, text="Profile").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.profile_var, width=90).grid(row=0, column=1, columnspan=6, sticky="ew", pady=4)

        ttk.Button(frame, text="Probe", command=lambda: self._run_async(self._probe)).grid(row=1, column=0, sticky="ew", pady=4)

        ttk.Label(frame, text="Block").grid(row=2, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.block_var, width=12).grid(row=2, column=1, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Set Block", command=lambda: self._run_async(self._set_block)).grid(row=2, column=2, sticky="ew", pady=4)
        ttk.Label(frame, text="Maintain Interval").grid(row=2, column=3, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.block_interval_var, width=10).grid(row=2, column=4, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Start Maintain Block", command=self._start_maintain_block).grid(row=2, column=5, sticky="ew", pady=4)
        ttk.Button(frame, text="Stop Maintain Block", command=self._stop_maintain_block).grid(row=2, column=6, sticky="ew", pady=4)

        ttk.Label(frame, text="Energy").grid(row=3, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.energy_var, width=12).grid(row=3, column=1, sticky=tk.W, pady=4)
        ttk.Label(frame, text="Max").grid(row=3, column=2, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.max_energy_var, width=12).grid(row=3, column=3, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Set Energy", command=lambda: self._run_async(self._set_energy)).grid(row=3, column=4, sticky="ew", pady=4)
        ttk.Label(frame, text="Maintain Interval").grid(row=3, column=5, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.energy_interval_var, width=10).grid(row=3, column=6, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Start Maintain Energy", command=self._start_maintain_energy).grid(row=4, column=5, sticky="ew", pady=4)
        ttk.Button(frame, text="Stop Maintain Energy", command=self._stop_maintain_energy).grid(row=4, column=6, sticky="ew", pady=4)

        ttk.Label(frame, text="Alias Source").grid(row=5, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(frame, textvariable=self.alias_source_var, values=("player", "enemy"), width=10, state="readonly").grid(row=5, column=1, sticky=tk.W, pady=4)
        ttk.Label(frame, text="Alias Dest").grid(row=5, column=2, sticky=tk.E, pady=4)
        ttk.Combobox(frame, textvariable=self.alias_dest_var, values=("player", "enemy"), width=10, state="readonly").grid(row=5, column=3, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Alias Powers", command=lambda: self._run_async(self._alias_powers)).grid(row=5, column=4, sticky="ew", pady=4)

        ttk.Label(frame, text="Power Target").grid(row=6, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(frame, textvariable=self.power_target_var, values=("player", "enemy"), width=10, state="readonly").grid(row=6, column=1, sticky=tk.W, pady=4)
        ttk.Label(frame, text="Power Type").grid(row=6, column=2, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.power_type_var, width=24).grid(row=6, column=3, sticky="ew", pady=4)
        ttk.Label(frame, text="Value").grid(row=6, column=4, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.power_value_var, width=12).grid(row=6, column=5, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Set Existing Power", command=lambda: self._run_async(self._set_power)).grid(row=6, column=6, sticky="ew", pady=4)

        ttk.Button(frame, text="Install Bridge Mod", command=lambda: self._run_async(self._install_bridge_mod)).grid(row=7, column=4, sticky="ew", pady=4)
        ttk.Button(frame, text="Grant Strength (Bridge)", command=lambda: self._run_async(self._grant_strength)).grid(row=7, column=5, sticky="ew", pady=4)
        ttk.Button(frame, text="Apply Power (Bridge)", command=lambda: self._run_async(self._apply_power_bridge)).grid(row=7, column=6, sticky="ew", pady=4)

        ttk.Label(frame, text="Card Type").grid(row=8, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.card_type_var, width=24).grid(row=8, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Count").grid(row=8, column=2, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.card_count_var, width=12).grid(row=8, column=3, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Add Card To Deck", command=lambda: self._run_async(self._add_card_to_deck_bridge)).grid(row=8, column=4, sticky="ew", pady=4)
        ttk.Button(frame, text="Add Card To Hand", command=lambda: self._run_async(self._add_card_to_hand_bridge)).grid(row=8, column=5, sticky="ew", pady=4)
        ttk.Button(frame, text="Replace Deck", command=lambda: self._run_async(self._replace_master_deck_bridge)).grid(row=8, column=6, sticky="ew", pady=4)

        ttk.Label(frame, text="Relic Type").grid(row=9, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.relic_type_var, width=24).grid(row=9, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="Relic Count").grid(row=9, column=2, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.relic_count_var, width=12).grid(row=9, column=3, sticky=tk.W, pady=4)
        ttk.Button(frame, text="Obtain Relic (Bridge)", command=lambda: self._run_async(self._obtain_relic_bridge)).grid(row=9, column=4, columnspan=3, sticky="ew", pady=4)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=10, column=0, columnspan=7, sticky="ew", pady=8)

        ttk.Label(frame, text="Catalog Search").grid(row=11, column=0, sticky=tk.W, pady=4)
        search_entry = ttk.Entry(frame, textvariable=self.catalog_query_var, width=32)
        search_entry.grid(row=11, column=1, columnspan=2, sticky="ew", pady=4)
        search_entry.bind("<KeyRelease>", lambda _event: self._render_catalogs())
        ttk.Button(frame, text="Refresh Catalogs", command=lambda: self._run_async(self._refresh_catalogs)).grid(row=11, column=3, sticky="ew", pady=4)
        ttk.Label(frame, textvariable=self.catalog_status_var).grid(row=11, column=4, columnspan=3, sticky=tk.W, pady=4)

        self.catalog_notebook = ttk.Notebook(frame)
        self.catalog_notebook.grid(row=12, column=0, columnspan=7, sticky="nsew", pady=(4, 8))
        frame.rowconfigure(12, weight=1)
        self.card_listbox = self._build_catalog_tab(self.catalog_notebook, "Cards", self._on_card_selected)
        self.power_listbox = self._build_catalog_tab(self.catalog_notebook, "Powers", self._on_power_selected)
        self.relic_listbox = self._build_catalog_tab(self.catalog_notebook, "Relics", self._on_relic_selected)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=13, column=0, columnspan=7, sticky="ew", pady=8)

        ttk.Label(frame, text="Dev Console Command").grid(row=14, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.console_command_var, width=72).grid(row=14, column=1, columnspan=4, sticky="ew", pady=4)
        ttk.Button(frame, text="Run Console Command", command=lambda: self._run_async(self._run_console_command)).grid(row=14, column=5, sticky="ew", pady=4)
        ttk.Button(frame, text="Enable Full Console", command=lambda: self._run_async(self._enable_full_console)).grid(row=14, column=6, sticky="ew", pady=4)

        ttk.Label(frame, text="Console Backend").grid(row=15, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(frame, textvariable=self.console_backend_var, values=("sendinput_scan", "sendinput", "directinput", "legacy_event"), width=16, state="readonly").grid(row=15, column=1, sticky=tk.W, pady=4)
        ttk.Label(frame, text="Open Key").grid(row=15, column=2, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.console_open_key_var, width=12).grid(row=15, column=3, sticky=tk.W, pady=4)
        ttk.Label(frame, text="Typing Interval").grid(row=15, column=4, sticky=tk.E, pady=4)
        ttk.Entry(frame, textvariable=self.console_typing_interval_var, width=12).grid(row=15, column=5, sticky=tk.W, pady=4)
        ttk.Checkbutton(frame, text="Leave Console Open", variable=self.console_leave_open_var).grid(row=15, column=6, sticky=tk.W, pady=4)

        ttk.Label(frame, text="Power Template").grid(row=16, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.console_power_template_var, width=40).grid(row=16, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Combobox(frame, textvariable=self.console_power_target_var, values=("player", "enemy"), width=10, state="readonly").grid(row=16, column=4, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=self.console_power_type_var, width=20).grid(row=16, column=5, sticky="ew", pady=4)
        ttk.Entry(frame, textvariable=self.console_power_amount_var, width=12).grid(row=16, column=6, sticky="ew", pady=4)
        ttk.Button(frame, text="Fill Power Command", command=self._fill_power_command).grid(row=17, column=5, sticky="ew", pady=4)

        ttk.Button(frame, text="Help", command=self._preset_help).grid(row=17, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="Help Power", command=self._preset_help_power).grid(row=17, column=2, sticky="ew", pady=4)
        ttk.Button(frame, text="Help Block", command=self._preset_help_block).grid(row=17, column=3, sticky="ew", pady=4)
        ttk.Button(frame, text="Help Energy", command=self._preset_help_energy).grid(row=17, column=4, sticky="ew", pady=4)

        self.log = tk.Text(frame, wrap=tk.WORD, height=22)
        self.log.grid(row=18, column=0, columnspan=7, sticky="nsew", pady=(12, 0))
        frame.rowconfigure(18, weight=1)

    def _build_catalog_tab(self, notebook: ttk.Notebook, title: str, on_select) -> tk.Listbox:
        container = ttk.Frame(notebook, padding=4)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        listbox = tk.Listbox(container, height=10, exportselection=False)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        listbox.bind("<<ListboxSelect>>", on_select)
        notebook.add(container, text=title)
        return listbox

    def _profile(self) -> CalibrationProfile:
        return CalibrationProfile.load(self._resolve_profile_path(Path(self.profile_var.get())))

    def _resolve_profile_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        if path.exists():
            return path.resolve()
        candidate = (self._repo_root / path).resolve()
        return candidate if candidate.exists() else path

    def _with_pid(self, action) -> None:
        profile = self._profile()
        runtime = create_runtime(profile)
        try:
            action(runtime.target.pid)
        finally:
            runtime.close()

    def _run_async(self, fn) -> None:
        thread = threading.Thread(target=self._safe_run, args=(fn,), daemon=True)
        thread.start()

    def _safe_run(self, fn) -> None:
        try:
            fn()
        except Exception as exc:  # pragma: no cover - UI path
            self._emit(f"ERROR: {exc}")

    def _refresh_catalogs(self) -> None:
        bundle = load_catalog_bundle(workspace_dir=self._repo_root)
        self._root.after(0, lambda: self._apply_catalog_bundle(bundle))

    def _apply_catalog_bundle(self, bundle: CatalogBundle) -> None:
        self._catalog_bundle = bundle
        self.catalog_status_var.set(
            f"Catalog: {len(bundle.cards)} cards, {len(bundle.powers)} powers, {len(bundle.relics)} relics"
        )
        self._render_catalogs()
        self._emit("catalog_refresh completed")

    def _render_catalogs(self) -> None:
        query = self.catalog_query_var.get().strip()
        self._card_view = list(filter_catalog(self._catalog_bundle.cards, query))
        self._power_view = list(filter_catalog(self._catalog_bundle.powers, query))
        self._relic_view = list(filter_catalog(self._catalog_bundle.relics, query))
        self._replace_listbox_contents(self.card_listbox, self._card_view)
        self._replace_listbox_contents(self.power_listbox, self._power_view)
        self._replace_listbox_contents(self.relic_listbox, self._relic_view)

    def _replace_listbox_contents(self, listbox: tk.Listbox, entries: list[CatalogEntry]) -> None:
        listbox.delete(0, tk.END)
        for entry in entries[:500]:
            listbox.insert(tk.END, f"{entry.display_name}  [{entry.short_name}]")

    def _selected_catalog_entry(self, listbox: tk.Listbox, entries: list[CatalogEntry]) -> CatalogEntry | None:
        selection = listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if index < 0 or index >= len(entries):
            return None
        return entries[index]

    def _on_card_selected(self, _event=None) -> None:
        entry = self._selected_catalog_entry(self.card_listbox, self._card_view)
        if entry is None:
            return
        self.card_type_var.set(entry.short_name)
        self._emit(f"selected_card {entry.type_name}")

    def _on_power_selected(self, _event=None) -> None:
        entry = self._selected_catalog_entry(self.power_listbox, self._power_view)
        if entry is None:
            return
        self.power_type_var.set(entry.short_name)
        self.console_power_type_var.set(entry.short_name)
        self._emit(f"selected_power {entry.type_name}")

    def _on_relic_selected(self, _event=None) -> None:
        entry = self._selected_catalog_entry(self.relic_listbox, self._relic_view)
        if entry is None:
            return
        self.relic_type_var.set(entry.short_name)
        self._emit(f"selected_relic {entry.type_name}")

    def _probe(self) -> None:
        def action(pid: int) -> None:
            summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
            self._emit(
                f"probe floor={summary.floor} hp={summary.hp}/{summary.max_hp} "
                f"block={summary.block} energy={summary.energy}/{summary.max_energy} gold={summary.gold}"
            )
            for power in summary.player_powers:
                self._emit(f"player_power {power.type_name} amount={power.amount}")
            for enemy in summary.enemies:
                self._emit(f"enemy {enemy.address} hp={enemy.current_hp}/{enemy.max_hp} block={enemy.block}")
                for power in enemy.powers:
                    self._emit(f"enemy_power {power.type_name} amount={power.amount}")

        self._with_pid(action)

    def _set_block(self) -> None:
        value = int(self.block_var.get())

        def action(pid: int) -> None:
            result = set_managed_player_block(pid, value, workspace_dir=self._repo_root)
            self._emit(f"set_block previous={result.previous} requested={result.requested}")
            summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
            self._emit(f"verified block={summary.block}")

        self._with_pid(action)

    def _set_energy(self) -> None:
        value = int(self.energy_var.get())
        max_value_text = self.max_energy_var.get().strip()
        max_value = int(max_value_text) if max_value_text else None

        def action(pid: int) -> None:
            result = set_managed_player_energy(pid, energy=value, max_energy=max_value, workspace_dir=self._repo_root)
            self._emit(
                f"set_energy previous={result.previous_energy}/{result.previous_max_energy} "
                f"requested={result.requested_energy}/{result.requested_max_energy}"
            )
            summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
            self._emit(f"verified energy={summary.energy}/{summary.max_energy}")

        self._with_pid(action)

    def _alias_powers(self) -> None:
        source = self.alias_source_var.get()
        dest = self.alias_dest_var.get()

        def action(pid: int) -> None:
            result = alias_managed_powers(pid, source=source, dest=dest, workspace_dir=self._repo_root)
            self._emit(f"alias_powers source={result.source} dest={result.dest} requested={result.requested}")
            summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
            for power in summary.player_powers:
                self._emit(f"player_power {power.type_name} amount={power.amount}")

        self._with_pid(action)

    def _set_power(self) -> None:
        target = self.power_target_var.get()
        power_type = self.power_type_var.get().strip()
        value = int(self.power_value_var.get())

        def action(pid: int) -> None:
            try:
                result = set_managed_power_amount(pid, target=target, power_type=power_type, value=value, workspace_dir=self._repo_root)
                self._emit(
                    f"set_existing_power target={result.target} type={result.power_type} "
                    f"previous={result.previous} requested={result.requested}"
                )
            except ManagedProbeError as exc:
                if "power not found:" not in str(exc):
                    raise
                bridge = send_bridge_apply_power(power_type=power_type, amount=value, target=target)
                self._emit(f"set_existing_power unavailable; bridge_apply request={bridge.request} response={bridge.response}")
                return
            summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
            for power in summary.player_powers:
                self._emit(f"player_power {power.type_name} amount={power.amount}")
            for enemy in summary.enemies:
                for power in enemy.powers:
                    self._emit(f"enemy_power {power.type_name} amount={power.amount}")

        self._with_pid(action)

    def _grant_strength(self) -> None:
        value = int(self.power_value_var.get())

        def action(pid: int) -> None:
            result = send_bridge_apply_power(power_type="StrengthPower", amount=value, target="player")
            self._emit(f"grant_strength_bridge request={result.request} response={result.response}")

        self._with_pid(action)

    def _start_maintain_block(self) -> None:
        if self._block_maintain_thread is not None and self._block_maintain_thread.is_alive():
            self._emit("maintain_block already running")
            return
        self._block_maintain_stop.clear()
        self._block_maintain_thread = threading.Thread(target=self._maintain_block_loop, daemon=True)
        self._block_maintain_thread.start()
        self._emit("maintain_block started")

    def _stop_maintain_block(self) -> None:
        self._block_maintain_stop.set()
        self._emit("maintain_block stop requested")

    def _maintain_block_loop(self) -> None:
        value = int(self.block_var.get())
        interval = float(self.block_interval_var.get())
        while not self._block_maintain_stop.is_set():
            try:
                def action(pid: int) -> None:
                    result = set_managed_player_block(pid, value, workspace_dir=self._repo_root)
                    summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
                    self._emit(
                        f"maintain_block previous={result.previous} verified={summary.block} "
                        f"hp={summary.hp}/{summary.max_hp} energy={summary.energy}/{summary.max_energy}"
                    )

                self._with_pid(action)
            except Exception as exc:  # pragma: no cover - UI path
                self._emit(f"maintain_block ERROR: {exc}")
            time.sleep(max(0.05, interval))

    def _start_maintain_energy(self) -> None:
        if self._energy_maintain_thread is not None and self._energy_maintain_thread.is_alive():
            self._emit("maintain_energy already running")
            return
        self._energy_maintain_stop.clear()
        self._energy_maintain_thread = threading.Thread(target=self._maintain_energy_loop, daemon=True)
        self._energy_maintain_thread.start()
        self._emit("maintain_energy started")

    def _stop_maintain_energy(self) -> None:
        self._energy_maintain_stop.set()
        self._emit("maintain_energy stop requested")

    def _maintain_energy_loop(self) -> None:
        value = int(self.energy_var.get())
        max_value_text = self.max_energy_var.get().strip()
        max_value = int(max_value_text) if max_value_text else None
        interval = float(self.energy_interval_var.get())
        while not self._energy_maintain_stop.is_set():
            try:
                def action(pid: int) -> None:
                    result = set_managed_player_energy(pid, energy=value, max_energy=max_value, workspace_dir=self._repo_root)
                    summary = probe_managed_numeric(pid, workspace_dir=self._repo_root)
                    self._emit(
                        f"maintain_energy previous={result.previous_energy}/{result.previous_max_energy} "
                        f"verified={summary.energy}/{summary.max_energy} hp={summary.hp}/{summary.max_hp} block={summary.block}"
                    )

                self._with_pid(action)
            except Exception as exc:  # pragma: no cover - UI path
                self._emit(f"maintain_energy ERROR: {exc}")
            time.sleep(max(0.05, interval))

    def _enable_full_console(self) -> None:
        result = enable_full_console()
        self._emit(
            f"enable_full_console updated={len(result.updated_paths)} unchanged={len(result.unchanged_paths)} root={result.searched_root}"
        )
        for path in result.updated_paths:
            self._emit(f"updated_settings {path}")
        for path in result.unchanged_paths:
            self._emit(f"unchanged_settings {path}")

    def _run_console_command(self) -> None:
        command = self.console_command_var.get().strip()
        backend = self.console_backend_var.get().strip() or "sendinput_scan"
        open_key = self.console_open_key_var.get().strip() or "backtick"
        typing_interval = float(self.console_typing_interval_var.get())
        close_console = not self.console_leave_open_var.get()
        if not command:
            self._emit("ERROR: console command is empty")
            return
        result = run_dev_console_command(
            self._profile(),
            command,
            backend=backend,
            open_key=open_key,
            typing_interval=typing_interval,
            close_console=close_console,
        )
        self._emit(
            f"console_command pid={result.pid} hwnd=0x{result.hwnd:x} backend={result.backend} "
            f"close_console={result.close_console} command={result.command}"
        )
        for path in result.settings.updated_paths:
            self._emit(f"updated_settings {path}")

    def _preset_help(self) -> None:
        self.console_command_var.set("help")

    def _preset_help_power(self) -> None:
        self.console_command_var.set("help power")

    def _preset_help_block(self) -> None:
        self.console_command_var.set("help block")

    def _preset_help_energy(self) -> None:
        self.console_command_var.set("help energy")

    def _fill_power_command(self) -> None:
        template = self.console_power_template_var.get().strip()
        if not template:
            self._emit("ERROR: power template is empty")
            return
        try:
            command = template.format(
                target=self.console_power_target_var.get().strip(),
                power_type=self.console_power_type_var.get().strip(),
                amount=self.console_power_amount_var.get().strip(),
            )
        except KeyError as exc:
            self._emit(f"ERROR: bad template placeholder {exc}")
            return
        self.console_command_var.set(command)
        self._emit(f"filled_console_command {command}")

    def _install_bridge_mod(self) -> None:
        result = install_bridge_mod(workspace_dir=self._repo_root)
        self._emit(f"bridge_install mod_dir={result.mod_dir}")
        self._emit(f"bridge_install dll={result.dll_path}")
        self._emit(f"bridge_install manifest={result.manifest_path}")
        self._emit("bridge_install note=restart the game once to load the bridge mod")

    def _apply_power_bridge(self) -> None:
        target = self.power_target_var.get().strip()
        power_type = self.power_type_var.get().strip()
        value = int(self.power_value_var.get())
        result = send_bridge_apply_power(power_type=power_type, amount=value, target=target)
        self._emit(f"bridge_power request={result.request} response={result.response}")

    def _add_card_to_deck_bridge(self) -> None:
        card_type = self.card_type_var.get().strip()
        count = int(self.card_count_var.get())
        result = send_bridge_add_card(card_type=card_type, destination="deck", count=count)
        self._emit(f"bridge_add_card request={result.request} response={result.response}")

    def _add_card_to_hand_bridge(self) -> None:
        card_type = self.card_type_var.get().strip()
        count = int(self.card_count_var.get())
        result = send_bridge_add_card(card_type=card_type, destination="hand", count=count)
        self._emit(f"bridge_add_card request={result.request} response={result.response}")

    def _replace_master_deck_bridge(self) -> None:
        card_type = self.card_type_var.get().strip()
        count_text = self.card_count_var.get().strip()
        count = int(count_text) if count_text else None
        result = send_bridge_replace_master_deck(card_type=card_type, count=count)
        self._emit(f"bridge_replace_deck request={result.request} response={result.response}")

    def _obtain_relic_bridge(self) -> None:
        relic_type = self.relic_type_var.get().strip()
        count = int(self.relic_count_var.get())
        result = send_bridge_obtain_relic(relic_type=relic_type, count=count)
        self._emit(f"bridge_obtain_relic request={result.request} response={result.response}")

    def _drain_logs(self) -> None:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            if message:
                self.log.insert(tk.END, message + "\n")
                self.log.see(tk.END)
        self._root.after(100, self._drain_logs)

    def _emit(self, message: str) -> None:
        self._queue.put(message)
        print(message, file=sys.stdout, flush=True)

    def _on_close(self) -> None:
        self._block_maintain_stop.set()
        self._energy_maintain_stop.set()
        self._root.destroy()


def launch_managed_control_ui(*, profile_path: Path) -> None:
    ManagedControlWindow(profile_path=profile_path).run()
