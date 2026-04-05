from __future__ import annotations

import json
import locale
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sts_bot.managed_probe import ManagedProbeError


BRIDGE_MOD_ID = "CodexBridge"
BRIDGE_DLL_NAME = f"{BRIDGE_MOD_ID}.dll"
BRIDGE_MANIFEST_NAME = f"{BRIDGE_MOD_ID}.json"
BRIDGE_PIPE_PATH = r"\\.\pipe\sts2_codex_bridge"


@dataclass(frozen=True)
class BridgeInstallResult:
    game_dir: str
    mod_dir: str
    dll_path: str
    manifest_path: str
    manifest: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "game_dir": self.game_dir,
            "mod_dir": self.mod_dir,
            "dll_path": self.dll_path,
            "manifest_path": self.manifest_path,
            "manifest": dict(self.manifest),
        }


@dataclass(frozen=True)
class BridgeCommandResult:
    pipe_path: str
    request: dict[str, object]
    response: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "pipe_path": self.pipe_path,
            "request": dict(self.request),
            "response": dict(self.response),
        }


def install_bridge_mod(*, game_dir: Path | None = None, workspace_dir: Path | None = None) -> BridgeInstallResult:
    workspace = workspace_dir.resolve() if workspace_dir is not None else _repo_root()
    target_game_dir = game_dir.resolve() if game_dir is not None else _default_game_dir()
    if not target_game_dir.exists():
        raise ManagedProbeError(f"game dir not found: {target_game_dir}")

    build_dir = workspace / "tmp" / "codex_bridge_mod"
    source_path = build_dir / "CodexBridge.cs"
    output_dll = build_dir / BRIDGE_DLL_NAME
    _build_bridge_mod(source_path=source_path, output_dll=output_dll, sts2_dll=target_game_dir / "data_sts2_windows_x86_64" / "sts2.dll")

    mod_dir = target_game_dir / "mods" / BRIDGE_MOD_ID
    mod_dir.mkdir(parents=True, exist_ok=True)
    dll_path = mod_dir / BRIDGE_DLL_NAME
    manifest_path = mod_dir / BRIDGE_MANIFEST_NAME
    try:
        shutil.copy2(output_dll, dll_path)
    except PermissionError as exc:
        raise ManagedProbeError(f"bridge dll is locked: {dll_path}. close the game before install-bridge-mod.") from exc

    manifest = {
        "id": BRIDGE_MOD_ID,
        "name": BRIDGE_MOD_ID,
        "author": "Codex",
        "description": "Local runtime bridge for in-process power application.",
        "version": "0.1.0",
        "has_pck": False,
        "has_dll": True,
        "dependencies": [],
        "affects_gameplay": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return BridgeInstallResult(
        game_dir=str(target_game_dir),
        mod_dir=str(mod_dir),
        dll_path=str(dll_path),
        manifest_path=str(manifest_path),
        manifest=manifest,
    )


def send_bridge_apply_power(
    *,
    power_type: str,
    amount: int,
    target: str = "player",
    enemy_index: int = 0,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "apply_power",
        "target": target,
        "power_type": power_type,
        "amount": int(amount),
        "enemy_index": int(enemy_index),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_add_card(
    *,
    card_type: str,
    destination: str,
    count: int = 1,
    upgrade_count: int = 0,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    normalized_destination = destination.strip().lower()
    if normalized_destination not in {"deck", "hand"}:
        raise ManagedProbeError(f"unsupported bridge card destination: {destination}")
    request = {
        "action": f"add_card_to_{normalized_destination}",
        "card_type": card_type,
        "count": int(count),
        "upgrade_count": int(upgrade_count),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_replace_master_deck(
    *,
    card_type: str,
    count: int | None = None,
    upgrade_count: int = 0,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request: dict[str, object] = {
        "action": "replace_master_deck",
        "card_type": card_type,
        "upgrade_count": int(upgrade_count),
    }
    if count is not None:
        request["count"] = int(count)
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_obtain_relic(
    *,
    relic_type: str,
    count: int = 1,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "obtain_relic",
        "relic_type": relic_type,
        "count": int(count),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_set_auto_power_on_combat_start(
    *,
    power_type: str,
    amount: int,
    target: str = "player",
    enemy_index: int = 0,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "set_auto_power_on_combat_start",
        "target": target,
        "power_type": power_type,
        "amount": int(amount),
        "enemy_index": int(enemy_index),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_clear_auto_power_on_combat_start(
    *,
    power_type: str = "",
    target: str = "",
    enemy_index: int = 0,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "clear_auto_power_on_combat_start",
        "power_type": power_type,
        "target": target,
        "enemy_index": int(enemy_index),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_jump_to_map_coord(
    *,
    col: int,
    row: int,
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "jump_to_map_coord",
        "col": int(col),
        "row": int(row),
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_tune_card_var(
    *,
    card_type: str,
    var_name: str,
    amount: int,
    scope: str,
    mode: str = "set",
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "tune_card_var",
        "card_type": card_type,
        "var_name": var_name,
        "amount": int(amount),
        "scope": scope,
        "mode": mode,
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def send_bridge_tune_relic_var(
    *,
    relic_type: str,
    var_name: str,
    amount: int,
    mode: str = "set",
    pipe_path: str = BRIDGE_PIPE_PATH,
) -> BridgeCommandResult:
    request = {
        "action": "tune_relic_var",
        "relic_type": relic_type,
        "var_name": var_name,
        "amount": int(amount),
        "mode": mode,
    }
    response = _send_pipe_json(pipe_path, request)
    return BridgeCommandResult(pipe_path=pipe_path, request=request, response=response)


def _send_pipe_json(pipe_path: str, payload: dict[str, object]) -> dict[str, object]:
    try:
        with _open_pipe(pipe_path) as handle:
            handle.write(_encode_bridge_payload(payload) + "\n")
            handle.flush()
            raw = handle.readline()
    except OSError as exc:
        raise ManagedProbeError(
            f"bridge pipe unavailable: {pipe_path}. install the bridge mod and restart the game once. ({exc})"
        ) from exc
    if not raw:
        raise ManagedProbeError("bridge pipe returned empty response")
    return _decode_bridge_payload(raw)


def _open_pipe(pipe_path: str):
    return open(pipe_path, "r+", encoding="utf-8", newline="\n")


def _build_bridge_mod(*, source_path: Path, output_dll: Path, sts2_dll: Path) -> None:
    csc_path = Path(r"C:\Program Files\Microsoft Visual Studio\18\Insiders\MSBuild\Current\Bin\Roslyn\csc.exe")
    if not csc_path.exists():
        raise ManagedProbeError(f"Roslyn csc.exe not found: {csc_path}")
    if not source_path.exists():
        raise ManagedProbeError(f"bridge mod source not found: {source_path}")
    if not sts2_dll.exists():
        raise ManagedProbeError(f"sts2.dll not found: {sts2_dll}")
    runtime_dir = sts2_dll.parent
    runtime_refs: list[str] = []
    for path in runtime_dir.glob("*.dll"):
        name = path.name
        if name not in {"netstandard.dll", "GodotSharp.dll"} and not name.startswith("System") and not name.startswith("Microsoft"):
            continue
        if any(token in name for token in ("Native", "mscordaccore", "mscordbi", "mscorrc", "coreclr", "clrjit", "clrgc", "clretwrc", "hostpolicy")):
            continue
        runtime_refs.append(f"/r:{path}")

    command = [
        str(csc_path),
        "/nologo",
        "/noconfig",
        "/nostdlib+",
        "/langversion:latest",
        "/t:library",
        f"/out:{output_dll}",
        *runtime_refs,
        f"/r:{sts2_dll}",
        str(source_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=False, check=False)
    if completed.returncode != 0:
        message = _decode_process_output(completed.stderr) or _decode_process_output(completed.stdout) or f"exit={completed.returncode}"
        raise ManagedProbeError(f"failed to compile bridge mod: {message}")


def _default_game_dir() -> Path:
    return Path(r"C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _require_dict(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ManagedProbeError(f"{label} was not an object")
    return payload


def _encode_bridge_payload(payload: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in payload.items():
        parts.append(f"{key}={value}")
    return ";".join(parts)


def _decode_bridge_payload(raw: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    for item in raw.strip().split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        if not key:
            continue
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            payload[key] = int(value)
            continue
        lowered = value.lower()
        if lowered == "true":
            payload[key] = True
            continue
        if lowered == "false":
            payload[key] = False
            continue
        payload[key] = value
    return payload


def _decode_process_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output.strip()
    if not output:
        return ""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return output.decode(encoding, errors="replace").strip()
