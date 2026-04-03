from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class CardKnowledge:
    name: str
    base_score: float = 0.0
    prefers: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    avoids_without: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    energy_cost: int | None = None
    damage: int = 0
    block: int = 0
    draw: int = 0
    grants_strength: bool = False


@dataclass(slots=True)
class RelicKnowledge:
    name: str
    base_score: float = 0.0
    prefers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class ChoiceKnowledge:
    name: str
    base_score: float = 0.0
    prefers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    downside_tags: tuple[str, ...] = ()


DEFAULT_KB_OVERLAY_PATH = Path("data") / "kb_overlay.json"


_CARD_DB: dict[str, CardKnowledge] = {
    "strike": CardKnowledge("Strike", base_score=0.2, tags=("attack",), energy_cost=1, damage=6),
    "defend": CardKnowledge("Defend", base_score=0.1, tags=("block",), energy_cost=1, block=5),
    "bash": CardKnowledge("Bash", base_score=1.5, tags=("attack",), energy_cost=2, damage=8),
    "pommel strike": CardKnowledge("Pommel Strike", base_score=2.0, tags=("attack",), energy_cost=1, damage=9, draw=1),
    "shrug it off": CardKnowledge("Shrug It Off", base_score=4.5, prefers=("block",), tags=("block",), energy_cost=1, block=8, draw=1),
    "limit break": CardKnowledge("Limit Break", base_score=1.0, requires=("strength",), avoids_without=("strength",), tags=("strength",), energy_cost=1),
    "true grit": CardKnowledge("True Grit", base_score=3.5, prefers=("exhaust", "block"), tags=("exhaust", "block"), energy_cost=1, block=7),
    "inflame": CardKnowledge("Inflame", base_score=3.2, prefers=("strength",), tags=("strength", "scaling"), energy_cost=1, grants_strength=True),
    "battle trance": CardKnowledge("Battle Trance", base_score=2.8, prefers=("draw",), tags=("draw",), energy_cost=1, draw=3),
    "offering": CardKnowledge("Offering", base_score=4.0, prefers=("exhaust",), tags=("draw", "energy", "exhaust"), energy_cost=0, draw=3),
    "second wind": CardKnowledge("Second Wind", base_score=3.0, prefers=("exhaust", "block"), tags=("exhaust", "block"), energy_cost=1, block=7),
    "uppercut": CardKnowledge("Uppercut", base_score=3.4, prefers=("attack",), tags=("attack",), energy_cost=2, damage=13),
    "slimed": CardKnowledge("Slimed", base_score=-4.0, tags=("status", "dead_draw"), energy_cost=1),
    "injury": CardKnowledge("Injury", base_score=-7.0, tags=("curse", "dead_draw")),
    "wound": CardKnowledge("Wound", base_score=-7.0, tags=("status", "dead_draw")),
    "dazed": CardKnowledge("Dazed", base_score=-7.0, tags=("status", "dead_draw"), energy_cost=0),
    "burn": CardKnowledge("Burn", base_score=-6.0, tags=("status", "dead_draw"), energy_cost=0),
}

_RELIC_DB: dict[str, RelicKnowledge] = {
    "anchor": RelicKnowledge("Anchor", base_score=3.0, prefers=("block",), tags=("block",)),
    "burning blood": RelicKnowledge("Burning Blood", base_score=2.5, tags=("heal",)),
}

_NEOW_DB: dict[str, ChoiceKnowledge] = {
    "transform": ChoiceKnowledge("Transform", base_score=5.0, tags=("cleanup",)),
    "remove": ChoiceKnowledge("Remove", base_score=5.5, tags=("cleanup",)),
    "gold": ChoiceKnowledge("Gold", base_score=3.5, tags=("economy",)),
    "rare": ChoiceKnowledge("Rare", base_score=4.0, tags=("power",)),
    "hp_cost": ChoiceKnowledge("HP cost", base_score=-4.0, downside_tags=("hp_cost",)),
    "card_add": ChoiceKnowledge("Card add", base_score=-1.5, downside_tags=("deck_bloat",)),
    "curse": ChoiceKnowledge("Curse", base_score=-6.0, downside_tags=("curse",)),
    "max_hp": ChoiceKnowledge("Max HP", base_score=1.0, tags=("heal",)),
}

