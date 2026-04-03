from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sts_bot.models import ActionKind, ScreenKind


def _parse_intlike(value: object, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        base = 16 if text.lower().startswith("0x") else 10
        return int(text, base=base)
    raise ValueError(f"Unsupported integer value: {value!r}")


@dataclass(slots=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @classmethod
    def from_list(cls, values: list[int]) -> "Rect":
        if len(values) != 4:
            raise ValueError(f"Expected four integers for rect, got: {values!r}")
        return cls(*map(int, values))

    def to_list(self) -> list[int]:
        return [self.left, self.top, self.width, self.height]

    def scaled(self, scale_x: float, scale_y: float) -> "Rect":
        return Rect(
            left=round(self.left * scale_x),
            top=round(self.top * scale_y),
            width=max(1, round(self.width * scale_x)),
            height=max(1, round(self.height * scale_y)),
        )


@dataclass(slots=True)
class AnchorDefinition:
    name: str
    screen: ScreenKind
    template_path: Path
    region: Rect
    threshold: float = 0.95
    scale_template: bool = True
    scale_region: bool = True


@dataclass(slots=True)
class TextRegionDefinition:
    name: str
    region: Rect
    whitelist: str | None = None
    parser: str = "int"


@dataclass(slots=True)
class ActionDefinition:
    screen: ScreenKind
    kind: ActionKind
    label: str
    point: tuple[int, int]
    tags: list[str] = field(default_factory=list)
    payload: dict[str, object] = field(default_factory=dict)
    template_path: Path | None = None
    region: Rect | None = None
    threshold: float = 0.95
    key: str | None = None
    keys: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    settle_ms: int = 0
    hold_ms: int = 40
    post_key_delay_ms: int = 150
    gap_ms: int = 250
    drag_point: tuple[int, int] | None = None
    drag_duration_ms: int = 220
    scale_template: bool = True
    scale_region: bool = True


@dataclass(slots=True)
class MemoryFieldDefinition:
    name: str
    screens: list[ScreenKind] = field(default_factory=list)
    locator_kind: str = "module_offset"
    offset: int | None = None
    pattern: str | None = None
    pattern_offset: int = 0
    pointer_offsets: list[int] = field(default_factory=list)
    value_type: str = "int32"


@dataclass(slots=True)
class MemoryReadConfig:
    enabled: bool = False
    module: str = "sts2.dll"
    refresh_ms: int = 250
    fields: list[MemoryFieldDefinition] = field(default_factory=list)


@dataclass(slots=True)
class CalibrationProfile:
    window_title: str
    reference_width: int = 1517
    reference_height: int = 944
    capture_backend_name: str = "auto"
    input_backend_name: str = "auto"
    window_message_delivery: str = "send"
    window_message_activation: str = "none"
    legacy_input_backend: str = "combined"
    target_process_name: str | None = None
    target_title_regex: str | None = None
    target_class_name: str | None = None
    dry_run: bool = False
    verbose_diagnostics: bool = False
    allow_foreground_fallback: bool = False
    match_threshold: float = 0.95
    action_delay_ms: int = 700
    battle_cancel_key: str = "down"
    startup_sequence_labels: list[str] = field(default_factory=list)
    scene_input_backends: dict[str, str] = field(default_factory=dict)
    start_actions: list[ActionDefinition] = field(default_factory=list)
    anchors: list[AnchorDefinition] = field(default_factory=list)
    text_regions: list[TextRegionDefinition] = field(default_factory=list)
    actions: list[ActionDefinition] = field(default_factory=list)
    static_character: str = "Unknown"
    memory_read: MemoryReadConfig = field(default_factory=MemoryReadConfig)

    @classmethod
    def load(cls, path: Path) -> "CalibrationProfile":
        payload = json.loads(path.read_text(encoding="utf-8"))
        base_dir = path.parent

        def resolve(relative_path: str | None) -> Path | None:
            if relative_path is None:
                return None
            candidate = Path(relative_path)
            if candidate.is_absolute():
                return candidate
            return (base_dir / candidate).resolve()

        anchors = [
            AnchorDefinition(
                name=item["name"],
                screen=ScreenKind(item["screen"]),
                template_path=resolve(item["template_path"]) or Path(item["template_path"]),
                region=Rect.from_list(item["region"]),
                threshold=float(item.get("threshold", payload.get("match_threshold", 0.95))),
                scale_template=bool(item.get("scale_template", True)),
                scale_region=bool(item.get("scale_region", True)),
            )
            for item in payload.get("anchors", [])
        ]
        text_regions = [
            TextRegionDefinition(
                name=item["name"],
                region=Rect.from_list(item["region"]),
                whitelist=item.get("whitelist"),
                parser=item.get("parser", "int"),
            )
            for item in payload.get("text_regions", [])
        ]
        memory_read_payload = payload.get("memory_read", {})
        memory_read = MemoryReadConfig(
            enabled=bool(memory_read_payload.get("enabled", False)),
            module=str(memory_read_payload.get("module", "sts2.dll")),
            refresh_ms=int(memory_read_payload.get("refresh_ms", 250)),
            fields=[
                MemoryFieldDefinition(
                    name=str(item["name"]),
                    screens=[ScreenKind(str(screen)) for screen in item.get("screens", [])],
                    locator_kind=str(item.get("locator_kind", "module_offset")),
                    offset=_parse_intlike(item.get("offset")),
                    pattern=str(item["pattern"]) if item.get("pattern") is not None else None,
                    pattern_offset=int(_parse_intlike(item.get("pattern_offset"), default=0) or 0),
                    pointer_offsets=[int(_parse_intlike(offset, default=0) or 0) for offset in item.get("pointer_offsets", [])],
                    value_type=str(item.get("value_type", "int32")),
                )
                for item in memory_read_payload.get("fields", [])
            ],
        )

        def build_action(item: dict[str, object]) -> ActionDefinition:
            region = item.get("region")
            point = item.get("point", [0, 0])
            return ActionDefinition(
                screen=ScreenKind(str(item["screen"])),
                kind=ActionKind(str(item["kind"])),
                label=str(item["label"]),
                point=(int(point[0]), int(point[1])),
                tags=[str(tag) for tag in item.get("tags", [])],
                payload=dict(item.get("payload", {})),
                template_path=resolve(item.get("template_path")) if item.get("template_path") else None,
                region=Rect.from_list(region) if region else None,
                threshold=float(item.get("threshold", payload.get("match_threshold", 0.95))),
                key=str(item["key"]) if "key" in item else None,
                keys=[str(key) for key in item.get("keys", [])],
                buttons=[str(button) for button in item.get("buttons", [])],
                settle_ms=int(item.get("settle_ms", 0)),
                hold_ms=int(item.get("hold_ms", 40)),
                post_key_delay_ms=int(item.get("post_key_delay_ms", 150)),
                gap_ms=int(item.get("gap_ms", 250)),
                drag_point=(int(item["drag_point"][0]), int(item["drag_point"][1])) if item.get("drag_point") else None,
                drag_duration_ms=int(item.get("drag_duration_ms", 220)),
                scale_template=bool(item.get("scale_template", True)),
                scale_region=bool(item.get("scale_region", True)),
            )

        return cls(
            window_title=str(payload["window_title"]),
            reference_width=int(payload.get("reference_width", 1517)),
            reference_height=int(payload.get("reference_height", 944)),
            capture_backend_name=str(payload.get("capture_backend", payload.get("capture_backend_name", "auto"))),
            input_backend_name=str(payload.get("input_backend_name", "auto")),
            window_message_delivery=str(payload.get("window_message_delivery", "send")),
            window_message_activation=str(payload.get("window_message_activation", "none")),
            legacy_input_backend=str(payload.get("legacy_input_backend", payload.get("input_backend", "combined"))),
            target_process_name=str(payload["target_process_name"]) if "target_process_name" in payload and payload["target_process_name"] is not None else None,
            target_title_regex=str(payload["target_title_regex"]) if "target_title_regex" in payload and payload["target_title_regex"] is not None else None,
            target_class_name=str(payload["target_class_name"]) if "target_class_name" in payload and payload["target_class_name"] is not None else None,
            dry_run=bool(payload.get("dry_run", False)),
            verbose_diagnostics=bool(payload.get("verbose_diagnostics", False)),
            allow_foreground_fallback=bool(payload.get("allow_foreground_fallback", False)),
            match_threshold=float(payload.get("match_threshold", 0.95)),
            action_delay_ms=int(payload.get("action_delay_ms", 700)),
            battle_cancel_key=str(payload.get("battle_cancel_key", "down")),
            startup_sequence_labels=[str(label) for label in payload.get("startup_sequence_labels", [])],
            scene_input_backends={str(key): str(value) for key, value in payload.get("scene_input_backends", {}).items()},
            anchors=anchors,
            text_regions=text_regions,
            actions=[build_action(item) for item in payload.get("actions", [])],
            start_actions=[build_action(item) for item in payload.get("start_actions", [])],
            static_character=str(payload.get("static_character", "Unknown")),
            memory_read=memory_read,
        )

    def to_dict(self, base_dir: Path | None = None) -> dict[str, object]:
        def relativize(path: Path | None) -> str | None:
            if path is None:
                return None
            if base_dir is None:
                return str(path)
            try:
                return str(path.relative_to(base_dir))
            except ValueError:
                return str(path)

        def action_to_dict(action: ActionDefinition) -> dict[str, object]:
            payload: dict[str, object] = {
                "screen": action.screen.value,
                "kind": action.kind.value,
                "label": action.label,
                "point": list(action.point),
                "tags": action.tags,
                "payload": action.payload,
                "threshold": action.threshold,
            }
            if action.template_path:
                payload["template_path"] = relativize(action.template_path)
            if action.region:
                payload["region"] = action.region.to_list()
            if action.key:
                payload["key"] = action.key
            if action.keys:
                payload["keys"] = action.keys
            if action.buttons:
                payload["buttons"] = action.buttons
            if action.settle_ms:
                payload["settle_ms"] = action.settle_ms
            if action.hold_ms != 40:
                payload["hold_ms"] = action.hold_ms
            if action.post_key_delay_ms != 150:
                payload["post_key_delay_ms"] = action.post_key_delay_ms
            if action.gap_ms != 250:
                payload["gap_ms"] = action.gap_ms
            if action.drag_point is not None:
                payload["drag_point"] = list(action.drag_point)
            if action.drag_duration_ms != 220:
                payload["drag_duration_ms"] = action.drag_duration_ms
            if not action.scale_template:
                payload["scale_template"] = False
            if not action.scale_region:
                payload["scale_region"] = False
            return payload

        return {
            "window_title": self.window_title,
            "reference_width": self.reference_width,
            "reference_height": self.reference_height,
            "capture_backend": self.capture_backend_name,
            "input_backend_name": self.input_backend_name,
            "window_message_delivery": self.window_message_delivery,
            "window_message_activation": self.window_message_activation,
            "legacy_input_backend": self.legacy_input_backend,
            "target_process_name": self.target_process_name,
            "target_title_regex": self.target_title_regex,
            "target_class_name": self.target_class_name,
            "dry_run": self.dry_run,
            "verbose_diagnostics": self.verbose_diagnostics,
            "allow_foreground_fallback": self.allow_foreground_fallback,
            "match_threshold": self.match_threshold,
            "action_delay_ms": self.action_delay_ms,
            "battle_cancel_key": self.battle_cancel_key,
            "startup_sequence_labels": self.startup_sequence_labels,
            "scene_input_backends": self.scene_input_backends,
            "static_character": self.static_character,
            "memory_read": {
                "enabled": self.memory_read.enabled,
                "module": self.memory_read.module,
                "refresh_ms": self.memory_read.refresh_ms,
                "fields": [
                    {
                        "name": field.name,
                        "screens": [screen.value for screen in field.screens],
                        "locator_kind": field.locator_kind,
                        "offset": field.offset,
                        "pattern": field.pattern,
                        "pattern_offset": field.pattern_offset,
                        "pointer_offsets": field.pointer_offsets,
                        "value_type": field.value_type,
                    }
                    for field in self.memory_read.fields
                ],
            },
            "anchors": [
                {
                    "name": anchor.name,
                    "screen": anchor.screen.value,
                    "template_path": relativize(anchor.template_path),
                    "region": anchor.region.to_list(),
                    "threshold": anchor.threshold,
                    "scale_template": anchor.scale_template,
                    "scale_region": anchor.scale_region,
                }
                for anchor in self.anchors
            ],
            "text_regions": [
                {
                    "name": region.name,
                    "region": region.region.to_list(),
                    "whitelist": region.whitelist,
                    "parser": region.parser,
                }
                for region in self.text_regions
            ],
            "actions": [action_to_dict(action) for action in self.actions],
            "start_actions": [action_to_dict(action) for action in self.start_actions],
        }


def example_profile(template_dir: Path | None = None) -> CalibrationProfile:
    template_dir = template_dir or Path("templates")

    def template(name: str) -> Path:
        return template_dir / name

    return CalibrationProfile(
        window_title="Slay the Spire 2",
        reference_width=1517,
        reference_height=944,
        capture_backend_name="auto",
        input_backend_name="auto",
        window_message_delivery="send",
        window_message_activation="none",
        legacy_input_backend="combined",
        target_process_name="SlayTheSpire2.exe",
        target_title_regex="Slay the Spire 2",
        target_class_name=None,
        dry_run=False,
        verbose_diagnostics=False,
        allow_foreground_fallback=False,
        match_threshold=0.94,
        action_delay_ms=700,
        battle_cancel_key="down",
        startup_sequence_labels=[
            "Single Play",
            "Select profile 1",
            "Standard run",
            "Select Ironclad",
            "Take default whale option",
            "Advance whale dialog",
            "Close relic popup",
        ],
        scene_input_backends={
            ScreenKind.MENU.value: "gamepad",
            ScreenKind.PROFILE_SELECT.value: "gamepad",
            ScreenKind.MODE_SELECT.value: "gamepad",
            ScreenKind.CHARACTER_SELECT.value: "gamepad",
            ScreenKind.NEOW_CHOICE.value: "gamepad",
            ScreenKind.NEOW_DIALOG.value: "window_messages",
            ScreenKind.CONFIRM_POPUP.value: "window_messages",
            ScreenKind.RELIC_POPUP.value: "gamepad",
            ScreenKind.CARD_GRID.value: "gamepad",
            ScreenKind.BATTLE.value: "gamepad",
            ScreenKind.MAP.value: "gamepad",
            ScreenKind.EVENT.value: "gamepad",
            ScreenKind.REWARD_MENU.value: "window_messages",
            ScreenKind.REWARD_CARDS.value: "window_messages",
            ScreenKind.REWARD_RELIC.value: "window_messages",
            ScreenKind.REWARD_POTION.value: "window_messages",
            ScreenKind.REWARD_GOLD_ONLY.value: "window_messages",
            ScreenKind.BOSS_RELIC.value: "window_messages",
            ScreenKind.CONTINUE.value: "gamepad",
            ScreenKind.GAME_OVER.value: "window_messages",
        },
        static_character="Ironclad",
        anchors=[
            AnchorDefinition("menu_anchor", ScreenKind.MENU, template("menu_anchor.png"), Rect(320, 150, 620, 320), 0.72),
            AnchorDefinition("menu_anchor_live_v2", ScreenKind.MENU, template("menu_anchor_live_v2.png"), Rect(430, 130, 760, 330), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("profile_select_anchor_live", ScreenKind.PROFILE_SELECT, template("profile_select_anchor_live.png"), Rect(230, 150, 530, 210), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("mode_select_anchor", ScreenKind.MODE_SELECT, template("mode_select_anchor.png"), Rect(180, 120, 360, 430), 0.50),
            AnchorDefinition("mode_select_anchor_live_v2", ScreenKind.MODE_SELECT, template("mode_select_anchor_live_v2.png"), Rect(250, 120, 940, 440), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("mode_select_anchor_live_v3", ScreenKind.MODE_SELECT, template("mode_select_anchor_live_v3.png"), Rect(314, 133, 827, 577), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("character_select_anchor", ScreenKind.CHARACTER_SELECT, template("character_select_anchor.png"), Rect(150, 230, 420, 260), 0.50),
            AnchorDefinition("character_select_anchor_live_v2", ScreenKind.CHARACTER_SELECT, template("character_select_anchor_live_v2.png"), Rect(140, 140, 620, 400), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("character_select_anchor_live_v3", ScreenKind.CHARACTER_SELECT, template("character_select_anchor_live_v3.png"), Rect(215, 235, 620, 285), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("character_select_anchor_live_v3", ScreenKind.CHARACTER_SELECT, template("character_select_anchor_live_v3.png"), Rect(180, 210, 760, 300), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("neow_choice_anchor", ScreenKind.NEOW_CHOICE, template("neow_choice_anchor.png"), Rect(250, 440, 720, 260), 0.55),
            AnchorDefinition("neow_choice_anchor_live_v2", ScreenKind.NEOW_CHOICE, template("neow_choice_anchor_live_v2.png"), Rect(300, 430, 880, 330), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("neow_choice_anchor_live_v3", ScreenKind.NEOW_CHOICE, template("neow_choice_anchor_live_v3.png"), Rect(389, 483, 830, 289), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("neow_dialog_anchor", ScreenKind.NEOW_DIALOG, template("neow_dialog_anchor.png"), Rect(270, 620, 720, 130), 0.55),
            AnchorDefinition("relic_popup_anchor", ScreenKind.RELIC_POPUP, template("relic_popup_anchor.png"), Rect(380, 120, 600, 580), 0.55),
            AnchorDefinition("card_grid_anchor", ScreenKind.CARD_GRID, template("card_grid_anchor.png"), Rect(60, 30, 860, 500), 0.55, scale_template=False, scale_region=False),
            AnchorDefinition("card_grid_anchor_live_v2", ScreenKind.CARD_GRID, template("card_grid_anchor_live_v2.png"), Rect(520, 680, 400, 85), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("confirm_popup_anchor", ScreenKind.CONFIRM_POPUP, template("confirm_popup_anchor.png"), Rect(952, 395, 90, 69), 0.55, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor", ScreenKind.MAP, template("map_anchor.png"), Rect(1480, 64, 340, 120), 0.94),
            AnchorDefinition("map_anchor_live", ScreenKind.MAP, template("map_anchor_live.png"), Rect(206, 272, 104, 96), 0.70, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v2", ScreenKind.MAP, template("map_anchor_live_v2.png"), Rect(850, 155, 170, 230), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v4", ScreenKind.MAP, template("map_anchor_live_v4.png"), Rect(962, 182, 176, 311), 0.75, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v5", ScreenKind.MAP, template("map_anchor_live_v5.png"), Rect(1233, 196, 253, 290), 0.72, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v6", ScreenKind.MAP, template("map_anchor_live_v6.png"), Rect(1160, 205, 305, 350), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v7", ScreenKind.MAP, template("map_anchor_live_v7.png"), Rect(1110, 205, 250, 315), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v8", ScreenKind.MAP, template("map_anchor_live_v8.png"), Rect(1110, 205, 250, 315), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v9", ScreenKind.MAP, template("map_anchor_live_v9.png"), Rect(360, 280, 570, 280), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("map_anchor_live_v10", ScreenKind.MAP, template("map_anchor_live_v10.png"), Rect(1140, 205, 255, 305), 0.60, scale_template=False, scale_region=False),
            AnchorDefinition("continue_anchor_live", ScreenKind.CONTINUE, template("continue_anchor_live.png"), Rect(857, 405, 135, 72), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("continue_anchor_live_v2", ScreenKind.CONTINUE, template("continue_anchor_live_v2.png"), Rect(250, 570, 430, 95), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("continue_anchor_shop_intro", ScreenKind.CONTINUE, template("continue_anchor_shop_intro.png"), Rect(1180, 500, 305, 195), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("continue_anchor_shop_intro_v2", ScreenKind.CONTINUE, template("continue_anchor_shop_intro_v2.png"), Rect(1160, 720, 225, 180), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("event_jungle_anchor_live", ScreenKind.EVENT, template("event_jungle_anchor_live.png"), Rect(685, 115, 265, 120), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("event_jungle_followup_anchor_live", ScreenKind.EVENT, template("event_jungle_followup_anchor_live.png"), Rect(690, 115, 275, 160), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("battle_anchor", ScreenKind.BATTLE, template("battle_anchor.png"), Rect(1170, 700, 260, 170), 0.70),
            AnchorDefinition("battle_anchor_live", ScreenKind.BATTLE, template("battle_anchor_live.png"), Rect(841, 451, 194, 91), 0.70, scale_template=False, scale_region=False),
            AnchorDefinition("battle_anchor_live_v2", ScreenKind.BATTLE, template("battle_anchor_live_v2.png"), Rect(1570, 760, 295, 245), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("battle_anchor_live_v3", ScreenKind.BATTLE, template("battle_anchor_live_v3.png"), Rect(1610, 845, 265, 185), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("battle_anchor_live_v4", ScreenKind.BATTLE, template("battle_anchor_live_v4.png"), Rect(1120, 560, 250, 170), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("reward_menu_anchor", ScreenKind.REWARD_MENU, template("reward_menu_anchor.png"), Rect(416, 257, 212, 47), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("reward_menu_single_anchor", ScreenKind.REWARD_MENU, template("reward_menu_single_anchor.png"), Rect(416, 206, 212, 43), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("reward_anchor", ScreenKind.REWARD_CARDS, template("reward_anchor.png"), Rect(400, 110, 700, 220), 0.80),
            AnchorDefinition("reward_gold_only_anchor_live", ScreenKind.REWARD_GOLD_ONLY, template("reward_gold_only_anchor_live.png"), Rect(416, 206, 212, 43), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("rest_anchor", ScreenKind.REST, template("rest_anchor.png"), Rect(720, 180, 500, 140), 0.94),
            AnchorDefinition("shop_anchor", ScreenKind.SHOP, template("shop_anchor.png"), Rect(1420, 130, 360, 120), 0.94),
            AnchorDefinition("shop_anchor_live_v2", ScreenKind.SHOP, template("shop_anchor_live_v2.png"), Rect(930, 420, 520, 430), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("shop_leave_anchor_live", ScreenKind.SHOP, template("shop_leave_anchor_live.png"), Rect(20, 680, 150, 190), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("shop_anchor_live_v3", ScreenKind.SHOP, template("shop_anchor_live_v3.png"), Rect(900, 390, 560, 470), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("shop_leave_anchor_live_v2", ScreenKind.SHOP, template("shop_leave_anchor_live_v2.png"), Rect(15, 675, 155, 215), 0.80, scale_template=False, scale_region=False),
            AnchorDefinition("game_over_anchor", ScreenKind.GAME_OVER, template("game_over_anchor.png"), Rect(630, 140, 660, 180), 0.94),
            AnchorDefinition("game_over_anchor_live_v2", ScreenKind.GAME_OVER, template("game_over_anchor_live_v2.png"), Rect(610, 150, 340, 180), 0.82, scale_template=False, scale_region=False),
            AnchorDefinition("game_over_main_menu_anchor_live", ScreenKind.GAME_OVER, template("game_over_main_menu_anchor_live.png"), Rect(551, 623, 243, 86), 0.80, scale_template=False, scale_region=False),
        ],
        text_regions=[
            TextRegionDefinition("hp", Rect(115, 12, 170, 56), whitelist="0123456789/", parser="pair"),
            TextRegionDefinition("gold", Rect(325, 12, 130, 56), whitelist="0123456789"),
            TextRegionDefinition("energy", Rect(35, 790, 120, 150), whitelist="0123456789/"),
            TextRegionDefinition("floor", Rect(760, 0, 150, 70), whitelist="0123456789"),
        ],
        memory_read=MemoryReadConfig(
            enabled=False,
            module="sts2.dll",
            refresh_ms=250,
            fields=[],
        ),
        start_actions=[
            ActionDefinition(ScreenKind.MAP, ActionKind.CHOOSE_PATH, "Begin run", (960, 900), tags=["bootstrap"]),
        ],
        actions=[
            ActionDefinition(
                ScreenKind.MENU,
                ActionKind.NAVIGATE,
                "Single Play",
                (540, 490),
                tags=["menu", "start"],
                payload={"target": "single_play"},
                keys=["up", "up", "up", "up", "up", "up", "enter"],
                buttons=["dpad_up", "dpad_up", "dpad_up", "dpad_up", "dpad_up", "dpad_up", "a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=900,
            ),
            ActionDefinition(
                ScreenKind.PROFILE_SELECT,
                ActionKind.NAVIGATE,
                "Select profile 1",
                (0, 0),
                tags=["menu", "start", "profile"],
                payload={"target": "profile_1"},
                key="enter",
                buttons=["a"],
                settle_ms=1200,
                hold_ms=120,
                gap_ms=300,
            ),
            ActionDefinition(
                ScreenKind.MODE_SELECT,
                ActionKind.NAVIGATE,
                "Standard run",
                (0, 0),
                tags=["menu", "start"],
                payload={"target": "standard"},
                buttons=["dpad_left", "a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=900,
            ),
            ActionDefinition(
                ScreenKind.CHARACTER_SELECT,
                ActionKind.NAVIGATE,
                "Select Ironclad",
                (0, 0),
                tags=["menu", "start", "ironclad"],
                payload={"target": "ironclad"},
                keys=["down", "up", "enter"],
                buttons=["dpad_down", "dpad_up", "a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=900,
            ),
            ActionDefinition(
                ScreenKind.NEOW_CHOICE,
                ActionKind.NAVIGATE,
                "Take default whale option",
                (0, 0),
                tags=["start", "neow"],
                payload={"target": "neow_default"},
                key="enter",
                buttons=["a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=900,
            ),
            ActionDefinition(
                ScreenKind.NEOW_DIALOG,
                ActionKind.NAVIGATE,
                "Advance whale dialog",
                (450, 850),
                tags=["start", "dialog"],
                payload={"target": "advance"},
                buttons=["dpad_down", "a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=1000,
            ),
            ActionDefinition(
                ScreenKind.RELIC_POPUP,
                ActionKind.NAVIGATE,
                "Close relic popup",
                (0, 0),
                tags=["start", "dialog"],
                payload={"target": "close_relic"},
                key="enter",
                buttons=["a"],
                settle_ms=2500,
                hold_ms=700,
                gap_ms=900,
            ),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 1", (294, 294), tags=["attack"], payload={"card": "slot_1"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 2", (510, 294), tags=["attack"], payload={"card": "slot_2"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 3", (725, 294), tags=["attack"], payload={"card": "slot_3"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 4", (944, 294), tags=["attack"], payload={"card": "slot_4"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 5", (1154, 294), tags=["attack"], payload={"card": "slot_5"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 6", (294, 631), tags=["block"], payload={"card": "slot_6"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 7", (510, 631), tags=["block"], payload={"card": "slot_7"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 8", (725, 631), tags=["block"], payload={"card": "slot_8"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 9", (944, 631), tags=["block"], payload={"card": "slot_9"}),
            ActionDefinition(ScreenKind.CARD_GRID, ActionKind.PICK_CARD, "Card slot 10", (1154, 631), tags=["starter_core"], payload={"card": "slot_10"}),
            ActionDefinition(ScreenKind.CONFIRM_POPUP, ActionKind.NAVIGATE, "Confirm modal", (1432, 690), tags=["confirm"], payload={"target": "confirm_modal"}),
            ActionDefinition(
                ScreenKind.CONTINUE,
                ActionKind.NAVIGATE,
                "Continue",
                (1340, 708),
                tags=["continue", "progress"],
                key="enter",
                buttons=["y"],
                hold_ms=80,
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Jungle: Push through",
                (1034, 560),
                tags=["gold", "progress", "hp_cost"],
                payload={"event": "jungle", "choice": "push_through"},
                template_path=template("event_jungle_option_top.png"),
                region=Rect(516, 326, 410, 47),
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Jungle: Rest and fight",
                (1034, 619),
                tags=["heal", "combat", "progress"],
                payload={"event": "jungle", "choice": "rest_then_fight"},
                template_path=template("event_jungle_option_bottom.png"),
                region=Rect(516, 385, 410, 47),
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Jungle: Fight!",
                (1034, 564),
                tags=["combat", "progress"],
                payload={"event": "jungle", "choice": "fight"},
                template_path=template("event_jungle_fight_option.png"),
                region=Rect(515, 330, 410, 47),
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Proceed event",
                (0, 0),
                tags=["progress"],
                payload={"target": "generic_event_proceed"},
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Event option 1",
                (0, 0),
                tags=["progress"],
                payload={"target": "generic_event_option", "option_index": 0},
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Event option 2",
                (0, 0),
                tags=["progress"],
                payload={"target": "generic_event_option", "option_index": 1},
            ),
            ActionDefinition(
                ScreenKind.EVENT,
                ActionKind.NAVIGATE,
                "Event option 3",
                (0, 0),
                tags=["progress"],
                payload={"target": "generic_event_option", "option_index": 2},
            ),
            ActionDefinition(ScreenKind.MAP, ActionKind.CHOOSE_PATH, "Take highlighted node", (404, 495), tags=["safe", "progress"], payload={"path": "highlighted"}),
            ActionDefinition(ScreenKind.MAP, ActionKind.CHOOSE_PATH, "Elite path", (1048, 495), tags=["elite", "aggressive"], payload={"path": "elite"}),
            ActionDefinition(ScreenKind.MAP, ActionKind.CHOOSE_PATH, "Safe path", (830, 489), tags=["safe"], payload={"path": "safe"}),
            ActionDefinition(
                ScreenKind.REWARD_MENU,
                ActionKind.NAVIGATE,
                "Take gold",
                (522, 228),
                tags=["gold", "progress"],
                payload={"target": "gold"},
                template_path=template("reward_gold_action.png"),
                region=Rect(416, 206, 212, 42),
                buttons=["a"],
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.REWARD_MENU,
                ActionKind.NAVIGATE,
                "Open card reward",
                (522, 281),
                tags=["reward", "progress"],
                payload={"target": "card_reward"},
                template_path=template("reward_card_action.png"),
                region=Rect(416, 257, 212, 47),
                buttons=["a"],
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.REWARD_MENU,
                ActionKind.NAVIGATE,
                "Open card reward",
                (522, 228),
                tags=["reward", "progress"],
                payload={"target": "card_reward"},
                template_path=template("reward_card_action_single.png"),
                region=Rect(416, 206, 212, 43),
                buttons=["a"],
                scale_template=False,
                scale_region=False,
            ),
            ActionDefinition(
                ScreenKind.REWARD_CARDS,
                ActionKind.PICK_CARD,
                "Card option 1",
                (485, 530),
                tags=["attack"],
                payload={"card": "slot_1"},
                key="1",
                buttons=["dpad_left", "a"],
                hold_ms=80,
            ),
            ActionDefinition(
                ScreenKind.REWARD_CARDS,
                ActionKind.PICK_CARD,
                "Card option 2",
                (780, 530),
                tags=["block"],
                payload={"card": "slot_2"},
                key="2",
                buttons=["a"],
                hold_ms=80,
            ),
            ActionDefinition(
                ScreenKind.REWARD_CARDS,
                ActionKind.PICK_CARD,
                "Card option 3",
                (1075, 530),
                tags=["scaling"],
                payload={"card": "slot_3"},
                key="3",
                buttons=["dpad_right", "a"],
                hold_ms=80,
            ),
            ActionDefinition(
                ScreenKind.REWARD_CARDS,
                ActionKind.SKIP_REWARD,
                "Skip reward",
                (780, 905),
                tags=["skip"],
                key="down",
                buttons=["b"],
                hold_ms=80,
            ),
            ActionDefinition(
                ScreenKind.REWARD_GOLD_ONLY,
                ActionKind.NAVIGATE,
                "Take gold and continue",
                (522, 228),
                tags=["gold", "progress"],
                payload={"target": "gold_only"},
                key="enter",
                buttons=["dpad_down", "a"],
                hold_ms=80,
            ),
            ActionDefinition(ScreenKind.REST, ActionKind.REST, "Rest", (720, 525), tags=["heal"]),
            ActionDefinition(ScreenKind.REST, ActionKind.SMITH, "Smith", (1180, 525), tags=["upgrade"]),
            ActionDefinition(
                ScreenKind.SHOP,
                ActionKind.NAVIGATE,
                "Close detail popup",
                (0, 0),
                tags=["shop", "popup"],
                key="escape",
                buttons=["b"],
                hold_ms=80,
            ),
            ActionDefinition(ScreenKind.SHOP, ActionKind.NAVIGATE, "Leave shop", (62, 710), tags=["progress", "shop_exit"]),
            ActionDefinition(
                ScreenKind.GAME_OVER,
                ActionKind.NAVIGATE,
                "Main menu",
                (0, 0),
                tags=["menu", "restart"],
                key="down",
                keys=["down", "enter"],
                buttons=["dpad_down", "a"],
                settle_ms=400,
                hold_ms=80,
                gap_ms=120,
            ),
            ActionDefinition(
                ScreenKind.BATTLE,
                ActionKind.PLAY_CARD,
                "Play basic turn",
                (0, 0),
                tags=["combat", "progress"],
                payload={"battle_macro": "basic_turn"},
            ),
            ActionDefinition(
                ScreenKind.BATTLE,
                ActionKind.NAVIGATE,
                "Use potion 1",
                (423, 47),
                tags=["combat", "potion", "consumable"],
                payload={"target": "potion", "slot": 1},
            ),
            ActionDefinition(
                ScreenKind.BATTLE,
                ActionKind.NAVIGATE,
                "Use potion 2",
                (468, 48),
                tags=["combat", "potion", "consumable"],
                payload={"target": "potion", "slot": 2},
            ),
            ActionDefinition(ScreenKind.BATTLE, ActionKind.END_TURN, "End turn", (1685, 920), tags=["pass"], key="e", buttons=["a"], hold_ms=80),
        ],
    )


def write_example_profile(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = example_profile(template_dir=Path("templates"))
    path.write_text(
        json.dumps(profile.to_dict(base_dir=path.parent), indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return path
