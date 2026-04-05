from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from sts_bot.managed_controls_commerce import build_purchase_url, load_managed_controls_commerce_config
from sts_bot.managed_controls_license import (
    activate_managed_controls,
    ensure_managed_controls_access,
    get_managed_controls_license_status,
    issue_managed_controls_license,
    ManagedControlsLicenseError,
)


class ManagedControlsLicenseTest(unittest.TestCase):
    def test_status_creates_trial_and_reports_remaining_time(self) -> None:
        tmp_dir = _workspace_test_dir("status")
        try:
            with _license_test_env(tmp_dir):
                now = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                status = get_managed_controls_license_status("probe-managed", now=now)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertTrue(status.can_use)
        self.assertFalse(status.unlocked)
        self.assertEqual(status.install_id, "SMC-C21C71BF7EC44131")
        self.assertGreater(status.remaining_seconds, 0)

    def test_ensure_access_blocks_after_trial_expiry(self) -> None:
        tmp_dir = _workspace_test_dir("expiry")
        try:
            with _license_test_env(tmp_dir):
                start = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                get_managed_controls_license_status("probe-managed", now=start)
                with self.assertRaises(ManagedControlsLicenseError) as cm:
                    ensure_managed_controls_access("probe-managed", now=start + timedelta(minutes=31))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn("Trial expired", str(cm.exception))

    def test_issue_and_activate_unlocks_unlimited_mode(self) -> None:
        tmp_dir = _workspace_test_dir("activate")
        private_key, public_key = _generate_keypair()
        try:
            with _license_test_env(tmp_dir, public_key):
                start = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
                status = get_managed_controls_license_status("probe-managed", now=start)
                token = issue_managed_controls_license(
                    install_id=status.install_id,
                    licensee="Test User",
                    private_key_pem=private_key,
                    now=start + timedelta(minutes=1),
                    plan="pro",
                    expires_at=start + timedelta(days=365),
                )
                unlocked = activate_managed_controls(token, now=start + timedelta(minutes=40))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertTrue(unlocked.unlocked)
        self.assertTrue(unlocked.can_use)
        self.assertEqual(unlocked.plan, "pro")
        self.assertEqual(unlocked.licensee, "Test User")

    def test_activate_rejects_key_for_other_install(self) -> None:
        tmp_dir = _workspace_test_dir("bad_key")
        private_key, public_key = _generate_keypair()
        try:
            with _license_test_env(tmp_dir, public_key):
                get_managed_controls_license_status("probe-managed")
                token = issue_managed_controls_license(
                    install_id="SMC-AAAAAAAAAAAAAAAA",
                    licensee="Test User",
                    private_key_pem=private_key,
                )
                with self.assertRaises(ManagedControlsLicenseError):
                    activate_managed_controls(token)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_purchase_url_includes_install_id(self) -> None:
        tmp_dir = _workspace_test_dir("purchase_url")
        try:
            with _license_test_env(tmp_dir):
                with patch.dict(os.environ, {"STS_MANAGED_CONTROLS_PURCHASE_URL": "https://example.com/buy"}, clear=False):
                    config = load_managed_controls_commerce_config(storage_dir=tmp_dir)
                    url = build_purchase_url(install_id="SMC-ABC123", config=config)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn("install_id=SMC-ABC123", url)

    def test_purchase_url_adds_lemonsqueezy_custom_install_id(self) -> None:
        tmp_dir = _workspace_test_dir("purchase_url_lemon")
        try:
            with _license_test_env(tmp_dir):
                with patch.dict(
                    os.environ,
                    {"STS_MANAGED_CONTROLS_PURCHASE_URL": "https://checkout.lemonsqueezy.com/buy/example"},
                    clear=False,
                ):
                    config = load_managed_controls_commerce_config(storage_dir=tmp_dir)
                    url = build_purchase_url(install_id="SMC-ABC123", config=config)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn("install_id=SMC-ABC123", url)
        self.assertIn("checkout%5Bcustom%5D%5Binstall_id%5D=SMC-ABC123", url)


def _generate_keypair() -> tuple[str, str]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def _license_test_env(root: Path, public_key_pem: str | None = None):
    env = {
        "STS_MANAGED_CONTROLS_HOME": str(root),
        "STS_MANAGED_CONTROLS_MACHINE_ID": "TEST-MACHINE-001",
        "STS_MANAGED_CONTROLS_DISABLE_REGISTRY": "1",
    }
    if public_key_pem is not None:
        env["STS_MANAGED_CONTROLS_PUBLIC_KEY_PEM"] = public_key_pem
    return patch.dict(os.environ, env, clear=False)


def _workspace_test_dir(name: str) -> Path:
    root = Path(__file__).resolve().parents[1] / "tmp" / "test_managed_controls_license" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


if __name__ == "__main__":
    unittest.main()