_EVENT_DB: dict[str, ChoiceKnowledge] = {
    "heal": ChoiceKnowledge("Heal", base_score=2.5, tags=("heal",)),
    "hp_cost": ChoiceKnowledge("HP cost", base_score=-2.5, downside_tags=("hp_cost",)),
    "remove": ChoiceKnowledge("Remove", base_score=2.0, tags=("cleanup",)),
    "upgrade": ChoiceKnowledge("Upgrade", base_score=1.5, tags=("upgrade",)),
    "relic": ChoiceKnowledge("Relic", base_score=1.5, tags=("relic",)),
    "curse": ChoiceKnowledge("Curse", base_score=-4.0, downside_tags=("curse",)),
    "fight": ChoiceKnowledge("Fight", base_score=0.5, tags=("combat",)),
}

_SHOP_DB: dict[str, ChoiceKnowledge] = {
    "card": ChoiceKnowledge("Card", base_score=1.5),
    "relic": ChoiceKnowledge("Relic", base_score=2.5),
    "potion": ChoiceKnowledge("Potion", base_score=1.0),
    "remove": ChoiceKnowledge("Remove", base_score=4.0, tags=("cleanup",)),
}

_POTION_DB: dict[str, ChoiceKnowledge] = {
    "attack potion": ChoiceKnowledge("Attack Potion", base_score=2.0, prefers=("attack",), tags=("attack",)),
    "skill potion": ChoiceKnowledge("Skill Potion", base_score=1.8, prefers=("block",), tags=("block",)),
    "fire potion": ChoiceKnowledge("Fire Potion", base_score=2.2, prefers=("attack",), tags=("attack",)),
}

_BOSS_RELIC_DB: dict[str, ChoiceKnowledge] = {
    "black blood": ChoiceKnowledge("Black Blood", base_score=4.0, tags=("heal",)),
    "coffee dripper": ChoiceKnowledge("Coffee Dripper", base_score=3.5, tags=("snowball",), downside_tags=("no_rest_heal",)),
    "philosopher's stone": ChoiceKnowledge("Philosopher's Stone", base_score=3.0, tags=("snowball",), downside_tags=("enemy_strength",)),
    "sozu": ChoiceKnowledge("Sozu", base_score=2.5, downside_tags=("no_more_potions",)),
}

_ACTIVE_KB_OVERLAY_PATH = DEFAULT_KB_OVERLAY_PATH
_OVERLAY_CACHE_PATH: Path | None = None
_OVERLAY_CACHE_MTIME_NS: int | None = None
_OVERLAY_CACHE_PAYLOAD: dict[str, object] | None = None


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).replace("_", " ").split()).strip().lower()


def _choice_db_for_domain(domain: str) -> dict[str, ChoiceKnowledge]:
    normalized = _normalize_name(domain)
    if normalized == "neow":
        return _NEOW_DB
    if normalized == "event":
        return _EVENT_DB
    if normalized in {"shop", "shop purchase", "shop_purchase", "shop remove", "shop_remove"}:
        return _SHOP_DB
    if normalized in {"reward potion", "reward_potion", "potion"}:
        return _POTION_DB
    if normalized in {"boss relic", "boss_relic"}:
        return _BOSS_RELIC_DB
    return {}


def _normalize_entity_key(value: str | None) -> str:
    normalized = _normalize_name(value)
    if normalized == "boss relic":
        return "boss_relic"
    return normalized.replace(" ", "_")


def _empty_overlay() -> dict[str, object]:
    return {
        "version": 1,
        "cards": {},
        "relics": {},
        "potions": {},
        "boss_relics": {},
        "choices": {},
        "aliases": {
            "card": {},
            "relic": {},
            "potion": {},
            "boss_relic": {},
        },
    }


def active_kb_overlay_path() -> Path:
    return _ACTIVE_KB_OVERLAY_PATH


