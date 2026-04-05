from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from sts_bot.managed_controls_fulfillment_service import (
    extract_lemonsqueezy_fulfillment_event,
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


if __name__ == "__main__":
    unittest.main()
