from __future__ import annotations

from dataclasses import replace

from sts_bot.adapters.base import GameAdapter
from sts_bot.models import ActionKind, DeckCard, GameAction, GameState, RunSummary, ScreenKind


class MockAdapter(GameAdapter):
    """Small deterministic environment used to validate logging and analysis."""

    def __init__(self) -> None:
        self._episode = 0
        self._step = 0
        self._won = False
        self._picked_cards: list[str] = []
        self._skipped_cards: list[str] = []
        self._path: list[str] = []
        self._deck: list[DeckCard] = []
        self._relics: list[str] = []
        self._state = self._build_state()

    def start_run(self) -> None:
        self._episode += 1
        self._step = 0
        self._won = self._episode % 3 != 0
        self._picked_cards = []
        self._skipped_cards = []
        self._path = []
        self._deck = [
            DeckCard("Strike", tags=["attack"]),
            DeckCard("Strike", tags=["attack"]),
            DeckCard("Defend", tags=["block"]),
            DeckCard("Bash", tags=["attack", "vulnerable"]),
        ]
        self._relics = ["Burning Blood"]
        self._state = self._build_state()

    def current_state(self) -> GameState:
        return replace(self._state, available_actions=self.available_actions())

    def available_actions(self) -> list[GameAction]:
        if self._step == 0:
            return [
                GameAction(ActionKind.CHOOSE_PATH, "Elite path", {"path": "elite"}, ["elite", "aggressive"]),
                GameAction(ActionKind.CHOOSE_PATH, "Safe path", {"path": "safe"}, ["safe"]),
            ]
        if self._step == 1:
            return [
                GameAction(ActionKind.PICK_CARD, "Pick Pommel Strike", {"card": "Pommel Strike"}, ["attack", "draw"]),
                GameAction(ActionKind.PICK_CARD, "Pick Shrug It Off", {"card": "Shrug It Off"}, ["block", "draw"]),
                GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
            ]
        if self._step == 2:
            return [
                GameAction(ActionKind.TAKE_RELIC, "Take Vajra", {"relic": "Vajra"}, ["strength"]),
                GameAction(ActionKind.TAKE_RELIC, "Take Anchor", {"relic": "Anchor"}, ["block"]),
            ]
        if self._step == 3:
            return [
                GameAction(ActionKind.PICK_CARD, "Pick Limit Break", {"card": "Limit Break"}, ["scaling", "strength"]),
                GameAction(ActionKind.PICK_CARD, "Pick True Grit", {"card": "True Grit"}, ["block", "exhaust"]),
                GameAction(ActionKind.SKIP_REWARD, "Skip", {}, ["skip"]),
            ]
        return []

    def apply_action(self, action: GameAction) -> None:
        if action.kind == ActionKind.CHOOSE_PATH:
            self._path.append(str(action.payload["path"]))
        elif action.kind == ActionKind.PICK_CARD:
            card_name = str(action.payload["card"])
            self._picked_cards.append(card_name)
            self._deck.append(DeckCard(card_name, tags=action.tags))
        elif action.kind == ActionKind.SKIP_REWARD:
            self._skipped_cards.append("reward")
        elif action.kind == ActionKind.TAKE_RELIC:
            self._relics.append(str(action.payload["relic"]))
        self._step += 1
        self._state = self._build_state()

    def is_run_over(self) -> bool:
        return self._step >= 4

    def run_summary(self) -> RunSummary:
        tags = self._infer_strategy_tags()
        return RunSummary(
            character="Ironclad",
            won=self._won,
            act_reached=3 if self._won else 2,
            floor_reached=57 if self._won else 33,
            score=430 if self._won else 180,
            deck=self._deck[:],
            relics=self._relics[:],
            picked_cards=self._picked_cards[:],
            skipped_cards=self._skipped_cards[:],
            path=self._path[:],
            strategy_tags=tags,
        )

    def _build_state(self) -> GameState:
        screen = ScreenKind.MAP
        enemy = None
        if self._step == 1:
            screen = ScreenKind.REWARD_CARDS
        elif self._step == 2:
            screen = ScreenKind.BOSS
            enemy = "Hexaghost"
        elif self._step == 3:
            screen = ScreenKind.REWARD_CARDS
        return GameState(
            screen=screen,
            act=1 if self._step < 3 else 2,
            floor=1 + self._step * 8,
            hp=70 - self._step * 8,
            max_hp=80,
            energy=3,
            gold=99 + self._step * 25,
            character="Ironclad",
            enemy=enemy,
            deck=self._deck[:],
            relics=self._relics[:],
            hand=[],
            tags=self._infer_strategy_tags(),
        )

    def _infer_strategy_tags(self) -> list[str]:
        tags = {"midrange"}
        card_names = {card.name for card in self._deck}
        relic_names = set(self._relics)
        if "Limit Break" in card_names or "Vajra" in relic_names:
            tags.add("strength")
        if any("block" in card.tags for card in self._deck):
            tags.add("block")
        return sorted(tags)
