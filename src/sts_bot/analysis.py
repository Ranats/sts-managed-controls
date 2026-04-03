from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from sts_bot.logging import DEFAULT_DB_PATH
from sts_bot.models import BuildInsight


def analyze_builds(db_path: Path = DEFAULT_DB_PATH, min_samples: int = 2) -> list[BuildInsight]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT character, won, picked_cards_json, relics_json, strategy_tags_json
            FROM runs
            """
        ).fetchall()

    grouped: dict[tuple[str, str], list[tuple[int, list[str], list[str]]]] = defaultdict(list)
    for character, won, picked_cards_json, relics_json, strategy_tags_json in rows:
        tags = json.loads(strategy_tags_json)
        label = " + ".join(tags) if tags else "untagged"
        grouped[(character, label)].append(
            (
                int(won),
                json.loads(picked_cards_json),
                json.loads(relics_json),
            )
        )

    insights: list[BuildInsight] = []
    for (character, label), samples in grouped.items():
        if len(samples) < min_samples:
            continue
        wins = sum(sample[0] for sample in samples)
        picked_counter: Counter[str] = Counter()
        relic_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter(label.split(" + ")) if label else Counter()

        for _, picked_cards, relics in samples:
            picked_counter.update(picked_cards)
            relic_counter.update(relics)

        insights.append(
            BuildInsight(
                character=character,
                label=label,
                sample_size=len(samples),
                win_rate=wins / len(samples),
                anchor_cards=[name for name, _ in picked_counter.most_common(5)],
                anchor_relics=[name for name, _ in relic_counter.most_common(3)],
                strategy_tags=[name for name, _ in tag_counter.most_common()],
            )
        )

    insights.sort(key=lambda item: (item.win_rate, item.sample_size), reverse=True)
    return insights


def render_report(insights: list[BuildInsight]) -> str:
    if not insights:
        return "No build insights yet. Run more episodes first."

    lines: list[str] = []
    for insight in insights:
        lines.append(
            (
                f"{insight.character} | {insight.label} | "
                f"win_rate={insight.win_rate:.1%} | samples={insight.sample_size}"
            )
        )
        lines.append(f"  anchor_cards: {', '.join(insight.anchor_cards) or '-'}")
        lines.append(f"  anchor_relics: {', '.join(insight.anchor_relics) or '-'}")
        lines.append(f"  tags: {', '.join(insight.strategy_tags) or '-'}")
    return "\n".join(lines)


def export_report_json(insights: list[BuildInsight], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(insight) for insight in insights]
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def latest_run_id(db_path: Path = DEFAULT_DB_PATH) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT run_id
            FROM decisions
            ORDER BY decision_id DESC
            LIMIT 1
            """
        ).fetchone()
    return None if row is None else str(row[0])


