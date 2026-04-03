from __future__ import annotations

import json
import locale
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sts_bot.managed_probe import ManagedProbeError, _latest_dotnet_runtime_dir


@dataclass(frozen=True)
class ReflectedParameter:
    name: str
    type_name: str

    @classmethod
    def from_payload(cls, payload: object) -> "ReflectedParameter":
        data = _require_dict(payload, "parameter")
        return cls(
            name=str(data.get("name", "")),
            type_name=str(data.get("type", "")),
        )


@dataclass(frozen=True)
class ReflectedMember:
    name: str
    kind: str
    return_type: str = ""
    is_static: bool = False
    parameters: list[ReflectedParameter] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "ReflectedMember":
        data = _require_dict(payload, "member")
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            return_type=str(data.get("return_type", "")),
            is_static=bool(data.get("is_static", False)),
            parameters=[ReflectedParameter.from_payload(item) for item in _require_list(data.get("parameters", []), "member.parameters")],
        )


@dataclass(frozen=True)
class ReflectedType:
    assembly_path: str
    type_name: str
    base_type: str = ""
    attributes: list[str] = field(default_factory=list)
    constructors: list[ReflectedMember] = field(default_factory=list)
    methods: list[ReflectedMember] = field(default_factory=list)
    properties: list[ReflectedMember] = field(default_factory=list)
    fields: list[ReflectedMember] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "ReflectedType":
        data = _require_dict(payload, "reflected type")
        return cls(
            assembly_path=str(data.get("assembly_path", "")),
            type_name=str(data.get("type_name", "")),
            base_type=str(data.get("base_type", "")),
            attributes=[str(item) for item in _require_list(data.get("attributes", []), "attributes")],
            constructors=[ReflectedMember.from_payload(item) for item in _require_list(data.get("constructors", []), "constructors")],
            methods=[ReflectedMember.from_payload(item) for item in _require_list(data.get("methods", []), "methods")],
            properties=[ReflectedMember.from_payload(item) for item in _require_list(data.get("properties", []), "properties")],
            fields=[ReflectedMember.from_payload(item) for item in _require_list(data.get("fields", []), "fields")],
        )

    def to_dict(self) -> dict[str, object]:
        def member_to_dict(member: ReflectedMember) -> dict[str, object]:
            return {
                "name": member.name,
                "kind": member.kind,
                "return_type": member.return_type,
                "is_static": member.is_static,
                "parameters": [{"name": param.name, "type": param.type_name} for param in member.parameters],
            }

        return {
            "assembly_path": self.assembly_path,
            "type_name": self.type_name,
            "base_type": self.base_type,
            "attributes": list(self.attributes),
            "constructors": [member_to_dict(member) for member in self.constructors],
            "methods": [member_to_dict(member) for member in self.methods],
            "properties": [member_to_dict(member) for member in self.properties],
            "fields": [member_to_dict(member) for member in self.fields],
        }


@dataclass(frozen=True)
class SignatureMatch:
    kind: str
    signature: str

    @classmethod
    def from_payload(cls, payload: object) -> "SignatureMatch":
        data = _require_dict(payload, "signature match")
        return cls(
            kind=str(data.get("kind", "")),
            signature=str(data.get("signature", "")),
        )


@dataclass(frozen=True)
class TypeSearchMatch:
    type_name: str
    type_matched: bool
    members: list[SignatureMatch] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "TypeSearchMatch":
        data = _require_dict(payload, "type search match")
        return cls(
            type_name=str(data.get("type_name", "")),
            type_matched=bool(data.get("type_matched", False)),
            members=[SignatureMatch.from_payload(item) for item in _require_list(data.get("members", []), "type search match members")],
        )


@dataclass(frozen=True)
class SignatureSearchResult:
    assembly_path: str
    keywords: list[str] = field(default_factory=list)
    matches: list[TypeSearchMatch] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "SignatureSearchResult":
        data = _require_dict(payload, "signature search result")
        return cls(
            assembly_path=str(data.get("assembly_path", "")),
            keywords=[str(item) for item in _require_list(data.get("keywords", []), "keywords")],
            matches=[TypeSearchMatch.from_payload(item) for item in _require_list(data.get("matches", []), "matches")],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "assembly_path": self.assembly_path,
            "keywords": list(self.keywords),
            "matches": [
                {
                    "type_name": match.type_name,
                    "type_matched": match.type_matched,
                    "members": [
                        {
                            "kind": member.kind,
                            "signature": member.signature,
                        }
                        for member in match.members
                    ],
                }
                for match in self.matches
            ],
        }


@dataclass(frozen=True)
class ListedType:
    type_name: str
    base_type: str = ""
    is_abstract: bool = False
    has_parameterless_constructor: bool = False

    @classmethod
    def from_payload(cls, payload: object) -> "ListedType":
        data = _require_dict(payload, "listed type")
        return cls(
            type_name=str(data.get("type_name", "")),
            base_type=str(data.get("base_type", "")),
            is_abstract=bool(data.get("is_abstract", False)),
            has_parameterless_constructor=bool(data.get("has_parameterless_constructor", False)),
        )


