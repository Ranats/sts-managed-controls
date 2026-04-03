from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import uuid4

from sts_bot.models import ActionEvaluation, ExecutionObservation, GameAction, GameState, RunSummary
from sts_bot.policy import build_state_snapshot, summarize_choice

DEFAULT_DB_PATH = Path("data") / "runs.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    character TEXT NOT NULL,
    won INTEGER NOT NULL,
    act_reached INTEGER NOT NULL,
    floor_reached INTEGER NOT NULL,
    score INTEGER NOT NULL,
    deck_json TEXT NOT NULL,
    relics_json TEXT NOT NULL,
    picked_cards_json TEXT NOT NULL,
    skipped_cards_json TEXT NOT NULL,
    path_json TEXT NOT NULL,
    strategy_tags_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    screen TEXT NOT NULL,
    floor INTEGER NOT NULL,
    action_kind TEXT NOT NULL,
    action_label TEXT NOT NULL,
    action_score REAL,
    action_payload_json TEXT NOT NULL,
    action_tags_json TEXT NOT NULL,
    action_reasons_json TEXT,
    candidate_scores_json TEXT,
    state_tags_json TEXT NOT NULL,
    state_snapshot_json TEXT,
    run_intent_json TEXT,
    reasoning_summary TEXT,
    provider_name TEXT,
    provider_reasoning_text TEXT,
    expected_outcome_json TEXT,
    observed_outcome_json TEXT,
    verification_status TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS harvest_cases (
    harvest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    screen TEXT NOT NULL,
    trigger TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    confidence REAL,
    asset_root TEXT,
    case_status TEXT NOT NULL DEFAULT 'pending',
    learner_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS harvest_cases_run_step_idx
ON harvest_cases(run_id, step_index);

CREATE TABLE IF NOT EXISTS harvest_assets (
    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    harvest_id INTEGER NOT NULL,
    asset_kind TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(harvest_id) REFERENCES harvest_cases(harvest_id)
);
"""


class RunLogger:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id: str | None = None
        self._step_index = 0

    def init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            existing_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(decisions)")
            }
            migrations = {
                "action_score": "ALTER TABLE decisions ADD COLUMN action_score REAL",
                "action_reasons_json": "ALTER TABLE decisions ADD COLUMN action_reasons_json TEXT",
                "candidate_scores_json": "ALTER TABLE decisions ADD COLUMN candidate_scores_json TEXT",
                "state_snapshot_json": "ALTER TABLE decisions ADD COLUMN state_snapshot_json TEXT",
                "run_intent_json": "ALTER TABLE decisions ADD COLUMN run_intent_json TEXT",
                "reasoning_summary": "ALTER TABLE decisions ADD COLUMN reasoning_summary TEXT",
                "provider_name": "ALTER TABLE decisions ADD COLUMN provider_name TEXT",
                "provider_reasoning_text": "ALTER TABLE decisions ADD COLUMN provider_reasoning_text TEXT",
                "expected_outcome_json": "ALTER TABLE decisions ADD COLUMN expected_outcome_json TEXT",
                "observed_outcome_json": "ALTER TABLE decisions ADD COLUMN observed_outcome_json TEXT",
                "verification_status": "ALTER TABLE decisions ADD COLUMN verification_status TEXT",
            }
            for column_name, statement in migrations.items():
                if column_name not in existing_columns:
                    conn.execute(statement)

    def start_run(self) -> str:
        self._run_id = str(uuid4())
        self._step_index = 0
        return self._run_id

    def log_decision(
        self,
        state: GameState,
        action: GameAction,
        *,
        evaluations: list[ActionEvaluation] | None = None,
        provider_name: str | None = None,
        expected_outcome: object | None = None,
        provider_reasoning: str | None = None,
    ) -> None:
        if self._run_id is None:
            raise RuntimeError("start_run must be called before logging decisions.")
        chosen_evaluation = None
        if evaluations:
            chosen_evaluation = next((item for item in evaluations if item.action_label == action.label), None)
        with sqlite3.connect(self.db_path) as conn:
            state_snapshot = build_state_snapshot(state)
            reasoning_summary = summarize_choice(state, action, evaluations)
            conn.execute(
                """
                INSERT INTO decisions (
                    run_id, step_index, screen, floor, action_kind, action_label,
                    action_score, action_payload_json, action_tags_json,
                    action_reasons_json, candidate_scores_json, state_tags_json,
                    state_snapshot_json, run_intent_json, reasoning_summary,
                    provider_name, provider_reasoning_text, expected_outcome_json,
                    observed_outcome_json, verification_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._run_id,
                    self._step_index,
                    state.screen.value,
                    state.floor,
                    action.kind.value,
                    action.label,
                    chosen_evaluation.score if chosen_evaluation else None,
                    json.dumps(action.payload, ensure_ascii=True),
                    json.dumps(action.tags, ensure_ascii=True),
                    json.dumps(chosen_evaluation.reasons, ensure_ascii=True) if chosen_evaluation else None,
                    json.dumps([asdict(item) for item in evaluations], ensure_ascii=True) if evaluations else None,
                    json.dumps(state.tags, ensure_ascii=True),
                    json.dumps(state_snapshot, ensure_ascii=True),
                    json.dumps(asdict(state.run_intent), ensure_ascii=True) if state.run_intent is not None else None,
                    reasoning_summary,
                    provider_name,
                    provider_reasoning,
                    _serialize_optional_json(expected_outcome),
                    None,
                    None,
                ),
            )
        self._step_index += 1

    def log_verification(
        self,
        *,
        observed_outcome: ExecutionObservation | dict[str, object] | None,
        verification_status: str,
    ) -> None:
        if self._run_id is None:
            raise RuntimeError("start_run must be called before log_verification.")
        if self._step_index <= 0:
            raise RuntimeError("log_decision must be called before log_verification.")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE decisions
                SET observed_outcome_json = ?, verification_status = ?
                WHERE run_id = ? AND step_index = ?
                """,
                (
                    _serialize_optional_json(observed_outcome),
                    verification_status,
                    self._run_id,
                    self._step_index - 1,
                ),
            )

    def log_harvest_case(
        self,
        *,
        state: GameState,
        trigger: str,
        verification_status: str,
        confidence: float | None = None,
        asset_root: str | None = None,
    ) -> int:
        if self._run_id is None:
            raise RuntimeError("start_run must be called before log_harvest_case.")
        if self._step_index <= 0:
            raise RuntimeError("log_decision must be called before log_harvest_case.")
        step_index = self._step_index - 1
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO harvest_cases (
                    run_id, step_index, screen, trigger, verification_status, confidence, asset_root
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._run_id,
                    step_index,
                    state.screen.value,
                    trigger,
                    verification_status,
                    confidence,
                    asset_root,
                ),
            )
            row = conn.execute(
                """
                SELECT harvest_id
                FROM harvest_cases
                WHERE run_id = ? AND step_index = ?
                """,
                (self._run_id, step_index),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create harvest case.")
        return int(row[0])

    def update_harvest_case(
        self,
        harvest_id: int,
        *,
        asset_root: str | None = None,
        case_status: str | None = None,
        learner_note: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if asset_root is not None:
            assignments.append("asset_root = ?")
            values.append(asset_root)
        if case_status is not None:
            assignments.append("case_status = ?")
            values.append(case_status)
        if learner_note is not None:
            assignments.append("learner_note = ?")
            values.append(learner_note)
        if not assignments:
            return
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        values.append(harvest_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE harvest_cases SET {', '.join(assignments)} WHERE harvest_id = ?",
                values,
            )

    def log_harvest_asset(
        self,
        *,
        harvest_id: int,
        asset_kind: str,
        path: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO harvest_assets (harvest_id, asset_kind, path, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    harvest_id,
                    asset_kind,
                    path,
                    json.dumps(metadata, ensure_ascii=True) if metadata is not None else None,
                ),
            )

    def pending_harvest_cases(self, *, limit: int = 8, status: str = "pending") -> list[dict[str, object]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT h.harvest_id, h.run_id, h.step_index, h.screen, h.trigger,
                       h.verification_status, h.confidence, h.asset_root, h.case_status,
                       d.action_kind, d.action_label, d.action_payload_json, d.action_tags_json,
                       d.action_reasons_json, d.candidate_scores_json, d.state_snapshot_json,
                       d.reasoning_summary, d.provider_name, d.provider_reasoning_text,
                       d.expected_outcome_json, d.observed_outcome_json
                FROM harvest_cases h
                JOIN decisions d ON d.run_id = h.run_id AND d.step_index = h.step_index
                WHERE h.case_status = ?
                ORDER BY h.harvest_id
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            asset_rows = conn.execute(
                """
                SELECT harvest_id, asset_kind, path, metadata_json
                FROM harvest_assets
                WHERE harvest_id IN (
                    SELECT harvest_id FROM harvest_cases
                    WHERE case_status = ?
                    ORDER BY harvest_id
                    LIMIT ?
                )
                ORDER BY asset_id
                """,
                (status, limit),
            ).fetchall()

        assets_by_case: dict[int, list[dict[str, object]]] = {}
        for harvest_id, asset_kind, path, metadata_json in asset_rows:
            assets_by_case.setdefault(int(harvest_id), []).append(
                {
                    "asset_kind": str(asset_kind),
                    "path": str(path),
                    "metadata": json.loads(metadata_json) if metadata_json else {},
                }
            )

        payload: list[dict[str, object]] = []
        for row in rows:
            (
                harvest_id,
                run_id,
                step_index,
                screen,
                trigger,
                verification_status,
                confidence,
                asset_root,
                case_status,
                action_kind,
                action_label,
                action_payload_json,
                action_tags_json,
                action_reasons_json,
                candidate_scores_json,
                state_snapshot_json,
                reasoning_summary,
                provider_name,
                provider_reasoning_text,
                expected_outcome_json,
                observed_outcome_json,
            ) = row
            payload.append(
                {
                    "harvest_id": int(harvest_id),
                    "run_id": str(run_id),
                    "step_index": int(step_index),
                    "screen": str(screen),
                    "trigger": str(trigger),
                    "verification_status": str(verification_status),
                    "confidence": None if confidence is None else float(confidence),
                    "asset_root": str(asset_root) if asset_root else "",
                    "case_status": str(case_status),
                    "action_kind": str(action_kind),
                    "action_label": str(action_label),
                    "action_payload": json.loads(action_payload_json) if action_payload_json else {},
                    "action_tags": json.loads(action_tags_json) if action_tags_json else [],
                    "action_reasons": json.loads(action_reasons_json) if action_reasons_json else [],
                    "candidate_scores": json.loads(candidate_scores_json) if candidate_scores_json else [],
                    "state_snapshot": json.loads(state_snapshot_json) if state_snapshot_json else {},
                    "reasoning_summary": str(reasoning_summary or ""),
                    "provider_name": str(provider_name or ""),
                    "provider_reasoning_text": str(provider_reasoning_text or ""),
                    "expected_outcome": json.loads(expected_outcome_json) if expected_outcome_json else {},
                    "observed_outcome": json.loads(observed_outcome_json) if observed_outcome_json else {},
                    "assets": assets_by_case.get(int(harvest_id), []),
                }
            )
        return payload

    def mark_harvest_cases(
        self,
        harvest_ids: list[int],
        *,
        case_status: str,
        learner_note: str | None = None,
    ) -> None:
        if not harvest_ids:
            return
        placeholders = ", ".join("?" for _ in harvest_ids)
        values: list[object] = [case_status, learner_note, *harvest_ids]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE harvest_cases
                SET case_status = ?, learner_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE harvest_id IN ({placeholders})
                """,
                values,
            )

    def finish_run(self, summary: RunSummary) -> str:
        if self._run_id is None:
            raise RuntimeError("start_run must be called before finish_run.")
        run_id = self._run_id
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, character, won, act_reached, floor_reached, score,
                    deck_json, relics_json, picked_cards_json, skipped_cards_json,
                    path_json, strategy_tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    summary.character,
                    int(summary.won),
                    summary.act_reached,
                    summary.floor_reached,
                    summary.score,
                    json.dumps([asdict(card) for card in summary.deck], ensure_ascii=True),
                    json.dumps(summary.relics, ensure_ascii=True),
                    json.dumps(summary.picked_cards, ensure_ascii=True),
                    json.dumps(summary.skipped_cards, ensure_ascii=True),
                    json.dumps(summary.path, ensure_ascii=True),
                    json.dumps(summary.strategy_tags, ensure_ascii=True),
                ),
            )
        self._run_id = None
        return run_id


def _serialize_optional_json(value: object | None) -> str | None:
    if value is None:
        return None
    if is_dataclass(value):
        return json.dumps(asdict(value), ensure_ascii=True)
    return json.dumps(value, ensure_ascii=True)
