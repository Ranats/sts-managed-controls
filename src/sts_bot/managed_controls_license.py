from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


TRIAL_SECONDS = 30 * 60
STATE_FILE_NAME = "license_state.json"
ENV_HOME = "STS_MANAGED_CONTROLS_HOME"
ENV_DEV_UNLOCK = "STS_MANAGED_CONTROLS_DEV_UNLOCK"
_SOFT_UNLOCK_SALT = "sts-managed-controls-soft-unlock-v1"


class ManagedControlsLicenseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedControlsLicenseStatus:
    feature: str
    storage_dir: str
    state_path: str
    install_id: str
    trial_started_at: str
    trial_expires_at: str
    last_seen_at: str
    unlocked: bool
    activated_at: str
    trial_seconds: int
    remaining_seconds: int
    expired: bool
    can_use: bool
    message: str
    purchase_prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "storage_dir": self.storage_dir,
            "state_path": self.state_path,
            "install_id": self.install_id,
            "trial_started_at": self.trial_started_at,
            "trial_expires_at": self.trial_expires_at,
            "last_seen_at": self.last_seen_at,
            "unlocked": self.unlocked,
            "activated_at": self.activated_at,
            "trial_seconds": self.trial_seconds,
            "remaining_seconds": self.remaining_seconds,
            "expired": self.expired,
            "can_use": self.can_use,
            "message": self.message,
            "purchase_prompt": self.purchase_prompt,
        }


def get_managed_controls_license_status(
    feature: str,
    *,
    storage_dir: Path | None = None,
    now: datetime | None = None,
    create_trial: bool = True,
) -> ManagedControlsLicenseStatus:
    current_time = _utc_now() if now is None else _ensure_utc(now)
    root = _resolve_storage_dir(storage_dir)
    state_path = root / STATE_FILE_NAME
    state = _load_state(state_path)
    if state is None:
        if not create_trial:
            raise ManagedControlsLicenseError(f"license state not found: {state_path}")
        state = {
            "install_id": secrets.token_hex(8),
            "trial_started_at": _format_ts(current_time),
            "last_seen_at": _format_ts(current_time),
            "activated_at": "",
            "license_key": "",
            "unlocked": False,
        }
        _save_state(state_path, state)

    install_id = str(state.get("install_id", "")) or secrets.token_hex(8)
    trial_started_at = _parse_ts(str(state.get("trial_started_at", ""))) or current_time
    last_seen_at = _parse_ts(str(state.get("last_seen_at", ""))) or trial_started_at
    activated_at = str(state.get("activated_at", ""))
    unlocked = bool(state.get("unlocked", False)) or _dev_unlock_enabled()

    effective_now = current_time if current_time >= last_seen_at else last_seen_at
    expires_at = trial_started_at + timedelta(seconds=TRIAL_SECONDS)
    remaining_seconds = max(0, int((expires_at - effective_now).total_seconds()))
    expired = remaining_seconds <= 0
    can_use = unlocked or not expired
    purchase_prompt = (
        "Trial expired. Purchase an unlock key, then run "
        "`python -m sts_bot.cli activate-managed-controls --license-key <KEY>`."
    )
    if unlocked:
        message = "Unlimited mode unlocked."
    elif expired:
        message = purchase_prompt
    else:
        minutes = max(1, remaining_seconds // 60)
        message = f"Trial active: about {minutes} minute(s) remaining."

    updated_state = dict(state)
    updated_state["install_id"] = install_id
    updated_state["trial_started_at"] = _format_ts(trial_started_at)
    updated_state["last_seen_at"] = _format_ts(effective_now)
    updated_state["activated_at"] = activated_at
    updated_state["unlocked"] = bool(unlocked)
    if updated_state != state:
        _save_state(state_path, updated_state)

    return ManagedControlsLicenseStatus(
        feature=feature,
        storage_dir=str(root),
        state_path=str(state_path),
        install_id=install_id,
        trial_started_at=_format_ts(trial_started_at),
        trial_expires_at=_format_ts(expires_at),
        last_seen_at=_format_ts(effective_now),
        unlocked=bool(unlocked),
        activated_at=activated_at,
        trial_seconds=TRIAL_SECONDS,
        remaining_seconds=remaining_seconds,
        expired=expired,
        can_use=can_use,
        message=message,
        purchase_prompt=purchase_prompt,
    )


def ensure_managed_controls_access(
    feature: str,
    *,
    storage_dir: Path | None = None,
    now: datetime | None = None,
) -> ManagedControlsLicenseStatus:
    status = get_managed_controls_license_status(feature, storage_dir=storage_dir, now=now, create_trial=True)
    if status.can_use:
        return status
    raise ManagedControlsLicenseError(f"{status.message} install_id={status.install_id}")


def activate_managed_controls(
    license_key: str,
    *,
    storage_dir: Path | None = None,
    now: datetime | None = None,
) -> ManagedControlsLicenseStatus:
    current_time = _utc_now() if now is None else _ensure_utc(now)
    root = _resolve_storage_dir(storage_dir)
    state_path = root / STATE_FILE_NAME
    status = get_managed_controls_license_status("activate-managed-controls", storage_dir=root, now=current_time, create_trial=True)
    normalized = license_key.strip().upper()
    if normalized != _expected_license_key(status.install_id):
        raise ManagedControlsLicenseError(
            "invalid license key. request a valid unlock key for this install id: "
            f"{status.install_id}"
        )

    state = _load_state(state_path)
    if state is None:
        raise ManagedControlsLicenseError(f"license state not found: {state_path}")
    state["unlocked"] = True
    state["activated_at"] = _format_ts(current_time)
    state["license_key"] = normalized
    state["last_seen_at"] = _format_ts(current_time)
    _save_state(state_path, state)
    return get_managed_controls_license_status("activate-managed-controls", storage_dir=root, now=current_time, create_trial=True)


def _expected_license_key(install_id: str) -> str:
    digest = hashlib.sha256(f"{install_id}:{_SOFT_UNLOCK_SALT}".encode("utf-8")).hexdigest().upper()
    return f"SMC1-{install_id.upper()}-{digest[:12]}"


def _resolve_storage_dir(storage_dir: Path | None) -> Path:
    if storage_dir is not None:
        root = storage_dir
    else:
        env_root = os.environ.get(ENV_HOME, "").strip()
        if env_root:
            root = Path(env_root)
        else:
            appdata = os.environ.get("APPDATA", "").strip()
            if appdata:
                root = Path(appdata) / "STSManagedControls"
            else:
                root = Path.home() / ".sts_managed_controls"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _load_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManagedControlsLicenseError(f"invalid license state: {path}") from exc
    if not isinstance(payload, dict):
        raise ManagedControlsLicenseError(f"invalid license state: {path}")
    return payload


def _save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _dev_unlock_enabled() -> bool:
    value = os.environ.get(ENV_DEV_UNLOCK, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ManagedControlsLicenseError(f"invalid timestamp in license state: {value}") from exc


def _format_ts(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")
