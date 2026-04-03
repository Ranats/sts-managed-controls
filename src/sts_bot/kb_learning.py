from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from sts_bot.knowledge import active_kb_overlay_path, apply_overlay_operations, overlay_snapshot
from sts_bot.logging import RunLogger


@dataclass(slots=True)
class KBLearningResult:
    processed_cases: int
    applied_operations: int
    ignored_operations: int
    summary: str = ""
    overlay_path: str = ""


class CodexKBLearner:
    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        timeout_seconds: float = 60.0,
        workspace_dir: Path | None = None,
        min_cases: int = 3,
        max_cases: int = 8,
        cooldown_seconds: float = 90.0,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.workspace_dir = workspace_dir
        self.min_cases = max(1, min_cases)
        self.max_cases = max(self.min_cases, max_cases)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._last_run_at = 0.0

    def maybe_learn(self, logger: RunLogger) -> KBLearningResult | None:
        now = time.time()
        if (now - self._last_run_at) < self.cooldown_seconds:
            return None
        cases = logger.pending_harvest_cases(limit=self.max_cases, status="pending")
        if len(cases) < self.min_cases:
            return None

        response = self._invoke_codex(cases)
        operations = response.get("operations")
        summary = str(response.get("summary") or "").strip()
        if not isinstance(operations, list):
            logger.mark_harvest_cases(
                [int(case["harvest_id"]) for case in cases],
                case_status="ignored",
                learner_note=summary or "kb_learning_invalid_response",
            )
            self._last_run_at = now
            return KBLearningResult(
                processed_cases=len(cases),
                applied_operations=0,
                ignored_operations=0,
                summary=summary or "invalid response",
                overlay_path=str(active_kb_overlay_path()),
            )

        apply_result = apply_overlay_operations(
            [item for item in operations if isinstance(item, dict)],
            path=active_kb_overlay_path(),
        )
        logger.mark_harvest_cases(
            [int(case["harvest_id"]) for case in cases],
            case_status="learned" if apply_result["applied"] > 0 else "ignored",
            learner_note=summary or "kb_learning_applied",
        )
        self._last_run_at = now
        return KBLearningResult(
            processed_cases=len(cases),
            applied_operations=int(apply_result["applied"]),
            ignored_operations=int(apply_result["ignored"]),
            summary=summary,
            overlay_path=str(apply_result["path"]),
        )

    def _invoke_codex(self, cases: list[dict[str, object]]) -> dict[str, object]:
        overlay = overlay_snapshot(max_entries=12)
        payload = {
            "kb_overlay_path": str(active_kb_overlay_path()),
            "overlay_snapshot": overlay,
            "cases": cases,
            "target": "Produce small, explainable overlay KB updates only.",
            "rules": [
                "Prefer conservative updates backed by repeated evidence.",
                "Use target_type=choice with target_key=<domain>:<key> for choice rules.",
                "Use target_type=card|relic|potion|boss_relic with target_key as the canonical item name.",
                "Use target_type=alias with target_key=<entity>:<ocr alias> and payload.canonical=<canonical name>.",
                "Never remove existing rules. Only upsert overlay entries.",
                "Keep operations small and specific. Max 6 operations.",
            ],
        }
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_type": {
                                "type": "string",
                                "enum": ["choice", "card", "relic", "potion", "boss_relic", "alias"],
                            },
                            "target_key": {"type": "string"},
                            "payload": {"type": "object"},
                            "reason": {"type": "string"},
                        },
                        "required": ["target_type", "target_key", "payload", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "operations"],
            "additionalProperties": False,
        }
        prompt = (
            "You are updating a symbolic Slay the Spire overlay KB from harvested live failures.\n"
            "Return strict JSON only.\n"
            "Focus on explainable OCR aliases and scoring updates.\n\n"
            f"{json.dumps(payload, ensure_ascii=True, indent=2)}"
        )
        temp_root = self.workspace_dir if self.workspace_dir is not None and self.workspace_dir.exists() else None
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "kb_learning_schema.json"
            output_path = temp_path / "kb_learning_output.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=True), encoding="utf-8")
            command = [
                "cmd",
                "/c",
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--model",
                self.model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.workspace_dir is not None:
                command.extend(["--cd", str(self.workspace_dir)])
            command.append(prompt)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, self.timeout_seconds),
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(stderr or f"codex exec failed with exit code {completed.returncode}")
            raw = output_path.read_text(encoding="utf-8").strip()
            return json.loads(raw)
