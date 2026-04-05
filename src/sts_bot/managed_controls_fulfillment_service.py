from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request

from sts_bot.managed_controls_commerce import ManagedControlsCommerceConfig
from sts_bot.managed_controls_license import issue_managed_controls_license, ManagedControlsLicenseError


FULFILLMENTS_FILE_NAME = "commerce_fulfillments.json"


@dataclass(frozen=True)
class ManagedControlsFulfillmentServiceConfig:
    host: str
    port: int
    private_key_file: Path
    admin_token: str
    default_plan: str
    default_days: int
    storage_dir: Path
    commerce: ManagedControlsCommerceConfig


def run_fulfillment_service(config: ManagedControlsFulfillmentServiceConfig) -> None:
    private_key_pem = config.private_key_file.read_text(encoding="utf-8")
    store = FulfillmentStore(config.storage_dir / FULFILLMENTS_FILE_NAME)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route == "/health":
                self._write_json(HTTPStatus.OK, {"ok": True, "service": "managed-controls-fulfillment"})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route == "/issue":
                self._handle_manual_issue()
                return
            if route == "/webhooks/lemonsqueezy":
                self._handle_lemonsqueezy_webhook()
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def _handle_manual_issue(self) -> None:
            auth = self.headers.get("Authorization", "").strip()
            if auth != f"Bearer {config.admin_token}":
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            payload = self._read_json()
            if payload is None:
                return
            install_id = str(payload.get("install_id", "")).strip()
            licensee = str(payload.get("licensee", "")).strip()
            plan = str(payload.get("plan", config.default_plan)).strip() or config.default_plan
            no_expiry = bool(payload.get("no_expiry", False))
            try:
                days = int(payload.get("days", config.default_days))
            except (TypeError, ValueError):
                days = config.default_days
            if not install_id or not licensee:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "install_id_and_licensee_required"})
                return
            expires_at = None if no_expiry else datetime.now(timezone.utc) + timedelta(days=max(1, days))
            try:
                token = issue_managed_controls_license(
                    install_id=install_id,
                    licensee=licensee,
                    private_key_pem=private_key_pem,
                    plan=plan,
                    expires_at=expires_at,
                )
            except ManagedControlsLicenseError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "install_id": install_id,
                    "licensee": licensee,
                    "plan": plan,
                    "expires_at": "" if expires_at is None else expires_at.isoformat().replace("+00:00", "Z"),
                    "license_key": token,
                },
            )

        def _handle_lemonsqueezy_webhook(self) -> None:
            secret = config.commerce.lemonsqueezy_webhook_secret.strip()
            if not secret:
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "lemonsqueezy_webhook_secret_not_configured"})
                return
            raw = self._read_body()
            signature = self.headers.get("X-Signature", "").strip()
            if not verify_lemonsqueezy_signature(raw, signature=signature, secret=secret):
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid_signature"})
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return
            event = extract_lemonsqueezy_fulfillment_event(payload)
            if event is None:
                self._write_json(HTTPStatus.OK, {"ok": True, "ignored": True})
                return
            existing = store.get(event.external_id)
            if existing is not None:
                self._write_json(HTTPStatus.OK, {"ok": True, "duplicate": True, "license_key": existing.get("license_key", "")})
                return
            expires_at = None if event.no_expiry else datetime.now(timezone.utc) + timedelta(days=max(1, event.days))
            try:
                token = issue_managed_controls_license(
                    install_id=event.install_id,
                    licensee=event.licensee,
                    private_key_pem=private_key_pem,
                    plan=event.plan,
                    expires_at=expires_at,
                )
                email_result = send_license_email(
                    commerce=config.commerce,
                    to_email=event.user_email,
                    licensee=event.licensee,
                    install_id=event.install_id,
                    license_key=token,
                    plan=event.plan,
                    activation_guide_url=config.commerce.activation_guide_url,
                )
            except (ManagedControlsLicenseError, CommerceEmailError) as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            record = {
                "external_id": event.external_id,
                "provider": "lemonsqueezy",
                "user_email": event.user_email,
                "licensee": event.licensee,
                "install_id": event.install_id,
                "plan": event.plan,
                "days": event.days,
                "license_key": token,
                "email_id": email_result.email_id,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            store.put(event.external_id, record)
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "provider": "lemonsqueezy",
                    "external_id": event.external_id,
                    "install_id": event.install_id,
                    "user_email": event.user_email,
                    "license_key": token,
                    "email_id": email_result.email_id,
                },
            )

        def _read_body(self) -> bytes:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            return self.rfile.read(max(0, content_length))

        def _read_json(self) -> dict[str, Any] | None:
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return None
            if not isinstance(payload, dict):
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_payload"})
                return None
            return payload

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


class FulfillmentStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, external_id: str) -> dict[str, Any] | None:
        return self._load().get(external_id)

    def put(self, external_id: str, payload: dict[str, Any]) -> None:
        current = self._load()
        current[external_id] = payload
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(current, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class LemonSqueezyFulfillmentEvent:
    external_id: str
    install_id: str
    user_email: str
    licensee: str
    plan: str
    days: int
    no_expiry: bool


def verify_lemonsqueezy_signature(raw_body: bytes, *, signature: str, secret: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature.strip())


def extract_lemonsqueezy_fulfillment_event(payload: dict[str, Any]) -> LemonSqueezyFulfillmentEvent | None:
    meta = payload.get("meta")
    data = payload.get("data")
    if not isinstance(meta, dict) or not isinstance(data, dict):
        return None
    event_name = str(meta.get("event_name", "")).strip()
    if event_name not in {"order_created", "order_refunded"}:
        return None
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        return None
    if event_name == "order_refunded":
        return None
    status = str(attributes.get("status", "")).strip().lower()
    if status and status != "paid":
        return None
    custom_data = meta.get("custom_data")
    if not isinstance(custom_data, dict):
        custom_data = {}
    install_id = str(custom_data.get("install_id", "")).strip()
    if not install_id:
        return None
    user_email = str(attributes.get("user_email", "")).strip()
    user_name = str(attributes.get("user_name", "")).strip()
    if not user_email:
        return None
    first_item = attributes.get("first_order_item")
    item_name = ""
    if isinstance(first_item, dict):
        item_name = str(first_item.get("product_name", "")).strip()
    licensee = user_name or user_email
    external_id = f"lemonsqueezy:{data.get('id')}"
    return LemonSqueezyFulfillmentEvent(
        external_id=external_id,
        install_id=install_id,
        user_email=user_email,
        licensee=licensee,
        plan=derive_plan_from_product_name(item_name),
        days=365,
        no_expiry=False,
    )


def derive_plan_from_product_name(name: str) -> str:
    text = name.strip().lower()
    if "pro" in text:
        return "pro"
    return "standard"


@dataclass(frozen=True)
class CommerceEmailResult:
    email_id: str


class CommerceEmailError(RuntimeError):
    pass


def send_license_email(
    *,
    commerce: ManagedControlsCommerceConfig,
    to_email: str,
    licensee: str,
    install_id: str,
    license_key: str,
    plan: str,
    activation_guide_url: str,
) -> CommerceEmailResult:
    if not commerce.resend_api_key.strip():
        raise CommerceEmailError("resend_api_key is not configured")
    if not commerce.email_from.strip():
        raise CommerceEmailError("email_from is not configured")
    subject = f"Your STS Managed Controls activation key ({plan})"
    html = f"""
<p>Hi {licensee},</p>
<p>Thanks for your purchase of STS Managed Controls.</p>
<p><strong>Install ID:</strong> {install_id}<br/>
<strong>Activation Key:</strong><br/><code>{license_key}</code></p>
<p>Activation guide: <a href="{activation_guide_url}">{activation_guide_url}</a></p>
<p>Open the app, go to the License screen, paste the activation key, and click Activate.</p>
"""
    payload: dict[str, Any] = {
        "from": commerce.email_from,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if commerce.email_reply_to.strip():
        payload["reply_to"] = commerce.email_reply_to
    req = request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {commerce.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8")
    except Exception as exc:
        raise CommerceEmailError(f"failed to send activation email: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CommerceEmailError("email API returned invalid JSON") from exc
    email_id = str(payload.get("id", "")).strip()
    if not email_id:
        raise CommerceEmailError("email API did not return an id")
    return CommerceEmailResult(email_id=email_id)
