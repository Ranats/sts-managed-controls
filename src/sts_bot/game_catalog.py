from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sts_bot.type_probe import ListedType, list_game_types


CARD_NAMESPACE = "MegaCrit.Sts2.Core.Models.Cards"
CARD_BASE_TYPE = "MegaCrit.Sts2.Core.Models.CardModel"
POWER_NAMESPACE = "MegaCrit.Sts2.Core.Models.Powers"
POWER_BASE_TYPE = "MegaCrit.Sts2.Core.Models.PowerModel"
RELIC_NAMESPACE = "MegaCrit.Sts2.Core.Models.Relics"
RELIC_BASE_TYPE = "MegaCrit.Sts2.Core.Models.RelicModel"

DEFAULT_STS2_ASSEMBLY = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2\data_sts2_windows_x86_64\sts2.dll")


@dataclass(frozen=True)
class CatalogEntry:
    kind: str
    type_name: str
    short_name: str
    display_name: str
    base_type: str = ""
    has_parameterless_constructor: bool = False

    @property
    def search_text(self) -> str:
        return " ".join(
            [
                self.kind,
                self.type_name,
                self.short_name,
                self.display_name,
                _normalize_query(self.display_name),
                _normalize_query(self.short_name),
            ]
        ).lower()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "type_name": self.type_name,
            "short_name": self.short_name,
            "display_name": self.display_name,
            "base_type": self.base_type,
            "has_parameterless_constructor": self.has_parameterless_constructor,
        }


@dataclass(frozen=True)
class CatalogBundle:
    cards: tuple[CatalogEntry, ...]
    powers: tuple[CatalogEntry, ...]
    relics: tuple[CatalogEntry, ...]


def load_card_catalog(*, assembly_path: Path | None = None, workspace_dir: Path | None = None) -> tuple[CatalogEntry, ...]:
    return _load_catalog(
        kind="card",
        assembly_path=assembly_path,
        workspace_dir=workspace_dir,
        namespace_prefix=CARD_NAMESPACE,
        assignable_to=CARD_BASE_TYPE,
    )


def load_power_catalog(*, assembly_path: Path | None = None, workspace_dir: Path | None = None) -> tuple[CatalogEntry, ...]:
    return _load_catalog(
        kind="power",
        assembly_path=assembly_path,
        workspace_dir=workspace_dir,
        namespace_prefix=POWER_NAMESPACE,
        assignable_to=POWER_BASE_TYPE,
    )


def load_relic_catalog(*, assembly_path: Path | None = None, workspace_dir: Path | None = None) -> tuple[CatalogEntry, ...]:
    return _load_catalog(
        kind="relic",
        assembly_path=assembly_path,
        workspace_dir=workspace_dir,
        namespace_prefix=RELIC_NAMESPACE,
        assignable_to=RELIC_BASE_TYPE,
    )


def load_catalog_bundle(*, assembly_path: Path | None = None, workspace_dir: Path | None = None) -> CatalogBundle:
    return CatalogBundle(
        cards=load_card_catalog(assembly_path=assembly_path, workspace_dir=workspace_dir),
        powers=load_power_catalog(assembly_path=assembly_path, workspace_dir=workspace_dir),
        relics=load_relic_catalog(assembly_path=assembly_path, workspace_dir=workspace_dir),
    )


def filter_catalog(entries: tuple[CatalogEntry, ...], query: str) -> tuple[CatalogEntry, ...]:
    normalized = _normalize_query(query)
    if not normalized:
        return entries
    tokens = normalized.split()
    filtered = [
        entry
        for entry in entries
        if all(token in entry.search_text for token in tokens)
    ]
    return tuple(filtered)


@lru_cache(maxsize=24)
def _load_catalog_cached(
    kind: str,
    assembly_path_text: str,
    assembly_mtime_ns: int,
    workspace_dir_text: str,
    namespace_prefix: str,
    assignable_to: str,
) -> tuple[CatalogEntry, ...]:
    del assembly_mtime_ns
    workspace_dir = Path(workspace_dir_text)
    listing = list_game_types(
        Path(assembly_path_text),
        namespace_prefix,
        workspace_dir=workspace_dir,
        assignable_to=assignable_to,
    )
    entries = [
        _entry_from_listed_type(kind, item)
        for item in listing.types
        if not item.is_abstract
    ]
    return tuple(sorted(entries, key=lambda item: (item.display_name.lower(), item.short_name.lower())))


def _load_catalog(
    *,
    kind: str,
    assembly_path: Path | None,
    workspace_dir: Path | None,
    namespace_prefix: str,
    assignable_to: str,
) -> tuple[CatalogEntry, ...]:
    resolved_assembly = (assembly_path or DEFAULT_STS2_ASSEMBLY).resolve()
    resolved_workspace = workspace_dir.resolve() if workspace_dir is not None else Path(__file__).resolve().parents[2]
    return _load_catalog_cached(
        kind,
        str(resolved_assembly),
        resolved_assembly.stat().st_mtime_ns,
        str(resolved_workspace),
        namespace_prefix,
        assignable_to,
    )


def _entry_from_listed_type(kind: str, item: ListedType) -> CatalogEntry:
    short_name = item.type_name.rsplit(".", 1)[-1]
    return CatalogEntry(
        kind=kind,
        type_name=item.type_name,
        short_name=short_name,
        display_name=_display_name(short_name),
        base_type=item.base_type,
        has_parameterless_constructor=item.has_parameterless_constructor,
    )


def _display_name(type_name: str) -> str:
    stripped = type_name
    for suffix in ("Card", "Power", "Relic"):
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return re.sub(r"(?<!^)(?=[A-Z][a-z0-9])", " ", stripped).strip() or type_name


def _normalize_query(value: str) -> str:
    lowered = value.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(part for part in normalized.split() if part)
