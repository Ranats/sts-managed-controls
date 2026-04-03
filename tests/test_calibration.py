from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from sts_bot.calibration import annotate_profile, crop_to_file
from sts_bot.config import example_profile


class CalibrationTest(unittest.TestCase):
    def test_crop_to_file_and_annotate_profile_write_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            cropped = tmp_path / "crop.png"
            annotated = tmp_path / "annotated.png"

            Image.new("RGB", (1920, 1080), color=(20, 20, 20)).save(source)

            crop_to_file(source, example_profile().anchors[1].region, cropped)
            annotate_profile(source, example_profile(), annotated)

            self.assertTrue(cropped.exists())
            self.assertTrue(annotated.exists())
            self.assertGreater(cropped.stat().st_size, 0)
            self.assertGreater(annotated.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
