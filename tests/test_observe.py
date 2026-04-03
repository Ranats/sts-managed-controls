from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sts_bot.observe import ObservationRecord, append_jsonl


class ObserveTest(unittest.TestCase):
    def test_append_jsonl_writes_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "obs.jsonl"
            record = ObservationRecord(
                timestamp=1.0,
                sample_index=1,
                screen="battle",
                act=1,
                floor=2,
                hp=10,
                max_hp=20,
                gold=99,
                energy=3,
                actions=[],
                anchor_scores={"battle_anchor": 0.98},
                metrics={"hp": [10, 20]},
                capture_path="frames/0001_battle.png",
            )
            append_jsonl(path, record)

            data = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(data["screen"], "battle")
            self.assertEqual(data["anchor_scores"]["battle_anchor"], 0.98)
            self.assertEqual(data["sample_index"], 1)


if __name__ == "__main__":
    unittest.main()
