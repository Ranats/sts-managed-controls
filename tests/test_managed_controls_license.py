from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sts_bot.managed_controls_license import (
    activate_managed_controls,
    ensure_managed_controls_access,
    get_managed_controls_license_status,
    ManagedControlsLicenseError,
)


class ManagedControlsLicenseTest(unittest.TestCase):
    def test_status_creates_trial_and_reports_remaining_time(self) -> None:
        tmp_dir = _workspace_test_dir("status")
        try:
            with patch.dict(os.environ, {"STS_MANAGED_CONTROLS_HOME": str(tmp_dir)}, clear=False):
                now = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                status = get_managed_controls_license_status("probe-managed", now=now)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertTrue(status.can_use)
        self.assertFalse(status.unlocked)
        self.assertEqual(status.install_id != "", True)
        self.assertGreater(status.remaining_seconds, 0)

    def test_ensure_access_blocks_after_trial_expiry(self) -> None:
        tmp_dir = _workspace_test_dir("expiry")
        try:
            with patch.dict(os.environ, {"STS_MANAGED_CONTROLS_HOME": str(tmp_dir)}, clear=False):
                start = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                get_managed_controls_license_status("probe-managed", now=start)
                with self.assertRaises(ManagedControlsLicenseError) as cm:
                    ensure_managed_controls_access("probe-managed", now=start + timedelta(minutes=31))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn("Trial expired", str(cm.exception))

    def test_activate_unlocks_unlimited_mode(self) -> None:
        tmp_dir = _workspace_test_dir("activate")
        try:
            with patch.dict(os.environ, {"STS_MANAGED_CONTROLS_HOME": str(tmp_dir)}, clear=False):
                start = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                status = get_managed_controls_license_status("probe-managed", now=start)
                expected = _expected_key(status.install_id)
                unlocked = activate_managed_controls(expected, now=start + timedelta(minutes=40))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertTrue(unlocked.unlocked)
        self.assertTrue(unlocked.can_use)
        self.assertEqual(unlocked.remaining_seconds, 0)

    def test_activate_rejects_bad_key(self) -> None:
        tmp_dir = _workspace_test_dir("bad_key")
        try:
            with patch.dict(os.environ, {"STS_MANAGED_CONTROLS_HOME": str(tmp_dir)}, clear=False):
                get_managed_controls_license_status("probe-managed")
                with self.assertRaises(ManagedControlsLicenseError):
                    activate_managed_controls("BAD-KEY")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _expected_key(install_id: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{install_id}:sts-managed-controls-soft-unlock-v1".encode("utf-8")).hexdigest().upper()
    return f"SMC1-{install_id.upper()}-{digest[:12]}"


def _workspace_test_dir(name: str) -> Path:
    root = Path(__file__).resolve().parents[1] / "tmp" / "test_managed_controls_license" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


if __name__ == "__main__":
    unittest.main()
