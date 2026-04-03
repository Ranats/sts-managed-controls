from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ScreenKind(str, Enum):
    UNKNOWN = "unknown"
    MENU = "menu"
    PROFILE_SELECT = "profile_select"
    MODE_SELECT = "mode_select"
    CHARACTER_SELECT = "character_select"
    NEOW_CHOICE = "neow_choice"
    NEOW_DIALOG = "neow_dialog"
    RELIC_POPUP = "relic_popup"
    CARD_GRID = "card_grid"
    CONFIRM_POPUP = "confirm_popup"
    MAP = "map"
    CONTINUE = "continue"
    EVENT = "event"
    BATTLE = "battle"
    REWARD_MENU = "reward_menu"
    REWARD_CARDS = "reward_cards"
    REWARD_RELIC = "reward_relic"
    REWARD_POTION = "reward_potion"
    REWARD_GOLD_ONLY = "reward_gold_only"
    SHOP = "shop"
    REST = "rest"
    BOSS = "boss"
    BOSS_RELIC = "boss_relic"
    GAME_OVER = "game_over"


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    PLAY_CARD = "play_card"
    END_TURN = "end_turn"
    PICK_CARD = "pick_card"
    SKIP_REWARD = "skip_reward"
    TAKE_RELIC = "take_relic"
    TAKE_POTION = "take_potion"
    CHOOSE_PATH = "choose_path"
    REST = "rest"
    SMITH = "smith"
    BUY = "buy"


class BattleTargetKind(str, Enum):
    NONE = "none"
    ENEMY = "enemy"
    SELF_OR_NON_TARGET = "self_or_non_target"
    UNKNOWN = "unknown"


class ChoiceDomain(str, Enum):
    NEOW = "neow"
    EVENT = "event"
    REWARD_CARD = "reward_card"
    REWARD_RELIC = "reward_relic"
    REWARD_POTION = "reward_potion"
    SHOP_PURCHASE = "shop_purchase"
    SHOP_REMOVE = "shop_remove"
    MAP_PATH = "map_path"
    REST_SITE = "rest_site"
    BOSS_RELIC = "boss_relic"


class StateSource(str, Enum):
    OCR = "ocr"
    MEMORY = "memory"
    HYBRID = "hybrid"
    MANUAL = "manual"


@dataclass(slots=True)
class GameAction:
    kind: ActionKind
    label: str
    payload: dict[str, object] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ActionEvaluation:
    action_label: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionExpectation:
    next_screen: str | None = None
    change_summary: str = ""
    verification_hint: str = ""


@dataclass(slots=True)
class ExecutionObservation:
    screen: str
    hp: int
    max_hp: int
    energy: int
    gold: int
    floor: int
    max_energy: int = 0
    block: int = 0
    actions: list[str] = field(default_factory=list)
    state_source: str = ""
    metric_sources: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass(slots=True)
class RunIntent:
    deck_axes: list[str] = field(default_factory=list)
    short_term_survival_need: str = "stable"
    long_term_direction: str = "balanced"
    elite_boss_risk_posture: str = "balanced"


@dataclass(slots=True)
class BuildIntentPreset:
    name: str
    character: str = ""
    desired_axes: list[str] = field(default_factory=list)
    avoid_axes: list[str] = field(default_factory=list)
    preferred_cards: list[str] = field(default_factory=list)
    banned_cards: list[str] = field(default_factory=list)
    risk_posture: str = "balanced"
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChoiceOption:
    option_id: str
    label: str
    text: str = ""
    tags: list[str] = field(default_factory=list)
    payload: dict[str, object] = field(default_factory=dict)
    source: StateSource = StateSource.OCR
    confidence: float | None = None
    kb_keys: list[str] = field(default_factory=list)
    upside_tags: list[str] = field(default_factory=list)
    downside_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChoiceContext:
    domain: ChoiceDomain
    screen: ScreenKind
    character: str
    act: int
    floor: int
    ascension: int | None = None
    hp: int = 0
    max_hp: int = 0
    gold: int = 0
    energy: int = 0
    deck_names: list[str] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    potion_names: list[str] = field(default_factory=list)
    run_intent: RunIntent | None = None
    build_preset: BuildIntentPreset | None = None
    options: list[ChoiceOption] = field(default_factory=list)
    option_source: StateSource = StateSource.OCR
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DeckCard:
    name: str
    upgraded: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EnemyState:
    x: int
    y: int
    width: int
    height: int
    hp: int | None = None
    max_hp: int | None = None
    hp_text: str | None = None
    status_icon_count: int = 0
    intent_damage: int | None = None
    block: int | None = None
    powers: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class BattleCardObservation:
    slot: int
    playable: bool
    energy_cost: int | None = None
    target_kind: BattleTargetKind = BattleTargetKind.UNKNOWN
    card_name: str | None = None
    damage: int | None = None
    block: int | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DecisionProviderResult:
    provider_name: str
    action: GameAction
    evaluations: list[ActionEvaluation] = field(default_factory=list)
    reasoning: str = ""
    expected_outcome: ExecutionExpectation | None = None
    confidence: float | None = None
    fallback_note: str = ""


@dataclass(slots=True)
class GameState:
    screen: ScreenKind
    act: int
    floor: int
    hp: int
    max_hp: int
    energy: int
    gold: int
    character: str
    enemy: str | None = None
    max_energy: int = 0
    block: int = 0
    enemies: list[EnemyState] = field(default_factory=list)
    deck: list[DeckCard] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    player_powers: dict[str, int] = field(default_factory=dict)
    hand: list[str] = field(default_factory=list)
    available_actions: list[GameAction] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    run_intent: RunIntent | None = None
    state_source: StateSource = StateSource.OCR
    metric_sources: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunSummary:
    character: str
    won: bool
    act_reached: int
    floor_reached: int
    score: int
    deck: list[DeckCard]
    relics: list[str]
    picked_cards: list[str]
    skipped_cards: list[str]
    path: list[str]
    strategy_tags: list[str]


@dataclass(slots=True)
class BuildInsight:
    character: str
    label: str
    sample_size: int
    win_rate: float
    anchor_cards: list[str]
    anchor_relics: list[str]
    strategy_tags: list[str]


@dataclass(slots=True)
class LiveLoopTick:
    step_index: int
    screen: str
    floor: int
    hp: int
    max_hp: int
    energy: int
    gold: int
    action_label: str
    provider_name: str
    max_energy: int = 0
    block: int = 0
    reasoning: str = ""
    expected_outcome: ExecutionExpectation | None = None
    observed_outcome: ExecutionObservation | None = None
    verification_status: str = "unknown"
    state_source: str = ""
    state_metric_sources: dict[str, str] = field(default_factory=dict)
    fallback_note: str = ""
    phase_timings_ms: dict[str, int] = field(default_factory=dict)