def load_run_trace(db_path: Path = DEFAULT_DB_PATH, *, run_id: str | None = None) -> tuple[str | None, list[dict[str, object]]]:
    selected_run_id = run_id or latest_run_id(db_path)
    if selected_run_id is None:
        return None, []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT step_index, screen, floor, action_kind, action_label, action_score,
                   action_reasons_json, state_tags_json, state_snapshot_json, run_intent_json, reasoning_summary,
                   provider_name, provider_reasoning_text, expected_outcome_json, observed_outcome_json, verification_status
            FROM decisions
            WHERE run_id = ?
            ORDER BY step_index
            """,
            (selected_run_id,),
        ).fetchall()
    trace: list[dict[str, object]] = []
    for row in rows:
        (
            step_index,
            screen,
            floor,
            action_kind,
            action_label,
            action_score,
            action_reasons_json,
            state_tags_json,
            state_snapshot_json,
            run_intent_json,
            reasoning_summary,
            provider_name,
            provider_reasoning_text,
            expected_outcome_json,
            observed_outcome_json,
            verification_status,
        ) = row
        trace.append(
            {
                "step_index": step_index,
                "screen": screen,
                "floor": floor,
                "action_kind": action_kind,
                "action_label": action_label,
                "action_score": action_score,
                "action_reasons": json.loads(action_reasons_json) if action_reasons_json else [],
                "state_tags": json.loads(state_tags_json) if state_tags_json else [],
                "state_snapshot": json.loads(state_snapshot_json) if state_snapshot_json else {},
                "run_intent": json.loads(run_intent_json) if run_intent_json else {},
                "reasoning_summary": reasoning_summary or "",
                "provider_name": provider_name or "",
                "provider_reasoning_text": provider_reasoning_text or "",
                "expected_outcome": json.loads(expected_outcome_json) if expected_outcome_json else {},
                "observed_outcome": json.loads(observed_outcome_json) if observed_outcome_json else {},
                "verification_status": verification_status or "",
            }
        )
    return selected_run_id, trace


def render_run_trace(run_id: str | None, trace: list[dict[str, object]]) -> str:
    if run_id is None or not trace:
        return "No decision trace yet."
    lines = [f"run_id={run_id}"]
    first_snapshot = trace[0].get("state_snapshot") or {}
    if first_snapshot:
        lines.append(
            "start: "
            f"screen={first_snapshot.get('screen')} hp={first_snapshot.get('hp')}/{first_snapshot.get('max_hp')} "
            f"gold={first_snapshot.get('gold')} axes={','.join(first_snapshot.get('deck_axes') or []) or '-'}"
        )
    for row in trace:
        snapshot = row.get("state_snapshot") or {}
        enemy_count = snapshot.get("enemy_count", 0)
        incoming = snapshot.get("incoming_intent", snapshot.get("incoming_damage", 0))
        axes = snapshot.get("deck_axes") or []
        lines.append(
            f"[{int(row['step_index']):03d}] screen={row['screen']} floor={row['floor']} "
            f"action={row['action_label']} score={row['action_score'] if row['action_score'] is not None else '-'}"
        )
        provider_name = str(row.get("provider_name") or "").strip()
        if provider_name:
            lines.append(f"  provider: {provider_name}")
        summary = str(row.get("reasoning_summary") or "").strip()
        if summary:
            lines.append(f"  why: {summary}")
        provider_reasoning = str(row.get("provider_reasoning_text") or "").strip()
        if provider_reasoning and provider_reasoning != summary:
            lines.append(f"  model: {provider_reasoning}")
        run_intent = row.get("run_intent") or snapshot.get("run_intent") or {}
        if run_intent:
            lines.append(
                "  intent: "
                f"axes={','.join(run_intent.get('deck_axes') or []) or '-'} "
                f"survival={run_intent.get('short_term_survival_need', '-')} "
                f"direction={run_intent.get('long_term_direction', '-')} "
                f"risk={run_intent.get('elite_boss_risk_posture', '-')}"
            )
        lines.append(
            f"  state: hp={snapshot.get('hp')}/{snapshot.get('max_hp')} energy={snapshot.get('energy')} "
            f"gold={snapshot.get('gold')} enemies={enemy_count} incoming={incoming} axes={','.join(axes) if axes else '-'} "
            f"source={snapshot.get('state_source') or '-'}"
        )
        metric_source_text = _format_trace_metric_sources(snapshot.get("metric_sources"))
        if metric_source_text:
            lines.append(f"  metrics: {metric_source_text}")
        reasons = row.get("action_reasons") or []
        if reasons:
            lines.append(f"  reasons: {', '.join(str(reason) for reason in reasons[:6])}")
        expected = row.get("expected_outcome") or {}
        if expected:
            lines.append(
                "  expected: "
                f"next_screen={expected.get('next_screen') or '-'} "
                f"change={expected.get('change_summary') or '-'}"
            )
        observed = row.get("observed_outcome") or {}
        if observed:
            lines.append(
                "  observed: "
                f"screen={observed.get('screen', '-')} hp={observed.get('hp', '-')}/{observed.get('max_hp', '-')} "
                f"energy={observed.get('energy', '-')} gold={observed.get('gold', '-')} floor={observed.get('floor', '-')} "
                f"source={observed.get('state_source', '-') or '-'}"
            )
            observed_metric_text = _format_trace_metric_sources(observed.get("metric_sources"))
            if observed_metric_text:
                lines.append(f"  observed_metrics: {observed_metric_text}")
            note = str(observed.get("note") or "").strip()
            if note:
                lines.append(f"  observed_note: {note}")
        verification = str(row.get("verification_status") or "").strip()
        if verification:
            lines.append(f"  verify: {verification}")
    return "\n".join(lines)


def _format_trace_metric_sources(metric_sources: object) -> str:
    if not isinstance(metric_sources, dict):
        return ""
    parts = [
        f"{name}:{source}"
        for name, source in metric_sources.items()
        if isinstance(source, str) and source
    ]
    return ", ".join(parts[:6])


def export_run_trace_json(run_id: str | None, trace: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "trace": trace}
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def build_fix_request_markdown(run_id: str | None, trace: list[dict[str, object]]) -> str:
    if run_id is None or not trace:
        return "# Fix Request\n\nNo decision trace was available.\n"

    mismatches = [
        row
        for row in trace
        if str(row.get("verification_status") or "").strip() in {"unknown", "partial", "mismatch"}
    ]
    by_screen: Counter[str] = Counter(str(row.get("screen") or "unknown") for row in mismatches)
    by_provider: Counter[str] = Counter(str(row.get("provider_name") or "unknown") for row in mismatches)

    lines = [
        "# Fix Request",
        "",
        f"- run_id: `{run_id}`",
        f"- total_steps: {len(trace)}",
        f"- mismatched_steps: {len(mismatches)}",
        f"- mismatch_screens: {', '.join(f'{screen}={count}' for screen, count in by_screen.most_common()) or '-'}",
        f"- mismatch_providers: {', '.join(f'{provider}={count}' for provider, count in by_provider.most_common()) or '-'}",
        "",
        "## Priority Issues",
    ]
    if not mismatches:
        lines.extend(
            [
                "",
                "No verification mismatches were recorded in this trace.",
                "",
                "## Suggested Next Step",
                "",
                "- Run another live loop and collect more examples before changing code.",
            ]
        )
        return "\n".join(lines) + "\n"

    for index, row in enumerate(mismatches[:5], start=1):
        expected = row.get("expected_outcome") or {}
        observed = row.get("observed_outcome") or {}
        lines.extend(
            [
                "",
                f"{index}. screen={row.get('screen')} floor={row.get('floor')} action={row.get('action_label')}",
                f"   provider={row.get('provider_name') or '-'} verify={row.get('verification_status') or '-'}",
                f"   expected_next={expected.get('next_screen') or '-'} change={expected.get('change_summary') or '-'}",
                f"   observed_screen={observed.get('screen', '-')} hp={observed.get('hp', '-')}/{observed.get('max_hp', '-')} energy={observed.get('energy', '-')}",
            ]
        )
        reasoning = str(row.get("provider_reasoning_text") or row.get("reasoning_summary") or "").strip()
        if reasoning:
            lines.append(f"   reasoning={reasoning}")

    lines.extend(
        [
            "",
            "## Suggested Next Step",
            "",
            "- Reproduce the top mismatch in live mode and save before/after captures.",
            "- If the mismatch is a scene classification error, update screen detection or action gating first.",
            "- If the mismatch is an execution error, adjust the scene-specific helper or fallback backend path.",
        ]
    )
    return "\n".join(lines) + "\n"


def export_fix_request_markdown(run_id: str | None, trace: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_fix_request_markdown(run_id, trace), encoding="utf-8")
