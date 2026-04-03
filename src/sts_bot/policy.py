from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import replace

from sts_bot.knowledge import (
    infer_deck_axes,
    lookup_boss_relic_knowledge,
    lookup_card_knowledge,
    lookup_event_choice,
    lookup_neow_choice,
    lookup_potion_knowledge,
    lookup_relic_knowledge,
    lookup_shop_choice,
)
from sts_bot.models import (
    ActionEvaluation,
    ActionKind,
    BattleCardObservation,
    BattleTargetKind,
    ChoiceContext,
    ChoiceDomain,
    ChoiceOption,
    GameAction,
    GameState,
    RunIntent,
    ScreenKind,
    StateSource,
)
from sts_bot.state_analysis import summarize_state


class Policy(ABC):
    @abstractmethod
    def choose_action(self, state: GameState, actions: list[GameAction]) -> GameAction:
        """Select one action from the available set."""


def _hp_ratio_if_known(state: GameState) -> float | None:
    if state.max_hp <= 0:
        return None
    return state.hp / state.max_hp


def infer_run_intent(state: GameState) -> RunIntent:
    counts = Counter(tag for card in state.deck for tag in card.tags)
    axes = sorted(infer_deck_axes([card.name for card in state.deck], counts))
    hp_ratio = _hp_ratio_if_known(state)
    incoming_damage = sum(enemy.intent_damage or 0 for enemy in state.enemies)
    if hp_ratio is not None and (hp_ratio < 0.25 or incoming_damage >= max(12, state.hp // 2)):
        survival_need = "critical"
    elif hp_ratio is not None and (hp_ratio < 0.45 or incoming_damage >= max(8, state.hp // 3)):
        survival_need = "stabilize"
    elif hp_ratio is not None and hp_ratio > 0.75 and incoming_damage == 0:
        survival_need = "greedy"
    else:
        survival_need = "stable"

    if "strength" in axes:
        direction = "strength"
    elif "exhaust" in axes:
        direction = "exhaust"
    elif counts.get("block", 0) >= counts.get("attack", 0) + 2:
        direction = "block"
    elif counts.get("attack", 0) >= counts.get("block", 0) + 2:
        direction = "damage"
    else:
        direction = "balanced"

    if (hp_ratio is not None and hp_ratio < 0.40) or (state.floor >= 10 and "strength" not in axes and "exhaust" not in axes):
        posture = "cautious"
    elif hp_ratio is not None and hp_ratio > 0.70 and ("strength" in axes or "exhaust" in axes or state.floor < 8):
        posture = "aggressive"
    else:
        posture = "balanced"

    return RunIntent(
        deck_axes=axes,
        short_term_survival_need=survival_need,
        long_term_direction=direction,
        elite_boss_risk_posture=posture,
    )


def attach_run_intent(state: GameState, intent: RunIntent | None) -> GameState:
    if intent is None:
        return state
    return replace(state, run_intent=intent)


def build_state_snapshot(state: GameState) -> dict[str, object]:
    snapshot = summarize_state(state)
    enemy_payload = [
        {
            "x": enemy.x,
            "hp": enemy.hp,
            "max_hp": enemy.max_hp,
            "intent_damage": enemy.intent_damage,
            "block": enemy.block,
            "powers": dict(enemy.powers),
            "status_icons": enemy.status_icon_count,
        }
        for enemy in state.enemies
    ]
    snapshot["incoming_intent"] = snapshot.get("incoming_damage", 0)
    snapshot["enemies"] = enemy_payload
    snapshot["player_block"] = state.block
    snapshot["player_powers"] = dict(state.player_powers)
    snapshot["relics"] = state.relics[:]
    snapshot["deck_cards"] = [card.name for card in state.deck]
    snapshot["state_source"] = state.state_source.value
    snapshot["metric_sources"] = dict(state.metric_sources)
    return snapshot


def _choice_text(action: GameAction) -> str:
    return " ".join(str(action.payload.get("option_text", "")).split()).strip()


def _choice_source_from_action(action: GameAction) -> StateSource:
    raw_source = action.payload.get("source")
    if isinstance(raw_source, StateSource):
        return raw_source
    if isinstance(raw_source, str):
        try:
            return StateSource(raw_source)
        except ValueError:
            return StateSource.OCR
    return StateSource.OCR


def _choice_candidate_name(action: GameAction, *keys: str) -> str | None:
    for key in keys:
        value = action.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    label = action.label
    for prefix in ("Pick ", "Take ", "Buy "):
        if label.startswith(prefix):
            return label[len(prefix):].strip()
    return None


def _choice_domain_from_action(state: GameState, action: GameAction) -> ChoiceDomain | None:
    explicit_domain = action.payload.get("choice_domain")
    if isinstance(explicit_domain, str):
        try:
            return ChoiceDomain(explicit_domain)
        except ValueError:
            pass
    if state.screen == ScreenKind.NEOW_CHOICE:
        return ChoiceDomain.NEOW
    if state.screen == ScreenKind.CARD_GRID:
        return ChoiceDomain.REWARD_CARD
    if state.screen == ScreenKind.EVENT:
        return ChoiceDomain.EVENT
    if state.screen == ScreenKind.REWARD_CARDS:
        return ChoiceDomain.REWARD_CARD
    if state.screen == ScreenKind.REWARD_RELIC:
        return ChoiceDomain.REWARD_RELIC
    if state.screen == ScreenKind.REWARD_POTION:
        return ChoiceDomain.REWARD_POTION
    if state.screen == ScreenKind.MAP:
        return ChoiceDomain.MAP_PATH
    if state.screen == ScreenKind.REST:
        return ChoiceDomain.REST_SITE
    if state.screen == ScreenKind.BOSS_RELIC:
        return ChoiceDomain.BOSS_RELIC
    if state.screen == ScreenKind.SHOP:
        item_type = str(action.payload.get("shop_item_type", "")).strip().lower()
        if item_type == "remove":
            return ChoiceDomain.SHOP_REMOVE
        if action.kind in {ActionKind.BUY, ActionKind.SKIP_REWARD, ActionKind.NAVIGATE}:
            return ChoiceDomain.SHOP_PURCHASE
    if action.kind == ActionKind.TAKE_POTION:
        return ChoiceDomain.REWARD_POTION
    if action.kind == ActionKind.TAKE_RELIC:
        boss_relic_name = _choice_candidate_name(action, "boss_relic", "relic")
        if state.screen in {ScreenKind.BOSS, ScreenKind.BOSS_RELIC} and lookup_boss_relic_knowledge(boss_relic_name) is not None:
            return ChoiceDomain.BOSS_RELIC
        return ChoiceDomain.REWARD_RELIC
    if action.kind == ActionKind.PICK_CARD:
        return ChoiceDomain.REWARD_CARD
    if action.kind == ActionKind.CHOOSE_PATH:
        return ChoiceDomain.MAP_PATH
    if action.kind in {ActionKind.REST, ActionKind.SMITH}:
        return ChoiceDomain.REST_SITE
    return None


def _infer_choice_option_keys(state: GameState, action: GameAction, domain: ChoiceDomain) -> list[str]:
    keys: list[str] = []
    payload_key = action.payload.get("option_key")
    if isinstance(payload_key, str) and payload_key:
        keys.append(payload_key)
    payload_keys = action.payload.get("kb_keys")
    if isinstance(payload_keys, list):
        for item in payload_keys:
            if isinstance(item, str) and item and item not in keys:
                keys.append(item)

    option_text = _choice_text(action).lower()
    action_tags = {str(tag) for tag in action.tags}

    def add(key: str) -> None:
        if key not in keys:
            keys.append(key)

    if domain == ChoiceDomain.NEOW:
        if not option_text and action.payload.get("target") == "neow_default":
            add("default")
        if "transform" in option_text or "transform" in action_tags:
            add("transform")
        if "remove" in option_text or "remove" in action_tags:
            add("remove")
        if "gold" in option_text or "gold" in action_tags:
            add("gold")
        if "rare" in option_text:
            add("rare")
        if "common relic" in option_text or ("relic" in option_text and "common" in option_text):
            add("common_relic")
        if "common card" in option_text or ("card" in option_text and "common" in option_text):
            add("common_card")
        if "max hp" in option_text:
            add("max_hp")
        if "upgrade" in option_text:
            add("upgrade")
        if "heal" in option_text or "restore" in option_text:
            add("heal")
        if any(token in option_text for token in ("curse", "clumsy", "wound", "regret", "decay")):
            add("curse")
        if ("lose" in option_text and "hp" in option_text) or " hp" in option_text or "hp_cost" in action_tags:
            add("hp_cost")
        if "add" in option_text and "deck" in option_text:
            add("card_add")
    elif domain == ChoiceDomain.EVENT:
        if "heal" in option_text or "rest" in option_text or "recover" in option_text or "heal" in action_tags:
            add("heal")
        if "gold" in option_text or "gold" in action_tags:
            add("gold")
        if "remove" in option_text or "purge" in option_text:
            add("remove")
        if "upgrade" in option_text or "smith" in option_text:
            add("upgrade")
        if "fight" in option_text or "combat" in option_text or "combat" in action_tags:
            add("fight")
        if "relic" in option_text:
            add("relic")
        if "shop" in option_text:
            add("shop")
        if "card" in option_text:
            add("card")
        if any(token in option_text for token in ("curse", "clumsy", "wound", "regret", "decay")):
            add("curse")
        if ("lose" in option_text and "hp" in option_text) or "hp_cost" in action_tags:
            add("hp_cost")
    elif domain in {ChoiceDomain.SHOP_PURCHASE, ChoiceDomain.SHOP_REMOVE}:
        item_type = str(action.payload.get("shop_item_type", "")).strip().lower()
        if item_type:
            add(item_type)
        if action.label == "Leave shop":
            add("leave")
        if action.kind == ActionKind.SKIP_REWARD:
            add("skip")
    return keys


def _build_choice_option(state: GameState, action: GameAction) -> tuple[ChoiceDomain | None, ChoiceOption]:
    domain = _choice_domain_from_action(state, action)
    option_text = _choice_text(action)
    candidate_name = _choice_candidate_name(action, "card", "relic", "potion", "boss_relic")
    label = action.label
    if candidate_name and candidate_name not in label and not candidate_name.startswith("slot_"):
        label = f"{label} ({candidate_name})"
    kb_keys = _infer_choice_option_keys(state, action, domain) if domain is not None else []
    return domain, ChoiceOption(
        option_id=str(action.payload.get("option_id", action.label)),
        label=label,
        text=option_text,
        tags=action.tags[:],
        payload=dict(action.payload),
        source=_choice_source_from_action(action),
        confidence=None,
        kb_keys=kb_keys,
        upside_tags=action.tags[:],
        downside_tags=[],
    )


def _build_choice_context(
    state: GameState,
    actions: list[GameAction],
    domain: ChoiceDomain,
    option_cache: dict[str, ChoiceOption] | None = None,
) -> ChoiceContext:
    options: list[ChoiceOption] = []
    for action in actions:
        cached = option_cache.get(action.label) if option_cache is not None else None
        if cached is not None:
            options.append(cached)
            continue
        _action_domain, option = _build_choice_option(state, action)
        options.append(option)
    option_sources = {option.source for option in options}
    if not option_sources:
        option_source = StateSource.OCR
    elif len(option_sources) == 1:
        option_source = option_sources.pop()
    else:
        option_source = StateSource.HYBRID
    return ChoiceContext(
        domain=domain,
        screen=state.screen,
        character=state.character,
        act=state.act,
        floor=state.floor,
        ascension=None,
        hp=state.hp,
        max_hp=state.max_hp,
        gold=state.gold,
        energy=state.energy,
        deck_names=[card.name for card in state.deck],
        relics=state.relics[:],
        potion_names=[],
        run_intent=state.run_intent,
        build_preset=None,
        options=options,
        option_source=option_source,
        notes=[f"state_source:{state.state_source.value}"],
    )


def build_choice_context(state: GameState, actions: list[GameAction]) -> ChoiceContext | None:
    option_cache: dict[str, ChoiceOption] = {}
    domains: list[ChoiceDomain] = []
    for action in actions:
        domain, option = _build_choice_option(state, action)
        option_cache[action.label] = option
        if domain is not None and domain not in domains:
            domains.append(domain)
    if not domains:
        return None
    if len(domains) == 1:
        return _build_choice_context(state, actions, domains[0], option_cache=option_cache)
    return _build_choice_context(state, actions, domains[0], option_cache=option_cache)


def build_choice_context_snapshot(state: GameState, actions: list[GameAction]) -> dict[str, object] | None:
    context = build_choice_context(state, actions)
    if context is None:
        return None
    return {
        "domain": context.domain.value,
        "screen": context.screen.value,
        "character": context.character,
        "act": context.act,
        "floor": context.floor,
        "ascension": context.ascension,
        "hp": context.hp,
        "max_hp": context.max_hp,
        "gold": context.gold,
        "energy": context.energy,
        "deck_names": context.deck_names[:],
        "relics": context.relics[:],
        "potion_names": context.potion_names[:],
        "run_intent": {
            "deck_axes": context.run_intent.deck_axes[:],
            "short_term_survival_need": context.run_intent.short_term_survival_need,
            "long_term_direction": context.run_intent.long_term_direction,
            "elite_boss_risk_posture": context.run_intent.elite_boss_risk_posture,
        } if context.run_intent is not None else None,
        "option_source": context.option_source.value,
        "notes": context.notes[:],
        "options": [
            {
                "option_id": option.option_id,
                "label": option.label,
                "text": option.text,
                "tags": option.tags[:],
                "payload": dict(option.payload),
                "source": option.source.value,
                "confidence": option.confidence,
                "kb_keys": option.kb_keys[:],
                "upside_tags": option.upside_tags[:],
                "downside_tags": option.downside_tags[:],
            }
            for option in context.options
        ],
    }


def _strategic_choice_screen(screen: ScreenKind) -> bool:
    return screen in {
        ScreenKind.NEOW_CHOICE,
        ScreenKind.CARD_GRID,
        ScreenKind.EVENT,
        ScreenKind.REWARD_CARDS,
        ScreenKind.REWARD_RELIC,
        ScreenKind.REWARD_POTION,
        ScreenKind.MAP,
        ScreenKind.REST,
        ScreenKind.SHOP,
        ScreenKind.BOSS_RELIC,
        ScreenKind.BOSS,
    }


def _deck_tag_counts_from_context(context: ChoiceContext) -> Counter[str]:
    counts: Counter[str] = Counter()
    for name in context.deck_names:
        knowledge = lookup_card_knowledge(context.character, name)
        if knowledge is None:
            continue
        counts.update(knowledge.tags)
    return counts


def _infer_axes_from_context(context: ChoiceContext) -> set[str]:
    if context.run_intent is not None:
        return set(context.run_intent.deck_axes)
    counts = _deck_tag_counts_from_context(context)
    return infer_deck_axes(context.deck_names, counts)


def _score_choice_option(context: ChoiceContext, option: ChoiceOption) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    hp_ratio = None if context.max_hp <= 0 else context.hp / context.max_hp
    deck_tag_counts = _deck_tag_counts_from_context(context)
    deck_axes = _infer_axes_from_context(context)
    run_intent = context.run_intent or RunIntent(deck_axes=sorted(deck_axes))

    if context.domain == ChoiceDomain.NEOW:
        option_text = option.text.lower()
        if option.payload.get("target") == "neow_default":
            score += 1.0
            reasons.append("neow_text_unreliable")
        for key in option.kb_keys:
            entry = lookup_neow_choice(key)
            if entry is None:
                continue
            score += entry.base_score
            if key == "transform":
                reasons.extend(["neow_prefers_cleanup", "neow_transform_upgrade"])
            elif key == "remove":
                reasons.extend(["neow_prefers_cleanup", "neow_remove_upgrade"])
            elif key == "gold":
                reasons.append("neow_values_gold")
            elif key == "rare":
                reasons.append("neow_values_rare")
            elif key == "hp_cost":
                reasons.append("neow_hp_cost_penalty")
            elif key == "card_add":
                reasons.append("neow_card_addition_penalty")
            elif key == "curse":
                reasons.append("neow_curse_penalty")
        if hp_ratio is not None and hp_ratio < 0.45 and "hp_cost" in option.kb_keys:
            score -= 3.0
            reasons.append("low_hp_avoid_hp_payment")
        if hp_ratio is not None and hp_ratio < 0.30 and "hp_cost" in option.kb_keys:
            score -= 4.0
            reasons.append("critical_hp_reject_hp_payment")
        if "rare" in option.kb_keys and "curse" in option.kb_keys:
            score -= 2.0
            reasons.append("neow_curse_penalty")
        if "max_hp" in option.kb_keys and hp_ratio is not None and hp_ratio < 0.55:
            score += 1.0
            reasons.append("low_hp_prioritize_healing")
        if not option_text and not reasons:
            reasons.append("neow_text_unreliable")
        return score, reasons

    if context.domain == ChoiceDomain.EVENT:
        for key in option.kb_keys:
            entry = lookup_event_choice(key)
            if entry is not None:
                score += entry.base_score
            if key == "heal" and hp_ratio is not None and hp_ratio < 0.45:
                score += 3.0
                reasons.append("low_hp_prioritize_healing")
            elif key == "hp_cost":
                if hp_ratio is not None and hp_ratio < 0.45:
                    score -= 3.0
                    reasons.append("low_hp_avoid_hp_payment")
                if hp_ratio is not None and hp_ratio < 0.30:
                    score -= 4.0
                    reasons.append("critical_hp_reject_hp_payment")
            elif key == "remove":
                score += 2.0
                reasons.append("event_cleanup")
            elif key == "upgrade":
                score += 1.5
                reasons.append("event_upgrade_value")
            elif key == "relic":
                score += 1.5
                reasons.append("event_relic_upside")
            elif key == "curse":
                score -= 4.0
                reasons.append("event_curse_penalty")
            elif key == "fight" and hp_ratio is not None:
                if hp_ratio > 0.70:
                    score += 1.5
                    reasons.append("healthy_enough_for_elite")
                elif hp_ratio < 0.40:
                    score -= 2.0
                    reasons.append("low_hp_take_safe_path")
        return score, reasons

    if context.domain == ChoiceDomain.REWARD_CARD:
        if option.payload.get("card") == "slot_1" or option.payload.get("card") == "slot_2" or option.payload.get("card") == "slot_3":
            candidate_name = _choice_candidate_name(
                GameAction(ActionKind.PICK_CARD, option.label, option.payload, option.tags),
                "card",
            )
        else:
            candidate_name = _choice_candidate_name(GameAction(ActionKind.PICK_CARD, option.label, option.payload, option.tags), "card")
        if "skip" in option.tags or option.payload.get("target") == "skip":
            if deck_tag_counts["scaling"] > 0 and deck_tag_counts["block"] > 0:
                score += 1.0
                reasons.append("deck_already_balanced")
            if run_intent.short_term_survival_need == "critical":
                score -= 2.0
                reasons.append("need_help_now_not_skip")
            return score, reasons
        score += 1.0
        reasons.append("prefer_take_over_skip")
        card_knowledge = lookup_card_knowledge(context.character, candidate_name)
        if card_knowledge is not None:
            score += card_knowledge.base_score
            reasons.append(f"kb_card:{card_knowledge.name}")
            for axis in card_knowledge.prefers:
                if axis in deck_axes:
                    score += 2.0
                    reasons.append(f"synergy:{axis}")
            for axis in card_knowledge.requires:
                if axis not in deck_axes:
                    score -= 4.0
                    reasons.append(f"missing_axis:{axis}")
            for axis in card_knowledge.avoids_without:
                if axis not in deck_axes:
                    score -= 6.0
                    reasons.append(f"awkward_without:{axis}")
            if run_intent.long_term_direction in card_knowledge.tags:
                score += 1.5
                reasons.append(f"fits_plan:{run_intent.long_term_direction}")
        if context.screen == ScreenKind.CARD_GRID:
            if "attack" in option.tags:
                score += 5.0
                reasons.append("transform_strike_before_defend")
            if "starter_core" in option.tags:
                score -= 6.0
                reasons.append("avoid_transforming_bash_early")
        if deck_tag_counts["attack"] >= deck_tag_counts["block"] + 2 and "block" in option.tags:
            score += 3.0
            reasons.append("deck_needs_block")
        if deck_tag_counts["block"] >= deck_tag_counts["attack"] + 2 and "attack" in option.tags:
            score += 2.0
            reasons.append("deck_can_add_attack")
        if context.floor >= 12 and deck_tag_counts["scaling"] == 0 and "scaling" in option.tags:
            score += 4.0
            reasons.append("need_scaling_for_late_game")
        if hp_ratio is not None and hp_ratio < 0.45 and "block" in option.tags:
            score += 2.5
            reasons.append("low_hp_values_block")
        if hp_ratio is not None and hp_ratio < 0.30 and "block" in option.tags:
            score += 2.0
            reasons.append("critical_hp_extra_block_value")
        if hp_ratio is not None and hp_ratio < 0.35 and "attack" in option.tags:
            score -= 1.0
            reasons.append("low_hp_avoid_extra_attack")
        return score, reasons

    if context.domain == ChoiceDomain.REWARD_RELIC:
        if option.payload.get("target") == "skip" or "skip" in option.tags:
            return score, reasons
        score += 1.0
        reasons.append("prefer_take_over_skip")
        relic_name = _choice_candidate_name(GameAction(ActionKind.TAKE_RELIC, option.label, option.payload, option.tags), "relic")
        relic_knowledge = lookup_relic_knowledge(context.character, relic_name)
        if relic_knowledge is not None:
            score += relic_knowledge.base_score
            reasons.append(f"kb_relic:{relic_knowledge.name}")
            for axis in relic_knowledge.prefers:
                if axis in deck_axes:
                    score += 1.5
                    reasons.append(f"relic_synergy:{axis}")
            if hp_ratio is not None and hp_ratio < 0.45 and "heal" in relic_knowledge.tags:
                score += 2.0
                reasons.append("low_hp_values_sustain_relic")
        else:
            score += 2.0
            reasons.append("unknown_relic_reasonable_default")
        return score, reasons

    if context.domain == ChoiceDomain.REWARD_POTION:
        if option.payload.get("target") == "skip" or "skip" in option.tags:
            if len(context.potion_names) >= 2:
                score += 1.0
                reasons.append("reward_skip_if_slots_full")
            return score, reasons
        if len(context.potion_names) >= 2:
            score -= 4.0
            reasons.append("potion_slots_full")
        potion_name = _choice_candidate_name(GameAction(ActionKind.TAKE_POTION, option.label, option.payload, option.tags), "potion")
        potion_knowledge = lookup_potion_knowledge(potion_name)
        if potion_knowledge is not None:
            score += potion_knowledge.base_score
            reasons.append(f"kb_potion:{potion_knowledge.name}")
            for axis in potion_knowledge.prefers:
                if axis in deck_axes:
                    score += 1.0
                    reasons.append(f"synergy:{axis}")
            if hp_ratio is not None and hp_ratio < 0.45 and any(tag in potion_knowledge.tags for tag in ("block", "survival")):
                score += 2.0
                reasons.append("low_hp_values_potion")
        else:
            score += 1.5
            reasons.append("unknown_potion_reasonable_default")
        return score, reasons

    if context.domain in {ChoiceDomain.SHOP_PURCHASE, ChoiceDomain.SHOP_REMOVE}:
        if option.label == "Close detail popup" or "popup" in option.tags:
            score -= 3.0
            reasons.append("shop_popup_close")
            return score, reasons
        if option.label == "Leave shop":
            score -= 2.0
            reasons.append("shop_leave_penalty")
            return score, reasons
        price_value = option.payload.get("price")
        price = int(price_value) if isinstance(price_value, int) else None
        if price is not None and price > context.gold:
            return -999.0, ["shop_unaffordable"]
        item_type = str(option.payload.get("shop_item_type", "")).strip().lower()
        shop_entry = lookup_shop_choice(item_type or ("remove" if context.domain == ChoiceDomain.SHOP_REMOVE else ""))
        if shop_entry is not None:
            score += shop_entry.base_score
        if item_type == "card":
            card_name = _choice_candidate_name(GameAction(ActionKind.BUY, option.label, option.payload, option.tags), "card")
            card_knowledge = lookup_card_knowledge(context.character, card_name)
            if card_knowledge is not None:
                score += card_knowledge.base_score
                reasons.append(f"kb_card:{card_knowledge.name}")
        elif item_type == "relic":
            relic_name = _choice_candidate_name(GameAction(ActionKind.BUY, option.label, option.payload, option.tags), "relic")
            relic_knowledge = lookup_relic_knowledge(context.character, relic_name)
            if relic_knowledge is not None:
                score += relic_knowledge.base_score
                reasons.append(f"kb_relic:{relic_knowledge.name}")
            else:
                score += 2.0
        elif item_type == "potion":
            potion_name = _choice_candidate_name(GameAction(ActionKind.BUY, option.label, option.payload, option.tags), "potion")
            potion_knowledge = lookup_potion_knowledge(potion_name)
            if potion_knowledge is not None:
                score += potion_knowledge.base_score
                reasons.append(f"kb_potion:{potion_knowledge.name}")
        elif item_type == "remove":
            score += 4.0
            reasons.append("event_cleanup")
        if price is not None:
            if price <= max(65, context.gold // 2):
                score += 1.0
                reasons.append("shop_price_good")
            score -= price / 55.0
            reasons.append("shop_price_penalty")
        return score, reasons

    if context.domain == ChoiceDomain.MAP_PATH:
        if hp_ratio is not None and hp_ratio < 0.45 and "safe" in option.tags:
            score += 4.0
            reasons.append("low_hp_take_safe_path")
        if hp_ratio is not None and hp_ratio < 0.30 and "safe" in option.tags:
            score += 3.0
            reasons.append("critical_hp_take_safest_path")
        if hp_ratio is not None and hp_ratio > 0.70 and "elite" in option.tags:
            score += 3.0
            reasons.append("healthy_enough_for_elite")
        if hp_ratio is not None and hp_ratio < 0.45 and "elite" in option.tags:
            score -= 4.0
            reasons.append("low_hp_avoid_elite")
        if context.floor >= 30 and "elite" in option.tags and hp_ratio is not None and hp_ratio < 0.60:
            score -= 3.0
            reasons.append("late_elite_risk")
        if "elite" in option.tags and context.floor >= 10 and "strength" not in deck_axes and "exhaust" not in deck_axes:
            score -= 1.5
            reasons.append("no_scaling_yet_for_elite_path")
        if run_intent.elite_boss_risk_posture == "aggressive" and "elite" in option.tags:
            score += 2.0
            reasons.append("aggressive_posture_accepts_elite")
        if run_intent.elite_boss_risk_posture == "cautious" and "safe" in option.tags:
            score += 2.0
            reasons.append("cautious_posture_prefers_safe")
        return score, reasons

    if context.domain == ChoiceDomain.REST_SITE:
        if option.payload.get("target") == "leave" or option.label.lower().startswith("leave"):
            return score, reasons
        if option.payload.get("rest_action") == "rest" or "heal" in option.tags or option.label == "Rest":
            if hp_ratio is not None and hp_ratio < 0.55:
                score += 4.0
                reasons.append("rest_to_stabilize")
            else:
                score -= 2.0
                reasons.append("healthy_enough_to_upgrade")
        if option.payload.get("rest_action") == "smith" or option.label == "Smith":
            if hp_ratio is not None and hp_ratio >= 0.50:
                score += 4.0
                reasons.append("upgrade_when_stable")
            elif hp_ratio is not None and hp_ratio < 0.35:
                score -= 3.0
                reasons.append("low_hp_prioritize_healing")
        return score, reasons

    if context.domain == ChoiceDomain.BOSS_RELIC:
        if option.payload.get("target") == "skip" or "skip" in option.tags:
            score -= 1.5
            reasons.append("prefer_take_over_skip")
            return score, reasons
        relic_name = _choice_candidate_name(GameAction(ActionKind.TAKE_RELIC, option.label, option.payload, option.tags), "boss_relic", "relic")
        relic_knowledge = lookup_boss_relic_knowledge(relic_name)
        if relic_knowledge is not None:
            score += relic_knowledge.base_score
            reasons.append(f"kb_boss_relic:{relic_knowledge.name}")
            if hp_ratio is not None and hp_ratio < 0.50 and any(tag in relic_knowledge.downside_tags for tag in ("no_rest_heal", "enemy_strength")):
                score -= 3.0
                reasons.append("boss_relic_sustain_risk")
            if context.potion_names and "no_more_potions" in relic_knowledge.downside_tags:
                score -= 2.0
                reasons.append("boss_relic_blocks_potions")
            if run_intent.elite_boss_risk_posture == "aggressive" and "snowball" in relic_knowledge.tags:
                score += 1.0
                reasons.append("boss_relic_aggressive_snowball")
        else:
            score += 2.5
            reasons.append("unknown_boss_relic_reasonable_default")
        return score, reasons

    return score, reasons


def summarize_choice(
    state: GameState,
    action: GameAction,
    evaluations: list[ActionEvaluation] | None = None,
) -> str:
    snapshot = build_state_snapshot(state)
    chosen_evaluation = None
    if evaluations:
        chosen_evaluation = next((item for item in evaluations if item.action_label == action.label), None)
    hp_ratio = _hp_ratio_if_known(state)
    hp_text = "unknown" if hp_ratio is None else f"{snapshot['hp']}/{snapshot['max_hp']}"
    summary_parts = [
        f"screen={snapshot['screen']}",
        f"floor={snapshot['floor']}",
        f"hp={hp_text}",
        f"energy={snapshot['energy']}",
    ]
    state_source = str(snapshot.get("state_source") or "").strip()
    if state_source:
        summary_parts.append(f"source={state_source}")
    metric_source_text = _format_metric_sources(snapshot.get("metric_sources"))
    if metric_source_text:
        summary_parts.append(f"metrics={metric_source_text}")
    if snapshot["enemy_count"]:
        summary_parts.append(
            f"enemies={snapshot['enemy_count']}"
        )
        summary_parts.append(f"incoming={snapshot['incoming_intent']}")
    if snapshot["deck_axes"]:
        summary_parts.append(f"axes={','.join(str(axis) for axis in snapshot['deck_axes'])}")
    intent = snapshot.get("run_intent") or {}
    if isinstance(intent, dict) and intent.get("long_term_direction"):
        summary_parts.append(f"intent={intent['long_term_direction']}/{intent.get('short_term_survival_need', '-')}")
    reason_text = _human_reason_summary(
        state,
        action,
        chosen_evaluation.reasons if chosen_evaluation is not None else [],
        action_count=len(evaluations or []),
        hp_known=hp_ratio is not None,
    )
    summary = " | ".join(summary_parts)
    alternative_text = ""
    if evaluations:
        ranked = sorted(evaluations, key=lambda item: item.score, reverse=True)
        runner_up = next((item for item in ranked if item.action_label != action.label), None)
        if chosen_evaluation is not None and runner_up is not None:
            delta = chosen_evaluation.score - runner_up.score
            alternative_text = f" over {runner_up.action_label} by {delta:.1f}"
    if reason_text:
        return f"{summary} -> {_describe_action_for_log(state, action)}{alternative_text} because {reason_text}"
    return f"{summary} -> {_describe_action_for_log(state, action)}{alternative_text}"


def _describe_action_for_log(state: GameState, action: GameAction) -> str:
    option_text = str(action.payload.get("option_text", "")).strip()
    if option_text:
        return f"{action.label} ({option_text})"
    if state.screen == ScreenKind.CARD_GRID:
        slot_value = str(action.payload.get("card", ""))
        if slot_value in {"slot_1", "slot_2", "slot_3", "slot_4", "slot_5"}:
            return f"{action.label} (starter Strike)"
        if slot_value in {"slot_6", "slot_7", "slot_8", "slot_9"}:
            return f"{action.label} (starter Defend)"
        if slot_value == "slot_10":
            return f"{action.label} (starter Bash)"
    named_value = _choice_candidate_name(action, "boss_relic", "relic", "card", "potion")
    if named_value and not named_value.startswith("slot_"):
        price = action.payload.get("price")
        if isinstance(price, int):
            return f"{action.label} ({named_value}, {price}g)"
        if named_value not in action.label:
            return f"{action.label} ({named_value})"
    return action.label


def _format_metric_sources(metric_sources: object) -> str:
    if not isinstance(metric_sources, dict):
        return ""
    preferred_order = ("hp", "max_hp", "energy", "gold", "floor", "act", "ascension")
    notable = [
        f"{name}:{metric_sources[name]}"
        for name in preferred_order
        if isinstance(metric_sources.get(name), str) and metric_sources[name] != "ocr"
    ]
    if notable:
        return ",".join(notable[:4])
    distinct_sources = {
        str(source)
        for source in metric_sources.values()
        if isinstance(source, str) and source
    }
    if len(distinct_sources) <= 1:
        return ""
    compact = [
        f"{name}:{metric_sources[name]}"
        for name in preferred_order
        if isinstance(metric_sources.get(name), str)
    ]
    return ",".join(compact[:4])


def _human_reason_summary(
    state: GameState,
    action: GameAction,
    reasons: list[str],
    *,
    action_count: int,
    hp_known: bool,
) -> str:
    if state.screen == ScreenKind.NEOW_CHOICE and action.payload.get("target") == "neow_default" and action_count <= 1:
        return "Neow 3択の比較ロジックが未実装なので、既定の上段を fallback として使う"
    if state.screen == ScreenKind.NEOW_CHOICE and action_count <= 1 and not reasons:
        return "現状は Neow 3択の比較ロジックが未実装なので、校正済みの既定選択で先へ進める"
    if state.screen == ScreenKind.NEOW_DIALOG:
        return "Neow ダイアログを閉じて次の画面へ進める"

    phrases: list[str] = []
    for reason in reasons:
        phrase = _explain_reason_code(state, action, reason, hp_known=hp_known)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= 3:
            break
    if not phrases and action.tags:
        phrases = [_explain_action_tags(action.tags)]
    return "; ".join(phrase for phrase in phrases if phrase)


def _explain_reason_code(
    state: GameState,
    action: GameAction,
    reason: str,
    *,
    hp_known: bool,
) -> str:
    if reason == "transform_strike_before_defend":
        return "Neow の変化では、序盤に価値が低い starter Strike を先に変える"
    if reason == "neow_prefers_cleanup":
        return "序盤の Neow では deck 圧縮や starter 改善の価値が高い"
    if reason == "neow_transform_upgrade":
        return "starter をランダム上振れ札に変えられる"
    if reason == "neow_remove_upgrade":
        return "不要な starter を直接減らせる"
    if reason == "neow_values_gold":
        return "序盤 gold は shop とルートの自由度を上げる"
    if reason == "neow_values_rare":
        return "序盤 rare は build の方向を早く決めやすい"
    if reason == "neow_hp_cost_penalty" and hp_known:
        return "HP を払う選択肢なので今は重く見ない"
    if reason == "neow_card_addition_penalty":
        return "序盤から微妙なカードを増やすより deck を絞りたい"
    if reason == "neow_curse_penalty":
        return "curse 系の downside は序盤の安定を崩しやすい"
    if reason == "neow_text_unreliable":
        return "選択肢 OCR が不安定なので、既定の上段を fallback として使う"
    if reason == "avoid_transforming_bash_early":
        return "Bash は序盤の要なので、変化対象から外す"
    if reason == "deck_needs_block":
        return "現在のデッキは防御が薄いので、block 側を優先する"
    if reason == "deck_can_add_attack":
        return "現在のデッキは防御寄りなので、attack を足しやすい"
    if reason == "need_scaling_for_late_game":
        return "Act 後半に向けた scaling が不足している"
    if reason == "low_hp_values_block" and hp_known:
        return "HP が低めなので、block の価値が上がっている"
    if reason == "critical_hp_extra_block_value" and hp_known:
        return "HP が危険域なので、防御札をさらに重く見る"
    if reason == "low_hp_avoid_extra_attack" and hp_known:
        return "HP が低いので、純粋な攻撃札の優先度を少し下げる"
    if reason == "low_hp_prioritize_healing" and hp_known:
        return "HP が低いので、回復を優先する"
    if reason == "low_hp_avoid_hp_payment" and hp_known:
        return "HP が低いので、HP 支払いイベントを避ける"
    if reason == "critical_hp_reject_hp_payment" and hp_known:
        return "HP が危険域なので、HP 支払いは却下する"
    if reason == "healthy_enough_for_elite":
        return "現在の体力なら elite を踏める余裕がある"
    if reason == "low_hp_take_safe_path" and hp_known:
        return "HP が低いので safer path を取る"
    if reason == "critical_hp_take_safest_path" and hp_known:
        return "HP が危険域なので最も安全な経路を選ぶ"
    if reason == "low_hp_avoid_elite" and hp_known:
        return "HP が低いので elite は避ける"
    if reason == "aggressive_posture_accepts_elite":
        return "現在の run posture は攻め寄りなので elite を許容する"
    if reason == "cautious_posture_prefers_safe":
        return "現在の run posture は慎重なので safe path を優先する"
    if reason == "prefer_take_over_skip" and state.screen != ScreenKind.CARD_GRID:
        return "skip よりも deck を前進させる選択を優先する"
    if reason == "rest_to_stabilize":
        return "次の戦闘に備えて HP を立て直す"
    if reason == "upgrade_when_stable":
        return "HP に余裕があるので upgrade を取る"
    if reason == "prefer_playing_cards_before_end_turn":
        return "まだ手番で取れる価値があるので、End Turn よりカード使用を優先する"
    if reason == "incoming_damage_pressure":
        return "今回の被ダメ圧が高い"
    if reason == "enemy_near_lethal":
        return "倒し切りが見える敵がいるので押し切りたい"
    if reason == "unsafe_to_pass_under_pressure":
        return "この盤面でそのまま渡すのは危険"
    if reason.startswith("kb_card:"):
        return f"カード知識が {reason.split(':', 1)[1]} を高く評価している"
    if reason.startswith("kb_relic:"):
        return f"レリック知識が {reason.split(':', 1)[1]} を高く評価している"
    if reason.startswith("synergy:"):
        return f"{reason.split(':', 1)[1]} 軸と相性が良い"
    if reason.startswith("relic_synergy:"):
        return f"{reason.split(':', 1)[1]} 軸と噛み合うレリック"
    if reason.startswith("fits_plan:"):
        return f"現在の build plan ({reason.split(':', 1)[1]}) に合う"
    if reason.startswith("missing_axis:"):
        return f"{reason.split(':', 1)[1]} 軸がまだ無く、今は弱い"
    if reason.startswith("awkward_without:"):
        return f"{reason.split(':', 1)[1]} 補助が無いと扱いづらい"
    if reason.startswith("kb_potion:"):
        return f"ポーション知識が {reason.split(':', 1)[1]} を高く評価している"
    if reason.startswith("kb_boss_relic:"):
        return f"boss relic 知識が {reason.split(':', 1)[1]} を高く評価している"
    if reason == "event_cleanup":
        return "event の選択で deck をきれいにできる"
    if reason == "event_upgrade_value":
        return "event を無難な tempo 変換に使える"
    if reason == "event_relic_upside":
        return "event 経由で relic の上振れを取りにいける"
    if reason == "event_curse_penalty":
        return "event line に curse の長期コストがある"
    if reason == "shop_unaffordable":
        return "現在の gold ではその shop item を買えない"
    if reason == "shop_price_good":
        return "今の gold に対して価格効率が良い"
    if reason == "shop_price_penalty":
        return "shop price を払うぶん純価値は落ちる"
    if reason == "shop_leave_penalty":
        return "shop で取れる価値を残して退出する"
    if reason == "shop_popup_close":
        return "popup を閉じるだけで価値獲得には直結しない"
    if reason == "reward_skip_if_slots_full":
        return "potion slot が埋まっているので skip も許容できる"
    if reason == "potion_slots_full":
        return "potion slot が埋まっていて取り回しが悪い"
    if reason == "low_hp_values_potion":
        return "HP が低いので defensive potion の価値が上がる"
    if reason == "boss_relic_sustain_risk":
        return "boss relic の downside が今の sustain と噛み合わない"
    if reason == "boss_relic_blocks_potions":
        return "boss relic の downside が potion 運用を制限する"
    if reason == "boss_relic_aggressive_snowball":
        return "攻め寄りの posture と snowball relic が噛み合う"
    if reason == "unknown_relic_reasonable_default":
        return "未知の relic だが報酬としては無難に前向き"
    if reason == "unknown_potion_reasonable_default":
        return "未知の potion だが報酬としては無難に前向き"
    if reason == "unknown_boss_relic_reasonable_default":
        return "未知の boss relic だが基礎価値は見込める"
    if reason.startswith("tag:"):
        return _explain_action_tags([reason.split(':', 1)[1]])
    return reason.replace("_", " ")


def _explain_action_tags(tags: list[str]) -> str:
    phrases: list[str] = []
    if "block" in tags:
        phrases.append("防御寄りの選択")
    if "attack" in tags:
        phrases.append("攻撃寄りの選択")
    if "elite" in tags:
        phrases.append("elite を踏む経路")
    if "safe" in tags:
        phrases.append("安全寄りの経路")
    if "confirm" in tags:
        phrases.append("確定操作")
    if "dialog" in tags:
        phrases.append("ダイアログを進める操作")
    if "start" in tags and "neow" in tags:
        phrases.append("run 開始を進める Neow 選択")
    if not phrases:
        phrases = [", ".join(tags[:3])] if tags else []
    return "; ".join(phrases)


def _power_amount(powers: dict[str, int], name: str) -> int:
    value = powers.get(name)
    return value if isinstance(value, int) else 0


def _effective_attack_damage(state: GameState, base_damage: int, enemy: object | None) -> tuple[int, list[str]]:
    if base_damage <= 0:
        return 0, []
    reasons: list[str] = []
    damage = float(base_damage)
    strength = _power_amount(state.player_powers, "Strength")
    if strength:
        damage += strength
        reasons.append(f"strength:{strength}")
    weak = _power_amount(state.player_powers, "Weak")
    if weak:
        damage *= 0.75
        reasons.append("player_weak_penalty")
    enemy_powers = getattr(enemy, "powers", {}) if enemy is not None else {}
    vulnerable = _power_amount(enemy_powers, "Vulnerable") if isinstance(enemy_powers, dict) else 0
    if vulnerable:
        damage *= 1.5
        reasons.append("enemy_vulnerable_bonus")
    return max(0, int(round(damage))), reasons


def _effective_block_value(state: GameState, base_block: int) -> tuple[int, list[str]]:
    if base_block <= 0:
        return 0, []
    reasons: list[str] = []
    block = float(base_block)
    dexterity = _power_amount(state.player_powers, "Dexterity")
    if dexterity:
        block += dexterity
        reasons.append(f"dexterity:{dexterity}")
    frail = _power_amount(state.player_powers, "Frail")
    if frail:
        block *= 0.75
        reasons.append("player_frail_penalty")
    return max(0, int(round(block))), reasons


def _preferred_damage_target(state: GameState) -> object | None:
    known = [enemy for enemy in state.enemies if enemy.hp is not None]
    if not known:
        return None
    return min(known, key=lambda enemy: ((enemy.hp or 0) + (enemy.block or 0), enemy.x, enemy.y))


def evaluate_battle_card(state: GameState, observation: BattleCardObservation, intent: RunIntent | None = None) -> BattleCardObservation:
    updated = BattleCardObservation(
        slot=observation.slot,
        playable=observation.playable,
        energy_cost=observation.energy_cost,
        target_kind=observation.target_kind,
        card_name=observation.card_name,
        damage=observation.damage,
        block=observation.block,
        score=observation.score,
        reasons=observation.reasons[:],
    )
    if not updated.playable:
        updated.score = -999.0
        updated.reasons.append("not_playable")
        return updated

    active_intent = intent or infer_run_intent(state)
    knowledge = lookup_card_knowledge(state.character, updated.card_name)
    energy_cost = updated.energy_cost if updated.energy_cost is not None else (knowledge.energy_cost if knowledge is not None else None)
    base_damage = updated.damage if updated.damage is not None else (knowledge.damage if knowledge is not None else 0)
    base_block = updated.block if updated.block is not None else (knowledge.block if knowledge is not None else 0)
    draw = knowledge.draw if knowledge is not None else 0
    score = 0.0
    reasons = updated.reasons[:]

    if energy_cost is not None:
        if energy_cost > state.energy:
            updated.score = -999.0
            reasons.append("insufficient_energy")
            updated.reasons = reasons
            return updated
        score -= energy_cost * 0.35
        reasons.append(f"energy:{energy_cost}")

    incoming_damage = sum(enemy.intent_damage or 0 for enemy in state.enemies)
    preferred_enemy = _preferred_damage_target(state)
    damage, damage_reasons = _effective_attack_damage(state, base_damage, preferred_enemy)
    block, block_reasons = _effective_block_value(state, base_block)
    reasons.extend(damage_reasons)
    reasons.extend(block_reasons)
    target_effective_hp = None
    if preferred_enemy is not None and preferred_enemy.hp is not None:
        target_effective_hp = max(0, preferred_enemy.hp + (preferred_enemy.block or 0))
    if damage:
        score += damage
        reasons.append(f"damage:{damage}")
        if preferred_enemy is not None and (preferred_enemy.block or 0) > 0:
            reasons.append(f"enemy_block:{preferred_enemy.block or 0}")
        if target_effective_hp is not None and damage >= target_effective_hp:
            score += 8.0
            reasons.append("lethal_or_near_lethal")
        elif target_effective_hp is not None and damage >= max(1, target_effective_hp - 3):
            score += 2.5
            reasons.append("sets_up_lethal")
    if block:
        uncovered_damage = max(0, incoming_damage - max(0, state.block))
        useful_block = min(block, uncovered_damage) if uncovered_damage > 0 else 0
        wasted_block = max(0, block - useful_block)
        block_value = float(block)
        if incoming_damage > 0:
            block_value = (useful_block * 1.25) + (min(block, uncovered_damage) * 0.35) - (wasted_block * 0.55)
            if state.block >= incoming_damage:
                block_value *= 0.45
                reasons.append("existing_block_covers_hit")
            if wasted_block > 0:
                reasons.append("avoid_overblock")
        if active_intent.short_term_survival_need in {"critical", "stabilize"}:
            block_value *= 1.35
            reasons.append("survival_bias_block")
        score += block_value
        reasons.append(f"block:{block}")
    if draw:
        score += draw * 1.5
        reasons.append(f"draw:{draw}")
    if knowledge is not None and knowledge.grants_strength:
        if active_intent.long_term_direction in {"strength", "balanced"}:
            score += 5.0
            reasons.append("supports_strength_plan")
        else:
            score += 2.0
            reasons.append("scaling_setup")
    if knowledge is not None and knowledge.name == "Offering":
        score += 6.0
        reasons.append("burst_energy_draw")
    if knowledge is not None and knowledge.name == "Limit Break":
        if "strength" in active_intent.deck_axes:
            score += 7.0
            reasons.append("convert_existing_strength")
        else:
            score -= 7.0
            reasons.append("no_strength_to_multiply")
    if knowledge is not None and knowledge.name == "Battle Trance" and state.energy >= 1:
        score += 1.5
        reasons.append("dig_for_better_followups")
    if knowledge is not None and knowledge.base_score:
        score += knowledge.base_score
        if knowledge.base_score < 0:
            reasons.append("dead_card_penalty")
        else:
            reasons.append("card_kb_bonus")

    if updated.target_kind == BattleTargetKind.ENEMY:
        score += 0.25
        reasons.append("enemy_targeted")
    elif updated.target_kind == BattleTargetKind.SELF_OR_NON_TARGET:
        score += 0.15
        reasons.append("self_or_non_target")
    else:
        score -= 0.5
        reasons.append("unknown_targeting")

    if active_intent.short_term_survival_need == "critical" and damage and not block and incoming_damage > 0:
        score -= 1.5
        reasons.append("critical_hp_prefers_defense")
    if active_intent.long_term_direction == "block" and block:
        score += 1.5
        reasons.append("fits_block_plan")
    if active_intent.long_term_direction == "damage" and damage:
        score += 1.5
        reasons.append("fits_damage_plan")

    updated.energy_cost = energy_cost
    updated.damage = damage
    updated.block = block
    updated.score = score
    updated.reasons = reasons
    return updated


class HeuristicPolicy(Policy):
    """Simple rule-based policy to bootstrap logs before learning exists."""

    _tag_scores: dict[str, int] = {
        "menu": 4,
        "continue": 5,
        "start": 4,
        "combat": 5,
        "gold": 4,
        "reward": 3,
        "hp_cost": -4,
        "strength": 6,
        "scaling": 5,
        "draw": 4,
        "block": 3,
        "attack": 2,
        "aoe": 4,
        "elite": 2,
        "progress": 3,
        "safe": -1,
        "skip": -3,
        "neow": 1,
        "heal": 2,
        "upgrade": 3,
    }

    def __init__(self) -> None:
        self._current_run_intent: RunIntent | None = None
        self._last_floor = 0

    def current_run_intent(self) -> RunIntent | None:
        return self._current_run_intent

    def _refresh_run_intent(self, state: GameState) -> RunIntent:
        if state.floor <= 1 and state.act <= 1 and self._last_floor > state.floor:
            self._current_run_intent = None
        self._current_run_intent = infer_run_intent(state)
        self._last_floor = state.floor
        return self._current_run_intent

    def choose_action(self, state: GameState, actions: list[GameAction]) -> GameAction:
        evaluations = self.evaluate_actions(state, actions)
        best = max(evaluations, key=lambda item: (item.score, -len(item.action_label)))
        for action in actions:
            if action.label == best.action_label:
                return action
        raise RuntimeError(f"Chosen action disappeared: {best.action_label}")

    def evaluate_actions(self, state: GameState, actions: list[GameAction]) -> list[ActionEvaluation]:
        if not actions:
            raise ValueError("No actions available.")

        deck_tag_counts = Counter(tag for card in state.deck for tag in card.tags)
        deck_card_names = [card.name for card in state.deck]
        deck_axes = infer_deck_axes(deck_card_names, deck_tag_counts)
        current_intent = self._refresh_run_intent(state)
        enriched_state = attach_run_intent(state, current_intent)
        strategic_domains_present = any(_choice_domain_from_action(enriched_state, action) is not None for action in actions)
        if _strategic_choice_screen(state.screen) or strategic_domains_present:
            option_cache: dict[str, ChoiceOption] = {}
            domain_cache: dict[str, ChoiceDomain | None] = {}
            for action in actions:
                domain, option = _build_choice_option(enriched_state, action)
                domain_cache[action.label] = domain
                option_cache[action.label] = option

            evaluations: list[ActionEvaluation] = []
            for action in actions:
                domain = domain_cache[action.label]
                if domain is None:
                    score = float(sum(self._tag_scores.get(tag, 0) for tag in action.tags))
                    reasons = [f"tag:{tag}" for tag in action.tags if tag in self._tag_scores]
                    evaluations.append(ActionEvaluation(action_label=action.label, score=score, reasons=reasons))
                    continue
                context = _build_choice_context(enriched_state, actions, domain, option_cache=option_cache)
                option = option_cache[action.label]
                score, reasons = _score_choice_option(context, option)
                if not reasons:
                    reasons = [f"tag:{tag}" for tag in action.tags if tag in self._tag_scores]
                evaluations.append(ActionEvaluation(action_label=action.label, score=score, reasons=reasons))
            return evaluations

        state_summary = summarize_state(state)
        hp_ratio = _hp_ratio_if_known(state)
        incoming_damage = int(state_summary.get("incoming_damage") or 0)
        enemy_count = int(state_summary.get("enemy_count") or 0)
        lowest_enemy_hp = state_summary.get("lowest_enemy_hp")
        evaluations: list[ActionEvaluation] = []
        for action in actions:
            score = float(sum(self._tag_scores.get(tag, 0) for tag in action.tags))
            reasons = [f"tag:{tag}" for tag in action.tags if tag in self._tag_scores]

            if state.screen == ScreenKind.NEOW_CHOICE:
                option_text = str(action.payload.get("option_text", "")).lower()
                if not option_text:
                    if action.payload.get("option_index") == 0:
                        score += 1.0
                        reasons.append("neow_text_unreliable")
                else:
                    if "transform" in option_text:
                        score += 5.0
                        reasons.append("neow_prefers_cleanup")
                        reasons.append("neow_transform_upgrade")
                    if "remove" in option_text:
                        score += 6.0
                        reasons.append("neow_prefers_cleanup")
                        reasons.append("neow_remove_upgrade")
                    if "gold" in option_text:
                        score += 3.5
                        reasons.append("neow_values_gold")
                    if "rare" in option_text:
                        score += 4.0
                        reasons.append("neow_values_rare")
                    if ("lose" in option_text and "hp" in option_text) or " hp" in option_text:
                        score -= 4.0
                        reasons.append("neow_hp_cost_penalty")
                    if "add" in option_text and "deck" in option_text:
                        score -= 1.5
                        reasons.append("neow_card_addition_penalty")
                    if any(token in option_text for token in ("curse", "clumsy", "wound", "regret", "decay")):
                        score -= 6.0
                        reasons.append("neow_curse_penalty")

            if action.kind == ActionKind.PICK_CARD and "skip" not in action.tags:
                if state.screen != ScreenKind.CARD_GRID:
                    score += 1.0
                    reasons.append("prefer_take_over_skip")
                card_name = self._candidate_name(action, key="card")
                card_knowledge = lookup_card_knowledge(state.character, card_name)
                if card_knowledge is not None:
                    score += card_knowledge.base_score
                    reasons.append(f"kb_card:{card_knowledge.name}")
                    for axis in card_knowledge.prefers:
                        if axis in deck_axes:
                            score += 2.0
                            reasons.append(f"synergy:{axis}")
                    for axis in card_knowledge.requires:
                        if axis not in deck_axes:
                            score -= 4.0
                            reasons.append(f"missing_axis:{axis}")
                    for axis in card_knowledge.avoids_without:
                        if axis not in deck_axes:
                            score -= 6.0
                            reasons.append(f"awkward_without:{axis}")
                    if current_intent.long_term_direction in card_knowledge.tags:
                        score += 1.5
                        reasons.append(f"fits_plan:{current_intent.long_term_direction}")
                if state.screen.value == "card_grid":
                    if "attack" in action.tags:
                        score += 5.0
                        reasons.append("transform_strike_before_defend")
                    if "starter_core" in action.tags:
                        score -= 6.0
                        reasons.append("avoid_transforming_bash_early")
                if deck_tag_counts["attack"] >= deck_tag_counts["block"] + 2 and "block" in action.tags:
                    score += 3.0
                    reasons.append("deck_needs_block")
                if deck_tag_counts["block"] >= deck_tag_counts["attack"] + 2 and "attack" in action.tags:
                    score += 2.0
                    reasons.append("deck_can_add_attack")
                if state.floor >= 12 and deck_tag_counts["scaling"] == 0 and "scaling" in action.tags:
                    score += 4.0
                    reasons.append("need_scaling_for_late_game")
                if hp_ratio is not None and hp_ratio < 0.45 and "block" in action.tags:
                    score += 2.5
                    reasons.append("low_hp_values_block")
                if hp_ratio is not None and hp_ratio < 0.30 and "block" in action.tags:
                    score += 2.0
                    reasons.append("critical_hp_extra_block_value")
                if hp_ratio is not None and hp_ratio < 0.35 and "attack" in action.tags:
                    score -= 1.0
                    reasons.append("low_hp_avoid_extra_attack")

            if action.kind == ActionKind.SKIP_REWARD and deck_tag_counts["scaling"] > 0 and deck_tag_counts["block"] > 0:
                score += 1.0
                reasons.append("deck_already_balanced")
            if action.kind == ActionKind.SKIP_REWARD and current_intent.short_term_survival_need == "critical":
                score -= 2.0
                reasons.append("need_help_now_not_skip")

            if "hp_cost" in action.tags and hp_ratio is not None and hp_ratio < 0.45:
                score -= 3.0
                reasons.append("low_hp_avoid_hp_payment")
            if "hp_cost" in action.tags and hp_ratio is not None and hp_ratio < 0.30:
                score -= 4.0
                reasons.append("critical_hp_reject_hp_payment")
            if "heal" in action.tags and hp_ratio is not None and hp_ratio < 0.45:
                score += 3.0
                reasons.append("low_hp_prioritize_healing")

            if action.kind == ActionKind.TAKE_RELIC:
                relic_name = self._candidate_name(action, key="relic")
                relic_knowledge = lookup_relic_knowledge(state.character, relic_name)
                if relic_knowledge is not None:
                    score += relic_knowledge.base_score
                    reasons.append(f"kb_relic:{relic_knowledge.name}")
                    for axis in relic_knowledge.prefers:
                        if axis in deck_axes:
                            score += 1.5
                            reasons.append(f"relic_synergy:{axis}")
                    if hp_ratio is not None and hp_ratio < 0.45 and "heal" in relic_knowledge.tags:
                        score += 2.0
                        reasons.append("low_hp_values_sustain_relic")

            if action.kind == ActionKind.CHOOSE_PATH:
                if hp_ratio is not None and hp_ratio < 0.45 and "safe" in action.tags:
                    score += 4.0
                    reasons.append("low_hp_take_safe_path")
                if hp_ratio is not None and hp_ratio < 0.30 and "safe" in action.tags:
                    score += 3.0
                    reasons.append("critical_hp_take_safest_path")
                if hp_ratio is not None and hp_ratio > 0.70 and "elite" in action.tags:
                    score += 3.0
                    reasons.append("healthy_enough_for_elite")
                if hp_ratio is not None and hp_ratio < 0.45 and "elite" in action.tags:
                    score -= 4.0
                    reasons.append("low_hp_avoid_elite")
                if state.floor >= 30 and "elite" in action.tags and hp_ratio is not None and hp_ratio < 0.60:
                    score -= 3.0
                    reasons.append("late_elite_risk")
                if deck_tag_counts["scaling"] == 0 and state.floor >= 10 and "elite" in action.tags:
                    score -= 1.5
                    reasons.append("no_scaling_yet_for_elite_path")
                if current_intent.elite_boss_risk_posture == "aggressive" and "elite" in action.tags:
                    score += 2.0
                    reasons.append("aggressive_posture_accepts_elite")
                if current_intent.elite_boss_risk_posture == "cautious" and "safe" in action.tags:
                    score += 2.0
                    reasons.append("cautious_posture_prefers_safe")

            if action.kind == ActionKind.REST:
                if hp_ratio is not None and hp_ratio < 0.55:
                    score += 4.0
                    reasons.append("rest_to_stabilize")
                else:
                    score -= 2.0
                    reasons.append("healthy_enough_to_upgrade")

            if action.kind == ActionKind.SMITH and hp_ratio is not None and hp_ratio >= 0.50:
                score += 4.0
                reasons.append("upgrade_when_stable")

            if action.kind == ActionKind.END_TURN and state.energy == 0:
                score += 1.0
                reasons.append("no_energy_remaining")
            if state.screen == ScreenKind.BATTLE:
                battle_macro = action.payload.get("battle_macro")
                if battle_macro == "basic_turn":
                    score += 8.0
                    reasons.append("prefer_playing_cards_before_end_turn")
                    if incoming_damage >= max(8, int(state.hp * 0.25)):
                        score += 2.0
                        reasons.append("incoming_damage_pressure")
                    if enemy_count >= 2:
                        score += 1.0
                        reasons.append("multi_enemy_board")
                    if lowest_enemy_hp is not None and int(lowest_enemy_hp) <= 12:
                        score += 1.5
                        reasons.append("enemy_near_lethal")
                    if current_intent.short_term_survival_need in {"critical", "stabilize"}:
                        score += 1.5
                        reasons.append(f"battle_plan:{current_intent.short_term_survival_need}")
                if action.kind == ActionKind.END_TURN:
                    has_battle_macro = any(candidate.payload.get("battle_macro") == "basic_turn" for candidate in actions)
                    if has_battle_macro:
                        score -= 6.0
                        reasons.append("defer_end_turn_until_cards_attempted")
                    if incoming_damage >= max(8, int(state.hp * 0.25)):
                        score -= 2.0
                        reasons.append("unsafe_to_pass_under_pressure")
                    if current_intent.short_term_survival_need == "critical":
                        score -= 1.5
                        reasons.append("critical_state_dislikes_end_turn")

            evaluations.append(ActionEvaluation(action_label=action.label, score=score, reasons=reasons))
        return evaluations

    @staticmethod
    def _candidate_name(action: GameAction, *, key: str) -> str | None:
        payload_name = action.payload.get(key)
        if isinstance(payload_name, str):
            return payload_name
        label = action.label
        if label.lower().startswith("pick "):
            return label[5:]
        if label.lower().startswith("take "):
            return label[5:]
        return None
