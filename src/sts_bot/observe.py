from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from sts_bot.adapters.windows_stub import WindowsStsAdapter
from sts_bot.models import GameAction, GameState


@dataclass(slots=True)
class ObservationRecord:
    timestamp: float
    sample_index: int
    screen: str
    act: int
    floor: int
    hp: int
    max_hp: int
    gold: int
    energy: int
    actions: list[dict[str, object]]
    anchor_scores: dict[str, float]
    metrics: dict[str, object]
    capture_path: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "timestamp": self.timestamp,
                "sample_index": self.sample_index,
                "screen": self.screen,
                "act": self.act,
                "floor": self.floor,
                "hp": self.hp,
                "max_hp": self.max_hp,
                "gold": self.gold,
                "energy": self.energy,
                "actions": self.actions,
                "anchor_scores": self.anchor_scores,
                "metrics": self.metrics,
                "capture_path": self.capture_path,
            },
            ensure_ascii=True,
        )


def action_to_dict(action: GameAction) -> dict[str, object]:
    return {
        "kind": action.kind.value,
        "label": action.label,
        "payload": dict(action.payload),
        "tags": action.tags[:],
    }


def state_to_record(
    adapter: WindowsStsAdapter,
    state: GameState,
    sample_index: int,
    capture_path: Path | None = None,
) -> ObservationRecord:
    return ObservationRecord(
        timestamp=time.time(),
        sample_index=sample_index,
        screen=state.screen.value,
        act=state.act,
        floor=state.floor,
        hp=state.hp,
        max_hp=state.max_hp,
        gold=state.gold,
        energy=state.energy,
        actions=[action_to_dict(action) for action in state.available_actions],
        anchor_scores=adapter.last_anchor_scores(),
        metrics=adapter.last_metrics(),
        capture_path=str(capture_path) if capture_path is not None else None,
    )


def append_jsonl(path: Path, record: ObservationRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json())
        handle.write("\n")
