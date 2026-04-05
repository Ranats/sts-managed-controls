from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from sts_bot.managed_controls_commerce import ManagedControlsCommerceConfig
from sts_bot.managed_controls_fulfillment_service import (
    extract_lemonsqueezy_fulfillment_event,
    send_license_email,
    verify_lemonsqueezy_signature,
)


class ManagedControlsFulfillmentServiceTest(unittest.TestCase):
    def test_verify_lemonsqueezy_signature_matches_hmac_sha256(self) -> None:
        body = b'{"ok":true}'
        secret = "secret123"
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        self.assertTrue(verify_lemonsqueezy_signature(body, signature=signature, secret=secret))
        self.assertFalse(verify_lemonsqueezy_signature(body, signature="bad", secret=secret))

    def test_extract_lemonsqueezy_fulfillment_event_reads_install_and_email(self) -> None:
        payload = {
            "meta": {
                "event_name": "order_created",
                "custom_data": {
                    "install_id": "SMC-ABC123",
                },
            },
            "data": {
                "id": "42",
                "attributes": {
                    "status": "paid",
                    "user_email": "buyer@example.com",
                    "user_name": "Buyer Name",
                    "first_order_item": {"product_name": "STS Managed Controls Pro"},
                },
            },
        }

        event = extract_lemonsqueezy_fulfillment_event(payload)

        assert event is not None
        self.assertEqual(event.external_id, "lemonsqueezy:42")
        self.assertEqual(event.install_id, "SMC-ABC123")
        self.assertEqual(event.user_email, "buyer@example.com")
        self.assertEqual(event.licensee, "Buyer Name")
        self.assertEqual(event.plan, "pro")

    def test_extract_lemonsqueezy_fulfillment_event_ignores_missing_install_id(self) -> None:
        payload = json.loads(
            """{
              "meta": {"event_name": "order_created", "custom_data": {}},
              "data": {"id": "42", "attributes": {"status": "paid", "user_email": "buyer@example.com"}}
            }"""
        )

        event = extract_lemonsqueezy_fulfillment_event(payload)

        self.assertIsNone(event)

    def test_send_license_email_sets_user_agent_header(self) -> None:
        commerce = ManagedControlsCommerceConfig(
            provider="lemonsqueezy",
            purchase_url="https://example.com/buy",
            activation_guide_url="https://example.com/guide",
            support_url="mailto:support@example.com",
            lemonsqueezy_webhook_secret="secret",
            resend_api_key="re_test",
            email_from="STS Managed Controls <licenses@example.com>",
            email_reply_to="support@example.com",
        )
        captured_headers: dict[str, str] = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"id":"email_123"}'

        def _fake_urlopen(req, timeout=0):
            del timeout
            captured_headers.update(dict(req.header_items()))
            return _FakeResponse()

        with mock.patch("sts_bot.managed_controls_fulfillment_service.request.urlopen", side_effect=_fake_urlopen):
            result = send_license_email(
                commerce=commerce,
                to_email="buyer@example.com",
                licensee="Buyer",
                install_id="SMC-ABC123",
                license_key="SMC2.token",
                plan="standard",
                activation_guide_url="https://example.com/guide",
            )

        self.assertEqual(result.email_id, "email_123")
        self.assertEqual(captured_headers.get("User-agent"), "sts2-managed-controls/1.0")


if __name__ == "__main__":
    unittest.main()
