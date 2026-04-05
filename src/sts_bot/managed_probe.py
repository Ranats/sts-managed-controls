from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sts_bot.config import CalibrationProfile
from sts_bot.windowing import WindowLocator, WindowSelector


class ManagedProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedBlockWriteResult:
    snapshot: "ManagedProbeSnapshot"
    field: str
    address: str
    previous: int
    requested: int

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedBlockWriteResult":
        data = _require_dict(payload, "managed block write payload")
        write = _require_dict(data.get("write"), "managed block write payload.write")
        return cls(
            snapshot=ManagedProbeSnapshot.from_payload(data),
            field=str(write.get("field", "")),
            address=str(write.get("address", "")),
            previous=_parse_int(write.get("previous"), "previous"),
            requested=_parse_int(write.get("requested"), "requested"),
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.snapshot.to_dict()
        payload["write"] = {
            "field": self.field,
            "address": self.address,
            "previous": self.previous,
            "requested": self.requested,
        }
        return payload


@dataclass(frozen=True)
class ManagedPowerWriteResult:
    snapshot: "ManagedProbeSnapshot"
    field: str
    target: str
    power_type: str
    power_address: str
    target_creature: str
    address: str
    previous: int
    requested: int

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedPowerWriteResult":
        data = _require_dict(payload, "managed power write payload")
        write = _require_dict(data.get("write"), "managed power write payload.write")
        return cls(
            snapshot=ManagedProbeSnapshot.from_payload(data),
            field=str(write.get("field", "")),
            target=str(write.get("target", "")),
            power_type=str(write.get("power_type", "")),
            power_address=str(write.get("power_address", "")),
            target_creature=str(write.get("target_creature", "")),
            address=str(write.get("address", "")),
            previous=_parse_int(write.get("previous"), "previous"),
            requested=_parse_int(write.get("requested"), "requested"),
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.snapshot.to_dict()
        payload["write"] = {
            "field": self.field,
            "target": self.target,
            "power_type": self.power_type,
            "power_address": self.power_address,
            "target_creature": self.target_creature,
            "address": self.address,
            "previous": self.previous,
            "requested": self.requested,
        }
        return payload


@dataclass(frozen=True)
class ManagedPowerAliasResult:
    snapshot: "ManagedProbeSnapshot"
    field: str
    source: str
    dest: str
    source_creature: str
    dest_creature: str
    previous: str
    requested: str
    address: str

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedPowerAliasResult":
        data = _require_dict(payload, "managed power alias payload")
        write = _require_dict(data.get("write"), "managed power alias payload.write")
        return cls(
            snapshot=ManagedProbeSnapshot.from_payload(data),
            field=str(write.get("field", "")),
            source=str(write.get("source", "")),
            dest=str(write.get("dest", "")),
            source_creature=str(write.get("source_creature", "")),
            dest_creature=str(write.get("dest_creature", "")),
            previous=str(write.get("previous", "")),
            requested=str(write.get("requested", "")),
            address=str(write.get("address", "")),
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.snapshot.to_dict()
        payload["write"] = {
            "field": self.field,
            "source": self.source,
            "dest": self.dest,
            "source_creature": self.source_creature,
            "dest_creature": self.dest_creature,
            "previous": self.previous,
            "requested": self.requested,
            "address": self.address,
        }
        return payload


@dataclass(frozen=True)
class ManagedEnergyWriteResult:
    snapshot: "ManagedProbeSnapshot"
    field: str
    energy_address: str
    previous_energy: int
    requested_energy: int
    max_energy_address: str
    previous_max_energy: int
    requested_max_energy: int
    wrote_max_energy: bool

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedEnergyWriteResult":
        data = _require_dict(payload, "managed energy write payload")
        write = _require_dict(data.get("write"), "managed energy write payload.write")
        return cls(
            snapshot=ManagedProbeSnapshot.from_payload(data),
            field=str(write.get("field", "")),
            energy_address=str(write.get("energy_address", "")),
            previous_energy=_parse_int(write.get("previous_energy"), "previous_energy"),
            requested_energy=_parse_int(write.get("requested_energy"), "requested_energy"),
            max_energy_address=str(write.get("max_energy_address", "")),
            previous_max_energy=_parse_int(write.get("previous_max_energy"), "previous_max_energy"),
            requested_max_energy=_parse_int(write.get("requested_max_energy"), "requested_max_energy"),
            wrote_max_energy=bool(write.get("wrote_max_energy", False)),
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.snapshot.to_dict()
        payload["write"] = {
            "field": self.field,
            "energy_address": self.energy_address,
            "previous_energy": self.previous_energy,
            "requested_energy": self.requested_energy,
            "max_energy_address": self.max_energy_address,
            "previous_max_energy": self.previous_max_energy,
            "requested_max_energy": self.requested_max_energy,
            "wrote_max_energy": self.wrote_max_energy,
        }
        return payload


@dataclass(frozen=True)
class ManagedGoldWriteResult:
    snapshot: "ManagedProbeSnapshot"
    field: str
    gold_address: str
    previous_gold: int
    requested_gold: int
    ui_address: str
    previous_ui_gold: int
    wrote_ui_gold: bool

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedGoldWriteResult":
        data = _require_dict(payload, "managed gold write payload")
        write = _require_dict(data.get("write"), "managed gold write payload.write")
        return cls(
            snapshot=ManagedProbeSnapshot.from_payload(data),
            field=str(write.get("field", "")),
            gold_address=str(write.get("gold_address", "")),
            previous_gold=_parse_int(write.get("previous_gold"), "previous_gold"),
            requested_gold=_parse_int(write.get("requested_gold"), "requested_gold"),
            ui_address=str(write.get("ui_address", "")),
            previous_ui_gold=_parse_int(write.get("previous_ui_gold"), "previous_ui_gold"),
            wrote_ui_gold=bool(write.get("wrote_ui_gold", False)),
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.snapshot.to_dict()
        payload["write"] = {
            "field": self.field,
            "gold_address": self.gold_address,
            "previous_gold": self.previous_gold,
            "requested_gold": self.requested_gold,
            "ui_address": self.ui_address,
            "previous_ui_gold": self.previous_ui_gold,
            "wrote_ui_gold": self.wrote_ui_gold,
        }
        return payload


@dataclass(frozen=True)
class ManagedPowerSnapshot:
    address: str
    type_name: str
    amount: int

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedPowerSnapshot":
        data = _require_dict(payload, "player power")
        return cls(
            address=str(data.get("address", "")),
            type_name=str(data.get("type", "")),
            amount=_parse_int(data.get("amount"), "player power amount"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "type": self.type_name,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class ManagedEnemySnapshot:
    address: str
    current_hp: int
    max_hp: int
    block: int
    powers: list[ManagedPowerSnapshot] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedEnemySnapshot":
        data = _require_dict(payload, "enemy")
        return cls(
            address=str(data.get("address", "")),
            current_hp=_parse_int(data.get("current_hp"), "enemy current_hp"),
            max_hp=_parse_int(data.get("max_hp"), "enemy max_hp"),
            block=_parse_int(data.get("block"), "enemy block"),
            powers=[
                ManagedPowerSnapshot.from_payload(item)
                for item in _parse_list(data.get("powers"), "enemy powers")
            ],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "current_hp": self.current_hp,
            "max_hp": self.max_hp,
            "block": self.block,
            "powers": [power.to_dict() for power in self.powers],
        }


@dataclass(frozen=True)
class ManagedProbeSnapshot:
    pid: int
    runtime_version: str
    floor: int
    ascension: int
    gold: int
    hp: int
    max_hp: int
    block: int
    energy: int
    max_energy: int
    objects: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    player_powers: list[ManagedPowerSnapshot] = field(default_factory=list)
    enemies: list[ManagedEnemySnapshot] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "ManagedProbeSnapshot":
        data = _require_dict(payload, "managed probe payload")
        return cls(
            pid=_parse_int(data.get("pid"), "pid"),
            runtime_version=str(data.get("runtime_version", "")),
            floor=_parse_int(data.get("floor"), "floor"),
            ascension=_parse_int(data.get("ascension"), "ascension"),
            gold=_parse_int(data.get("gold"), "gold"),
            hp=_parse_int(data.get("hp"), "hp"),
            max_hp=_parse_int(data.get("max_hp"), "max_hp"),
            block=_parse_int(data.get("block"), "block"),
            energy=_parse_int(data.get("energy"), "energy"),
            max_energy=_parse_int(data.get("max_energy"), "max_energy"),
            objects=_parse_str_dict(data.get("objects"), "objects"),
            sources=_parse_str_dict(data.get("sources"), "sources"),
            player_powers=[
                ManagedPowerSnapshot.from_payload(item)
                for item in _parse_list(data.get("player_powers"), "player_powers")
            ],
            enemies=[
                ManagedEnemySnapshot.from_payload(item)
                for item in _parse_list(data.get("enemies"), "enemies")
            ],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "runtime_version": self.runtime_version,
            "floor": self.floor,
            "ascension": self.ascension,
            "gold": self.gold,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "block": self.block,
            "energy": self.energy,
            "max_energy": self.max_energy,
            "objects": dict(self.objects),
            "sources": dict(self.sources),
            "player_powers": [power.to_dict() for power in self.player_powers],
            "enemies": [enemy.to_dict() for enemy in self.enemies],
        }


class ManagedSnapshotProbe:
    def __init__(self, *, workspace_dir: Path | None = None) -> None:
        self._workspace_dir = workspace_dir.resolve() if workspace_dir is not None else _repo_root()

    def probe_profile(self, profile: CalibrationProfile) -> ManagedProbeSnapshot:
        target = WindowLocator(_selector_for_profile(profile)).locate()
        return self.probe_pid(target.pid)

    def probe_pid(self, pid: int) -> ManagedProbeSnapshot:
        payload = self._run_helper(pid)
        return ManagedProbeSnapshot.from_payload(payload)

    def set_player_block(self, pid: int, value: int) -> ManagedBlockWriteResult:
        payload = self._run_helper_command("--set-player-block", str(pid), str(value))
        return ManagedBlockWriteResult.from_payload(payload)

    def set_power_amount(self, pid: int, *, target: str, power_type: str, value: int) -> ManagedPowerWriteResult:
        payload = self._run_helper_command("--set-power-amount", str(pid), target, power_type, str(value))
        return ManagedPowerWriteResult.from_payload(payload)

    def alias_powers(self, pid: int, *, source: str, dest: str) -> ManagedPowerAliasResult:
        payload = self._run_helper_command("--alias-powers", str(pid), source, dest)
        return ManagedPowerAliasResult.from_payload(payload)

    def set_player_energy(self, pid: int, *, energy: int, max_energy: int | None = None) -> ManagedEnergyWriteResult:
        args = ["--set-player-energy", str(pid), str(energy)]
        if max_energy is not None:
            args.append(str(max_energy))
        payload = self._run_helper_command(*args)
        return ManagedEnergyWriteResult.from_payload(payload)

    def set_player_gold(self, pid: int, *, gold: int) -> ManagedGoldWriteResult:
        payload = self._run_helper_command("--set-player-gold", str(pid), str(gold))
        return ManagedGoldWriteResult.from_payload(payload)

    def _run_helper(self, pid: int) -> dict[str, object]:
        return self._run_helper_command("--summary-json", str(pid))

    def _run_helper_command(self, *args: str) -> dict[str, object]:
        helper_dll, runtimeconfig = self._ensure_probe_helper()
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            raise ManagedProbeError("dotnet runtime not found")

        command = [
            dotnet,
            "exec",
            "--runtimeconfig",
            str(runtimeconfig),
            str(helper_dll),
            *args,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
            raise ManagedProbeError(f"managed probe failed: {stderr}")

        stdout = completed.stdout.strip()
        if not stdout:
            raise ManagedProbeError("managed probe returned empty output")
        try:
            return _require_dict(json.loads(stdout), "managed probe payload")
        except json.JSONDecodeError:
            candidate = _extract_json_object(stdout)
            if candidate is None:
                raise ManagedProbeError("managed probe returned invalid json")
            try:
                return _require_dict(json.loads(candidate), "managed probe payload")
            except json.JSONDecodeError as exc:
                raise ManagedProbeError(f"managed probe returned invalid json: {exc}") from exc

    def _ensure_probe_helper(self) -> tuple[Path, Path]:
        helper_dir = self._helper_dir()
        source_path = helper_dir / "Program.cs"
        output_dll = helper_dir / "clrmd_probe.dll"
        runtimeconfig = helper_dir / "clrmd_probe.runtimeconfig.json"
        if not source_path.exists():
            raise ManagedProbeError(f"probe source not found: {source_path}")
        if not runtimeconfig.exists():
            raise ManagedProbeError(f"probe runtimeconfig not found: {runtimeconfig}")

        if not output_dll.exists() or output_dll.stat().st_mtime < source_path.stat().st_mtime:
            self._build_probe_helper(source_path=source_path, output_dll=output_dll)

        if not output_dll.exists():
            raise ManagedProbeError(f"probe binary not found: {output_dll}")
        return output_dll, runtimeconfig

    def _helper_dir(self) -> Path:
        return self._workspace_dir / "tmp" / "clrmd_probe"

    def _nuget_root(self) -> Path:
        return self._workspace_dir / "tmp" / "nuget"

    def _build_probe_helper(self, *, source_path: Path, output_dll: Path) -> None:
        csc_path = Path(r"C:\Program Files\Microsoft Visual Studio\18\Insiders\MSBuild\Current\Bin\Roslyn\csc.exe")
        if not csc_path.exists():
            raise ManagedProbeError(f"Roslyn csc.exe not found: {csc_path}")

        runtime_dir = _latest_dotnet_runtime_dir()
        runtime_refs: list[str] = []
        for path in runtime_dir.glob("*.dll"):
            name = path.name
            if name != "netstandard.dll" and not name.startswith("System") and not name.startswith("Microsoft"):
                continue
            if any(
                token in name
                for token in (
                    "Native",
                    "mscordaccore",
                    "mscordbi",
                    "mscorrc",
                    "coreclr",
                    "clrjit",
                    "clrgc",
                    "clretwrc",
                    "hostpolicy",
                )
            ):
                continue
            runtime_refs.append(f"/r:{path}")

        extra_refs = [
            _require_dependency(self._nuget_root() / "clrmd" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Diagnostics.Runtime.dll"),
            _require_dependency(self._nuget_root() / "netcoreclient0410" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Diagnostics.NETCore.Client.dll"),
            _require_dependency(self._nuget_root() / "system.collections.immutable" / "pkg" / "lib" / "netstandard2.0" / "System.Collections.Immutable.dll"),
            _require_dependency(self._nuget_root() / "system.runtime.compilerservices.unsafe" / "pkg" / "lib" / "netstandard2.0" / "System.Runtime.CompilerServices.Unsafe.dll"),
        ]
        copy_candidates = [
            self._nuget_root() / "clrmd" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Diagnostics.Runtime.dll",
            self._nuget_root() / "netcoreclient0410" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Diagnostics.NETCore.Client.dll",
            self._nuget_root() / "system.collections.immutable" / "pkg" / "lib" / "netstandard2.0" / "System.Collections.Immutable.dll",
            self._nuget_root() / "system.runtime.compilerservices.unsafe" / "pkg" / "lib" / "netstandard2.0" / "System.Runtime.CompilerServices.Unsafe.dll",
            self._nuget_root() / "microsoft.bcl.asyncinterfaces" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Bcl.AsyncInterfaces.dll",
            self._nuget_root() / "microsoft.extensions.logging" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Extensions.Logging.dll",
            self._nuget_root() / "microsoft.extensions.logging.abstractions" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Extensions.Logging.Abstractions.dll",
            self._nuget_root() / "microsoft.extensions.dependencyinjection.abstractions" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Extensions.DependencyInjection.Abstractions.dll",
            self._nuget_root() / "microsoft.extensions.options" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Extensions.Options.dll",
            self._nuget_root() / "microsoft.extensions.primitives" / "pkg" / "lib" / "netstandard2.0" / "Microsoft.Extensions.Primitives.dll",
        ]

        command = [
            str(csc_path),
            "/nologo",
            "/noconfig",
            "/nostdlib+",
            "/langversion:latest",
            "/t:exe",
            f"/out:{output_dll}",
            *runtime_refs,
            *[f"/r:{path}" for path in extra_refs],
            str(source_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
            raise ManagedProbeError(f"failed to compile managed probe helper: {message}")

        for source in copy_candidates:
            if source.exists():
                shutil.copy2(source, output_dll.parent / source.name)


def probe_managed_numeric(pid: int, *, workspace_dir: Path | None = None) -> ManagedProbeSnapshot:
        return ManagedSnapshotProbe(workspace_dir=workspace_dir).probe_pid(pid)


def set_managed_player_block(pid: int, value: int, *, workspace_dir: Path | None = None) -> ManagedBlockWriteResult:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).set_player_block(pid, value)


def set_managed_power_amount(
    pid: int,
    *,
    target: str,
    power_type: str,
    value: int,
    workspace_dir: Path | None = None,
) -> ManagedPowerWriteResult:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).set_power_amount(pid, target=target, power_type=power_type, value=value)


def alias_managed_powers(
    pid: int,
    *,
    source: str,
    dest: str,
    workspace_dir: Path | None = None,
) -> ManagedPowerAliasResult:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).alias_powers(pid, source=source, dest=dest)


def set_managed_player_energy(
    pid: int,
    *,
    energy: int,
    max_energy: int | None = None,
    workspace_dir: Path | None = None,
) -> ManagedEnergyWriteResult:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).set_player_energy(pid, energy=energy, max_energy=max_energy)


def set_managed_player_gold(
    pid: int,
    *,
    gold: int,
    workspace_dir: Path | None = None,
) -> ManagedGoldWriteResult:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).set_player_gold(pid, gold=gold)


def probe_managed_pid(pid: int, *, workspace_dir: Path | None = None) -> dict[str, object]:
    return probe_managed_numeric(pid, workspace_dir=workspace_dir).to_dict()


def probe_managed_profile(profile: CalibrationProfile, *, workspace_dir: Path | None = None) -> dict[str, object]:
    return ManagedSnapshotProbe(workspace_dir=workspace_dir).probe_profile(profile).to_dict()


def _selector_for_profile(profile: CalibrationProfile) -> WindowSelector:
    return WindowSelector(
        process_name=profile.target_process_name,
        title_regex=profile.target_title_regex or profile.window_title,
        class_name=profile.target_class_name,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _latest_dotnet_runtime_dir() -> Path:
    root = Path(r"C:\Program Files\dotnet\shared\Microsoft.NETCore.App")
    versions = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: item.name)
    if not versions:
        raise ManagedProbeError(f".NET runtime directory not found: {root}")
    return versions[-1]


def _require_dependency(path: Path) -> Path:
    if not path.exists():
        raise ManagedProbeError(f"managed probe dependency not found: {path}")
    return path


def _require_dict(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ManagedProbeError(f"{label} must be a JSON object")
    return payload


def _parse_list(payload: object, label: str) -> list[object]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ManagedProbeError(f"{label} must be a JSON array")
    return payload


def _parse_str_dict(payload: object, label: str) -> dict[str, str]:
    if payload is None:
        return {}
    data = _require_dict(payload, label)
    return {str(key): str(value) for key, value in data.items()}


def _parse_int(value: object, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ManagedProbeError(f"{label} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ManagedProbeError(f"{label} must be an integer") from exc


def _extract_json_object(stdout: str) -> str | None:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            return line
    return None