def set_active_kb_overlay_path(path: Path | None) -> Path:
    global _ACTIVE_KB_OVERLAY_PATH
    global _OVERLAY_CACHE_PATH
    global _OVERLAY_CACHE_MTIME_NS
    global _OVERLAY_CACHE_PAYLOAD
    _ACTIVE_KB_OVERLAY_PATH = Path(path) if path is not None else DEFAULT_KB_OVERLAY_PATH
    _OVERLAY_CACHE_PATH = None
    _OVERLAY_CACHE_MTIME_NS = None
    _OVERLAY_CACHE_PAYLOAD = None
    return _ACTIVE_KB_OVERLAY_PATH


def _normalize_overlay(payload: object) -> dict[str, object]:
    overlay = _empty_overlay()
    if not isinstance(payload, dict):
        return overlay
    for key in ("cards", "relics", "potions", "boss_relics", "choices"):
        value = payload.get(key)
        overlay[key] = dict(value) if isinstance(value, dict) else {}
    aliases = payload.get("aliases")
    if isinstance(aliases, dict):
        for entity in ("card", "relic", "potion", "boss_relic"):
            entity_aliases = aliases.get(entity)
            overlay["aliases"][entity] = dict(entity_aliases) if isinstance(entity_aliases, dict) else {}
    return overlay


def load_kb_overlay(path: Path | None = None, *, force_reload: bool = False) -> dict[str, object]:
    global _OVERLAY_CACHE_PATH
    global _OVERLAY_CACHE_MTIME_NS
    global _OVERLAY_CACHE_PAYLOAD

    selected_path = Path(path) if path is not None else _ACTIVE_KB_OVERLAY_PATH
    if not selected_path.exists():
        _OVERLAY_CACHE_PATH = selected_path
        _OVERLAY_CACHE_MTIME_NS = None
        _OVERLAY_CACHE_PAYLOAD = _empty_overlay()
        return _OVERLAY_CACHE_PAYLOAD

    mtime_ns = selected_path.stat().st_mtime_ns
    if (
        not force_reload
        and _OVERLAY_CACHE_PAYLOAD is not None
        and _OVERLAY_CACHE_PATH == selected_path
        and _OVERLAY_CACHE_MTIME_NS == mtime_ns
    ):
        return _OVERLAY_CACHE_PAYLOAD

    raw = json.loads(selected_path.read_text(encoding="utf-8"))
    _OVERLAY_CACHE_PATH = selected_path
    _OVERLAY_CACHE_MTIME_NS = mtime_ns
    _OVERLAY_CACHE_PAYLOAD = _normalize_overlay(raw)
    return _OVERLAY_CACHE_PAYLOAD


def save_kb_overlay(payload: dict[str, object], path: Path | None = None) -> Path:
    selected_path = Path(path) if path is not None else _ACTIVE_KB_OVERLAY_PATH
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_overlay(payload)
    selected_path.write_text(json.dumps(normalized, indent=2, ensure_ascii=True), encoding="utf-8")
    load_kb_overlay(selected_path, force_reload=True)
    return selected_path


