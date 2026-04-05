from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sts_bot.managed_controls_license import issue_managed_controls_license, ManagedControlsLicenseError


@dataclass(frozen=True)
class ManagedControlsIssuerConfig:
    host: str
    port: int
    private_key_file: Path
    admin_token: str
    default_plan: str
    default_days: int


def run_issuer_service(config: ManagedControlsIssuerConfig) -> None:
    private_key_pem = config.private_key_file.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._write_json(HTTPStatus.OK, {"ok": True, "service": "managed-controls-issuer"})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/issue":
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return
            auth = self.headers.get("Authorization", "").strip()
            if auth != f"Bearer {config.admin_token}":
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            raw = self.rfile.read(max(0, content_length))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_payload"})
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