@dataclass(frozen=True)
class TypeListingResult:
    assembly_path: str
    namespace_prefix: str
    assignable_to: str = ""
    types: list[ListedType] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: object) -> "TypeListingResult":
        data = _require_dict(payload, "type listing result")
        return cls(
            assembly_path=str(data.get("assembly_path", "")),
            namespace_prefix=str(data.get("namespace_prefix", "")),
            assignable_to=str(data.get("assignable_to", "")),
            types=[ListedType.from_payload(item) for item in _require_list(data.get("types", []), "types")],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "assembly_path": self.assembly_path,
            "namespace_prefix": self.namespace_prefix,
            "assignable_to": self.assignable_to,
            "types": [
                {
                    "type_name": item.type_name,
                    "base_type": item.base_type,
                    "is_abstract": item.is_abstract,
                    "has_parameterless_constructor": item.has_parameterless_constructor,
                }
                for item in self.types
            ],
        }


class TypeProbe:
    def __init__(self, *, workspace_dir: Path | None = None) -> None:
        self._workspace_dir = workspace_dir.resolve() if workspace_dir is not None else _repo_root()

    def describe_type(self, assembly_path: Path, type_name: str, *, member_keywords: tuple[str, ...] = ()) -> ReflectedType:
        payload = self._run_helper(str(assembly_path.resolve()), type_name, *member_keywords)
        return ReflectedType.from_payload(payload)

    def search_signatures(self, assembly_path: Path, *keywords: str) -> SignatureSearchResult:
        payload = self._run_helper("--search", str(assembly_path.resolve()), *keywords)
        return SignatureSearchResult.from_payload(payload)

    def list_types(self, assembly_path: Path, namespace_prefix: str, *, assignable_to: str = "") -> TypeListingResult:
        args = ["--types", str(assembly_path.resolve()), namespace_prefix]
        if assignable_to:
            args.extend(["--assignable-to", assignable_to])
        payload = self._run_helper(*args)
        return TypeListingResult.from_payload(payload)

    def _run_helper(self, *args: str) -> dict[str, object]:
        helper_dll, runtimeconfig = self._ensure_helper()
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            raise ManagedProbeError("dotnet runtime not found")

        command = [dotnet, "exec", "--runtimeconfig", str(runtimeconfig), str(helper_dll), *args]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
            raise ManagedProbeError(f"type probe failed: {stderr}")
        stdout = completed.stdout.strip()
        if not stdout:
            raise ManagedProbeError("type probe returned empty output")
        return _require_dict(json.loads(stdout), "type probe payload")

    def _ensure_helper(self) -> tuple[Path, Path]:
        helper_dir = self._workspace_dir / "tmp" / "type_probe"
        source_path = helper_dir / "Program.cs"
        output_dll = helper_dir / "type_probe.dll"
        runtimeconfig = helper_dir / "type_probe.runtimeconfig.json"
        if not source_path.exists():
            raise ManagedProbeError(f"type probe source not found: {source_path}")
        if not runtimeconfig.exists():
            raise ManagedProbeError(f"type probe runtimeconfig not found: {runtimeconfig}")
        if not output_dll.exists() or output_dll.stat().st_mtime < source_path.stat().st_mtime:
            self._build_helper(source_path=source_path, output_dll=output_dll)
        return output_dll, runtimeconfig

    def _build_helper(self, *, source_path: Path, output_dll: Path) -> None:
        csc_path = Path(r"C:\Program Files\Microsoft Visual Studio\18\Insiders\MSBuild\Current\Bin\Roslyn\csc.exe")
        if not csc_path.exists():
            raise ManagedProbeError(f"Roslyn csc.exe not found: {csc_path}")

        runtime_dir = _latest_dotnet_runtime_dir()
        runtime_refs: list[str] = []
        for path in runtime_dir.glob("*.dll"):
            name = path.name
            if name != "netstandard.dll" and not name.startswith("System") and not name.startswith("Microsoft"):
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
            "/t:exe",
            f"/out:{output_dll}",
            *runtime_refs,
            str(source_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=False, check=False)
        if completed.returncode != 0:
            message = _decode_process_output(completed.stderr) or _decode_process_output(completed.stdout) or f"exit={completed.returncode}"
            raise ManagedProbeError(f"failed to compile type probe helper: {message}")


def describe_game_type(
    assembly_path: Path,
    type_name: str,
    *,
    workspace_dir: Path | None = None,
    member_keywords: tuple[str, ...] = (),
) -> ReflectedType:
    return TypeProbe(workspace_dir=workspace_dir).describe_type(assembly_path, type_name, member_keywords=member_keywords)


def search_game_signatures(
    assembly_path: Path,
    *keywords: str,
    workspace_dir: Path | None = None,
) -> SignatureSearchResult:
    return TypeProbe(workspace_dir=workspace_dir).search_signatures(assembly_path, *keywords)


def list_game_types(
    assembly_path: Path,
    namespace_prefix: str,
    *,
    workspace_dir: Path | None = None,
    assignable_to: str = "",
) -> TypeListingResult:
    return TypeProbe(workspace_dir=workspace_dir).list_types(
        assembly_path,
        namespace_prefix,
        assignable_to=assignable_to,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _require_dict(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ManagedProbeError(f"{label} was not an object")
    return payload


def _require_list(payload: object, label: str) -> list[object]:
    if not isinstance(payload, list):
        raise ManagedProbeError(f"{label} was not a list")
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