def _unique_texts(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    seen: list[str] = []
    for value in values:
        normalized = _normalize_name(value)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return tuple(seen)


def _clamp_score(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(-20.0, min(20.0, number))


def _clean_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _choice_payload_to_model(default_key: str, payload: object) -> ChoiceKnowledge:
    if not isinstance(payload, dict):
        return ChoiceKnowledge(default_key.title())
    return ChoiceKnowledge(
        name=str(payload.get("name") or default_key.title()),
        base_score=_clamp_score(payload.get("base_score"), 0.0),
        prefers=_unique_texts(payload.get("prefers")),
        tags=_unique_texts(payload.get("tags")),
        downside_tags=_unique_texts(payload.get("downside_tags")),
    )


def _card_payload_to_model(default_key: str, payload: object) -> CardKnowledge:
    if not isinstance(payload, dict):
        return CardKnowledge(default_key.title())
    return CardKnowledge(
        name=str(payload.get("name") or default_key.title()),
        base_score=_clamp_score(payload.get("base_score"), 0.0),
        prefers=_unique_texts(payload.get("prefers")),
        requires=_unique_texts(payload.get("requires")),
        avoids_without=_unique_texts(payload.get("avoids_without")),
        tags=_unique_texts(payload.get("tags")),
        energy_cost=_clean_optional_int(payload.get("energy_cost")),
        damage=int(payload.get("damage") or 0),
        block=int(payload.get("block") or 0),
        draw=int(payload.get("draw") or 0),
        grants_strength=bool(payload.get("grants_strength")),
    )


def _relic_payload_to_model(default_key: str, payload: object) -> RelicKnowledge:
    if not isinstance(payload, dict):
        return RelicKnowledge(default_key.title())
    return RelicKnowledge(
        name=str(payload.get("name") or default_key.title()),
        base_score=_clamp_score(payload.get("base_score"), 0.0),
        prefers=_unique_texts(payload.get("prefers")),
        tags=_unique_texts(payload.get("tags")),
    )


def _sanitize_choice_payload(key: str, payload: object) -> dict[str, object]:
    model = _choice_payload_to_model(key, payload)
    return asdict(model)


def _sanitize_card_payload(key: str, payload: object) -> dict[str, object]:
    model = _card_payload_to_model(key, payload)
    return asdict(model)


def _sanitize_relic_payload(key: str, payload: object) -> dict[str, object]:
    model = _relic_payload_to_model(key, payload)
    return asdict(model)


def _entity_aliases(entity: str) -> dict[str, str]:
    overlay = load_kb_overlay()
    aliases = overlay.get("aliases", {})
    if not isinstance(aliases, dict):
        return {}
    selected = aliases.get(entity, {})
    return dict(selected) if isinstance(selected, dict) else {}


def _resolve_alias(entity: str, value: str | None) -> str:
    normalized = _normalize_name(value)
    aliases = _entity_aliases(entity)
    return _normalize_name(aliases.get(normalized, normalized))


def _merged_card_db() -> dict[str, CardKnowledge]:
    overlay = load_kb_overlay()
    merged = dict(_CARD_DB)
    overlay_cards = overlay.get("cards", {})
    if isinstance(overlay_cards, dict):
        for key, payload in overlay_cards.items():
            normalized = _normalize_name(key)
            if normalized:
                merged[normalized] = _card_payload_to_model(normalized, payload)
    return merged


def _merged_relic_db() -> dict[str, RelicKnowledge]:
    overlay = load_kb_overlay()
    merged = dict(_RELIC_DB)
    overlay_relics = overlay.get("relics", {})
    if isinstance(overlay_relics, dict):
        for key, payload in overlay_relics.items():
            normalized = _normalize_name(key)
            if normalized:
                merged[normalized] = _relic_payload_to_model(normalized, payload)
    return merged


def _merged_potion_db() -> dict[str, ChoiceKnowledge]:
    overlay = load_kb_overlay()
    merged = dict(_POTION_DB)
    overlay_potions = overlay.get("potions", {})
    if isinstance(overlay_potions, dict):
        for key, payload in overlay_potions.items():
            normalized = _normalize_name(key)
            if normalized:
                merged[normalized] = _choice_payload_to_model(normalized, payload)
    return merged


def _merged_boss_relic_db() -> dict[str, ChoiceKnowledge]:
    overlay = load_kb_overlay()
    merged = dict(_BOSS_RELIC_DB)
    overlay_boss_relics = overlay.get("boss_relics", {})
    if isinstance(overlay_boss_relics, dict):
        for key, payload in overlay_boss_relics.items():
            normalized = _normalize_name(key)
            if normalized:
                merged[normalized] = _choice_payload_to_model(normalized, payload)
    return merged


def _merged_choice_db(domain: str) -> dict[str, ChoiceKnowledge]:
    overlay = load_kb_overlay()
    merged = {
        _normalize_name(key): value
        for key, value in _choice_db_for_domain(domain).items()
    }
    overlay_choices = overlay.get("choices", {})
    if not isinstance(overlay_choices, dict):
        return merged
    selected = overlay_choices.get(_normalize_name(domain), {})
    if not isinstance(selected, dict):
        return merged
    for key, payload in selected.items():
        normalized = _normalize_name(key)
        if normalized:
            merged[normalized] = _choice_payload_to_model(normalized, payload)
    return merged


def canonicalize_card_name(card_name: str | None) -> str:
    normalized = _resolve_alias("card", card_name)
    if not normalized:
        return ""
    entry = _merged_card_db().get(normalized)
    return entry.name if entry is not None else " ".join(normalized.split())


def canonicalize_relic_name(relic_name: str | None) -> str:
    normalized = _resolve_alias("relic", relic_name)
    if not normalized:
        return ""
    entry = _merged_relic_db().get(normalized)
    return entry.name if entry is not None else " ".join(normalized.split())


def canonicalize_potion_name(potion_name: str | None) -> str:
    normalized = _resolve_alias("potion", potion_name)
    if not normalized:
        return ""
    entry = _merged_potion_db().get(normalized)
    return entry.name if entry is not None else " ".join(normalized.split())


def canonicalize_boss_relic_name(relic_name: str | None) -> str:
    normalized = _resolve_alias("boss_relic", relic_name)
    if not normalized:
        return ""
    entry = _merged_boss_relic_db().get(normalized)
    return entry.name if entry is not None else " ".join(normalized.split())


def known_relic_names() -> list[str]:
    return sorted(entry.name for entry in _merged_relic_db().values())


def known_potion_names() -> list[str]:
    return sorted(entry.name for entry in _merged_potion_db().values())


def lookup_card_knowledge(character: str, card_name: str | None) -> CardKnowledge | None:
    del character
    normalized = _resolve_alias("card", card_name)
    if not normalized:
        return None
    return _merged_card_db().get(normalized)


def lookup_relic_knowledge(character: str, relic_name: str | None) -> RelicKnowledge | None:
    del character
    normalized = _resolve_alias("relic", relic_name)
    if not normalized:
        return None
    return _merged_relic_db().get(normalized)


def lookup_neow_choice(key: str | None) -> ChoiceKnowledge | None:
    normalized = _normalize_name(key)
    if not normalized:
        return None
    return _merged_choice_db("neow").get(normalized)


def lookup_event_choice(key: str | None) -> ChoiceKnowledge | None:
    normalized = _normalize_name(key)
    if not normalized:
        return None
    return _merged_choice_db("event").get(normalized)


def lookup_shop_choice(key: str | None) -> ChoiceKnowledge | None:
    normalized = _normalize_name(key)
    if not normalized:
        return None
    return _merged_choice_db("shop").get(normalized)


def lookup_potion_knowledge(name: str | None) -> ChoiceKnowledge | None:
    normalized = _resolve_alias("potion", name)
    if not normalized:
        return None
    return _merged_potion_db().get(normalized)


def lookup_boss_relic_knowledge(name: str | None) -> ChoiceKnowledge | None:
    normalized = _resolve_alias("boss_relic", name)
    if not normalized:
        return None
    return _merged_boss_relic_db().get(normalized)


def overlay_snapshot(*, max_entries: int = 8) -> dict[str, object]:
    overlay = load_kb_overlay()

    def _keys(section_name: str) -> list[str]:
        section = overlay.get(section_name, {})
        if not isinstance(section, dict):
            return []
        return sorted(str(key) for key in section.keys())[:max_entries]

    choices = overlay.get("choices", {})
    choice_summary: dict[str, list[str]] = {}
    if isinstance(choices, dict):
        for domain, items in choices.items():
            if isinstance(items, dict):
                choice_summary[str(domain)] = sorted(str(key) for key in items.keys())[:max_entries]
    aliases = overlay.get("aliases", {})
    alias_summary: dict[str, int] = {}
    if isinstance(aliases, dict):
        for entity, entity_aliases in aliases.items():
            alias_summary[str(entity)] = len(entity_aliases) if isinstance(entity_aliases, dict) else 0

    return {
        "path": str(active_kb_overlay_path()),
        "cards": _keys("cards"),
        "relics": _keys("relics"),
        "potions": _keys("potions"),
        "boss_relics": _keys("boss_relics"),
        "choices": choice_summary,
        "alias_counts": alias_summary,
    }


def apply_overlay_operations(operations: list[dict[str, object]], *, path: Path | None = None) -> dict[str, object]:
    overlay_path = Path(path) if path is not None else active_kb_overlay_path()
    overlay = load_kb_overlay(overlay_path, force_reload=True) if overlay_path.exists() else _empty_overlay()
    applied = 0
    ignored = 0

    for operation in operations:
        if not isinstance(operation, dict):
            ignored += 1
            continue
        target_type = _normalize_name(operation.get("target_type"))
        target_key = _normalize_name(operation.get("target_key"))
        payload = operation.get("payload")
        if target_type == "choice":
            domain, separator, key = target_key.partition(":")
            if not separator or not domain or not key:
                ignored += 1
                continue
            choices = overlay.setdefault("choices", {})
            if not isinstance(choices, dict):
                choices = {}
                overlay["choices"] = choices
            domain_bucket = choices.setdefault(domain, {})
            if not isinstance(domain_bucket, dict):
                domain_bucket = {}
                choices[domain] = domain_bucket
            domain_bucket[key] = _sanitize_choice_payload(key, payload)
            applied += 1
            continue
        if target_type == "card":
            if not target_key:
                ignored += 1
                continue
            cards = overlay.setdefault("cards", {})
            if not isinstance(cards, dict):
                cards = {}
                overlay["cards"] = cards
            cards[target_key] = _sanitize_card_payload(target_key, payload)
            applied += 1
            continue
        if target_type == "relic":
            if not target_key:
                ignored += 1
                continue
            relics = overlay.setdefault("relics", {})
            if not isinstance(relics, dict):
                relics = {}
                overlay["relics"] = relics
            relics[target_key] = _sanitize_relic_payload(target_key, payload)
            applied += 1
            continue
        if target_type == "potion":
            if not target_key:
                ignored += 1
                continue
            potions = overlay.setdefault("potions", {})
            if not isinstance(potions, dict):
                potions = {}
                overlay["potions"] = potions
            potions[target_key] = _sanitize_choice_payload(target_key, payload)
            applied += 1
            continue
        if target_type in {"boss relic", "boss_relic"}:
            if not target_key:
                ignored += 1
                continue
            boss_relics = overlay.setdefault("boss_relics", {})
            if not isinstance(boss_relics, dict):
                boss_relics = {}
                overlay["boss_relics"] = boss_relics
            boss_relics[target_key] = _sanitize_choice_payload(target_key, payload)
            applied += 1
            continue
        if target_type == "alias":
            entity, separator, alias = target_key.partition(":")
            entity = _normalize_entity_key(entity)
            canonical = _normalize_name(payload.get("canonical") if isinstance(payload, dict) else None)
            if entity not in {"card", "relic", "potion", "boss_relic"} or not separator or not alias or not canonical:
                ignored += 1
                continue
            aliases = overlay.setdefault("aliases", {})
            if not isinstance(aliases, dict):
                aliases = {}
                overlay["aliases"] = aliases
            entity_aliases = aliases.setdefault(entity, {})
            if not isinstance(entity_aliases, dict):
                entity_aliases = {}
                aliases[entity] = entity_aliases
            entity_aliases[alias] = canonical
            applied += 1
            continue
        ignored += 1

    save_kb_overlay(overlay, overlay_path)
    return {
        "applied": applied,
        "ignored": ignored,
        "path": str(overlay_path),
    }


def infer_deck_axes(names: Iterable[str], tag_counts: dict[str, int] | object) -> list[str]:
    lowered = {_normalize_name(name) for name in names}
    counts = dict(tag_counts) if isinstance(tag_counts, dict) else {}
    axes: list[str] = []
    if counts.get("strength", 0) > 0 or {"inflame", "limit break"} & lowered:
        axes.append("strength")
    if counts.get("exhaust", 0) > 0 or {"true grit", "offering", "second wind"} & lowered:
        axes.append("exhaust")
    if counts.get("block", 0) >= 2:
        axes.append("block")
    if counts.get("attack", 0) >= 2:
        axes.append("attack")
    if counts.get("scaling", 0) > 0 or {"inflame", "limit break"} & lowered:
        axes.append("scaling")
    if counts.get("draw", 0) > 0 or {"pommel strike", "shrug it off", "battle trance", "offering"} & lowered:
        axes.append("draw")
    return sorted(set(axes))
