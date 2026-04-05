from __future__ import annotations

import json
import os
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


ENV_PURCHASE_URL = "STS_MANAGED_CONTROLS_PURCHASE_URL"
ENV_ACTIVATION_GUIDE_URL = "STS_MANAGED_CONTROLS_ACTIVATION_GUIDE_URL"
ENV_SUPPORT_URL = "STS_MANAGED_CONTROLS_SUPPORT_URL"
ENV_PROVIDER = "STS_MANAGED_CONTROLS_PROVIDER"
ENV_LEMONSQUEEZY_WEBHOOK_SECRET = "STS_MANAGED_CONTROLS_LEMONSQUEEZY_WEBHOOK_SECRET"
ENV_RESEND_API_KEY = "STS_MANAGED_CONTROLS_RESEND_API_KEY"
ENV_EMAIL_FROM = "STS_MANAGED_CONTROLS_EMAIL_FROM"
ENV_EMAIL_REPLY_TO = "STS_MANAGED_CONTROLS_EMAIL_REPLY_TO"
COMMERCE_FILE_NAME = "commerce.json"
DEFAULT_ACTIVATION_GUIDE_URL = "https://github.com/Ranats/sts-managed-controls#trial-and-unlock"
DEFAULT_PROVIDER = "lemonsqueezy"


@dataclass(frozen=True)
class ManagedControlsCommerceConfig:
    provider: str
    purchase_url: str
    activation_guide_url: str
    support_url: str
    lemonsqueezy_webhook_secret: str
    resend_api_key: str
    email_from: str
    email_reply_to: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "purchase_url": self.purchase_url,
            "activation_guide_url": self.activation_guide_url,
            "support_url": self.support_url,
            "lemonsqueezy_webhook_secret": self.lemonsqueezy_webhook_secret,
            "resend_api_key": self.resend_api_key,
            "email_from": self.email_from,
            "email_reply_to": self.email_reply_to,
        }


def load_managed_controls_commerce_config(*, storage_dir: Path | None = None) -> ManagedControlsCommerceConfig:
    storage = storage_dir.resolve() if storage_dir is not None else None
    file_payload: dict[str, str] = {}
    if storage is not None:
        commerce_path = storage / COMMERCE_FILE_NAME
        if commerce_path.exists():
            try:
                payload = json.loads(commerce_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                file_payload = {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}
    provider = os.environ.get(ENV_PROVIDER, "").strip() or file_payload.get("provider", "").strip() or DEFAULT_PROVIDER
    purchase_url = os.environ.get(ENV_PURCHASE_URL, "").strip() or file_payload.get("purchase_url", "").strip()
    activation_guide_url = (
        os.environ.get(ENV_ACTIVATION_GUIDE_URL, "").strip()
        or file_payload.get("activation_guide_url", "").strip()
        or DEFAULT_ACTIVATION_GUIDE_URL
    )
    support_url = os.environ.get(ENV_SUPPORT_URL, "").strip() or file_payload.get("support_url", "").strip()
    lemonsqueezy_webhook_secret = (
        os.environ.get(ENV_LEMONSQUEEZY_WEBHOOK_SECRET, "").strip()
        or file_payload.get("lemonsqueezy_webhook_secret", "").strip()
    )
    resend_api_key = os.environ.get(ENV_RESEND_API_KEY, "").strip() or file_payload.get("resend_api_key", "").strip()
    email_from = os.environ.get(ENV_EMAIL_FROM, "").strip() or file_payload.get("email_from", "").strip()
    email_reply_to = os.environ.get(ENV_EMAIL_REPLY_TO, "").strip() or file_payload.get("email_reply_to", "").strip()
    return ManagedControlsCommerceConfig(
        provider=provider,
        purchase_url=purchase_url,
        activation_guide_url=activation_guide_url,
        support_url=support_url,
        lemonsqueezy_webhook_secret=lemonsqueezy_webhook_secret,
        resend_api_key=resend_api_key,
        email_from=email_from,
        email_reply_to=email_reply_to,
    )


def build_purchase_url(*, install_id: str, config: ManagedControlsCommerceConfig) -> str:
    base_url = config.purchase_url.strip()
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("install_id", install_id)
    host = (parsed.netloc or "").lower()
    if "lemonsqueezy.com" in host:
        query.setdefault("checkout[custom][install_id]", install_id)
    rebuilt = parsed._replace(query=urlencode(query, doseq=True))
    return urlunparse(rebuilt)


def open_purchase_page(*, install_id: str, config: ManagedControlsCommerceConfig) -> str:
    url = build_purchase_url(install_id=install_id, config=config)
    if not url:
        return ""
    webbrowser.open(url)
    return url


def open_activation_guide(*, config: ManagedControlsCommerceConfig) -> str:
    url = config.activation_guide_url.strip()
    if not url:
        return ""
    webbrowser.open(url)
    return url
