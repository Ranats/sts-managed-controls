from __future__ import annotations

from collections import Counter

from sts_bot.knowledge import infer_deck_axes
from sts_bot.models import GameState, RunIntent


def deck_tag_counts(state: GameState) -> Counter[str]:
    return Counter(tag for card in state.deck for tag in card.tags)


def deck_axes(state: GameState) -> list[str]:
    counts = deck_tag_counts(state)
    names = [card.name for card in state.deck]
    return sorted(infer_deck_axes(names, counts))


def summarize_state(state: GameState) -> dict[str, object]:
    counts = deck_tag_counts(state)
    axes = deck_axes(state)
    hp_ratio = round((state.hp / state.max_hp), 3) if state.max_hp else 0.0
    known_enemy_hp = [enemy.hp for enemy in state.enemies if enemy.hp is not None]
    incoming_damage_values = [enemy.intent_damage for enemy in state.enemies if enemy.intent_damage is not None]
    incoming_damage = int(sum(incoming_damage_values))
    summary: dict[str, object] = {
        "screen": state.screen.value,
        "act": state.act,
        "floor": state.floor,
        "hp": state.hp,
        "max_hp": state.max_hp,
        "hp_ratio": hp_ratio,
        "energy": state.energy,
        "max_energy": state.max_energy,
        "block": state.block,
        "gold": state.gold,
        "deck_size": len(state.deck),
        "relic_count": len(state.relics),
        "player_power_count": len(state.player_powers),
        "enemy_count": len(state.enemies),
        "incoming_damage": incoming_damage,
        "lowest_enemy_hp": min(known_enemy_hp) if known_enemy_hp else None,
        "known_enemy_hp_total": sum(known_enemy_hp) if known_enemy_hp else None,
        "enemy_block_total": sum(enemy.block or 0 for enemy in state.enemies),
        "enemy_status_icons": sum(enemy.status_icon_count for enemy in state.enemies),
        "deck_axes": axes,
        "deck_tag_counts": dict(sorted(counts.items())),
        "state_tags": state.tags[:],
    }
    if state.run_intent is not None:
        summary["run_intent"] = summarize_run_intent(state.run_intent)
    return summary


def summarize_run_intent(intent: RunIntent) -> dict[str, object]:
    return {
        "deck_axes": intent.deck_axes[:],
        "short_term_survival_need": intent.short_term_survival_need,
        "long_term_direction": intent.long_term_direction,
        "elite_boss_risk_posture": intent.elite_boss_risk_posture,
    }


def summarize_state_text(summary: dict[str, object]) -> str:
    axes = summary.get("deck_axes") or []
    axes_text = ",".join(str(axis) for axis in axes) if axes else "-"
    lowest_enemy_hp = summary.get("lowest_enemy_hp")
    return (
        f"screen={summary.get('screen')} floor={summary.get('floor')} "
        f"hp={summary.get('hp')}/{summary.get('max_hp')} energy={summary.get('energy')} "
        f"gold={summary.get('gold')} enemies={summary.get('enemy_count')} "
        f"incoming={summary.get('incoming_damage')} "
        f"lowest_enemy_hp={lowest_enemy_hp if lowest_enemy_hp is not None else '-'} "
        f"deck_axes={axes_text}"
    )
