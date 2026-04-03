from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sts_bot.kb_learning import CodexKBLearner
from sts_bot.knowledge import (
    active_kb_overlay_path,
    canonicalize_card_name,
    lookup_event_choice,
    set_active_kb_overlay_path,
)
from sts_bot.logging import RunLogger
from sts_bot.models import ActionKind, ExecutionObservation, GameAction, GameState, RunSummary, ScreenKind


class KBLearningTest(unittest.TestCase):
    def test_learner_applies_overlay_updates_from_pending_harvest_cases(self) -> None:
        Path("tmp").mkdir(exist_ok=True)
        fd, raw_path = tempfile.mkstemp(suffix=".sqlite3", dir=Path("tmp"))
        os.close(fd)
        db_path = Path(raw_path)
        overlay_path = Path("tmp") / "kb_overlay_learning_test.json"
        case_path = Path("tmp") / "kb_learning_case.json"
        if overlay_path.exists():
            overlay_path.unlink()
        if case_path.exists():
            case_path.unlink()
        original_overlay_path = active_kb_overlay_path()
        try:
            set_active_kb_overlay_path(overlay_path)
            logger = RunLogger(db_path)
            logger.init_db()
            logger.start_run()
            state = GameState(
                screen=ScreenKind.EVENT,
                act=1,
                floor=9,
                hp=24,
                max_hp=80,
                energy=3,
                gold=117,
                character="Ironclad",
            )
            logger.log_decision(
                state,
                GameAction(
                    ActionKind.NAVIGATE,
                    "Heal option",
                    {"option_text": "Heal 20 HP", "click_point": (300, 200)},
                    ["heal"],
                ),
                provider_name="codex",
                provider_reasoning="Low HP event choice.",
            )
            logger.log_verification(
                observed_outcome=ExecutionObservation(
                    screen="event",
                    hp=24,
                    max_hp=80,
                    energy=3,
                    gold=117,
                    floor=9,
                    actions=["Heal option"],
                ),
                verification_status="unknown",
            )
            harvest_id = logger.log_harvest_case(
                state=state,
                trigger="verification:unknown",
                verification_status="unknown",
                confidence=0.41,
            )
            logger.log_harvest_asset(
                harvest_id=harvest_id,
                asset_kind="case_json",
                path=str(case_path),
                metadata={"screen": "event"},
            )
            logger.finish_run(
                RunSummary(
                    character="Ironclad",
                    won=False,
                    act_reached=1,
                    floor_reached=9,
                    score=0,
                    deck=[],
                    relics=[],
                    picked_cards=[],
                    skipped_cards=[],
                    path=[],
                    strategy_tags=[],
                )
            )
            learner = CodexKBLearner(
                model="gpt-5.4",
                timeout_seconds=5.0,
                workspace_dir=Path.cwd(),
                min_cases=1,
                max_cases=4,
                cooldown_seconds=0.0,
            )
            with patch.object(
                CodexKBLearner,
                "_invoke_codex",
                return_value={
                    "summary": "Promoted event heal value and OCR alias.",
                    "operations": [
                        {
                            "target_type": "choice",
                            "target_key": "event:heal",
                            "payload": {"name": "Heal", "base_score": 4.5, "tags": ["heal"]},
                            "reason": "Low HP event decisions favored heal repeatedly.",
                        },
                        {
                            "target_type": "alias",
                            "target_key": "card:shrg it off",
                            "payload": {"canonical": "shrug it off"},
                            "reason": "Common OCR miss.",
                        },
                    ],
                },
            ):
                result = learner.maybe_learn(logger)

            self.assertIsNotNone(result)
            self.assertEqual(result.applied_operations, 2)
            self.assertAlmostEqual(lookup_event_choice("heal").base_score, 4.5)
            self.assertEqual(canonicalize_card_name("shrg it off"), "Shrug It Off")
            self.assertEqual(logger.pending_harvest_cases(limit=10), [])
        finally:
            set_active_kb_overlay_path(original_overlay_path)
            if overlay_path.exists():
                overlay_path.unlink()
            if case_path.exists():
                case_path.unlink()
            if db_path.exists():
                try:
                    db_path.unlink()
                except PermissionError:
                    pass

    @patch("sts_bot.kb_learning.subprocess.run")
    def test_invoke_codex_uses_utf8_decode_settings(self, mock_run) -> None:
        Path("tmp").mkdir(exist_ok=True)
        overlay_path = Path("tmp") / "kb_overlay_learning_invoke_test.json"
        fixed_temp = Path("tmp") / "kb_learning_invoke"
        fixed_temp.mkdir(parents=True, exist_ok=True)
        original_overlay_path = active_kb_overlay_path()
        if overlay_path.exists():
            overlay_path.unlink()
        try:
            set_active_kb_overlay_path(overlay_path)

            class _FixedTempDir:
                def __init__(self, path: Path) -> None:
                    self.path = path

                def __enter__(self) -> str:
                    return str(self.path)

                def __exit__(self, exc_type, exc, tb) -> bool:
                    return False

            def _fake_run(command, **kwargs):
                output_index = command.index("--output-last-message") + 1
                Path(command[output_index]).write_text(
                    '{"summary":"ok","operations":[]}',
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            mock_run.side_effect = _fake_run
            learner = CodexKBLearner(
                model="gpt-5.4",
                timeout_seconds=5.0,
                workspace_dir=Path.cwd(),
                min_cases=1,
                max_cases=2,
                cooldown_seconds=0.0,
            )

            with patch("sts_bot.kb_learning.tempfile.TemporaryDirectory", return_value=_FixedTempDir(fixed_temp)):
                payload = learner._invoke_codex(
                    [
                        {
                            "harvest_id": 1,
                            "screen": "event",
                            "trigger": "verification:unknown",
                            "verification_status": "unknown",
                            "action_label": "Heal option",
                        }
                    ]
                )
        finally:
            set_active_kb_overlay_path(original_overlay_path)
            if overlay_path.exists():
                overlay_path.unlink()

        self.assertEqual(payload["summary"], "ok")
        self.assertEqual(mock_run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(mock_run.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
