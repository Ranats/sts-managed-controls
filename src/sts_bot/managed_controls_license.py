from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


TRIAL_SECONDS = 30 * 60
STATE_FILE_NAME = "license_state.json"
ENV_HOME = "STS_MANAGED_CONTROLS_HOME"
ENV_DEV_UNLOCK = "STS_MANAGED_CONTROLS_DEV_UNLOCK"
ENV_MACHINE_ID = "STS_MANAGED_CONTROLS_MACHINE_ID"
ENV_PUBLIC_KEY_PEM = "STS_MANAGED_CONTROLS_PUBLIC_KEY_PEM"
ENV_PURCHASE_URL = "STS_MANAGED_CONTROLS_PURCHASE_URL"
ENV_DISABLE_REGISTRY = "STS_MANAGED_CONTROLS_DISABLE_REGISTRY"
PRODUCT_CODE = "sts-managed-controls"
LICENSE_VERSION = "SMC2"
REGISTRY_SUBKEY = r"Software\STSManagedControls"
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAEl6SGR4f3LHxL8/QSjLPpGuTAHcZZMD/ra9DfgJkv00=
-----END PUBLIC KEY-----
"""


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
    licensee: str
    plan: str
    license_expires_at: str
    machine_id_source: str
    purchase_url: str

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
            "licensee": self.licensee,
            "plan": self.plan,
            "license_expires_at": self.license_expires_at,
            "machine_id_source": self.machine_id_source,
            "purchase_url": self.purchase_url,
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
    install_id, machine_id_source = _derive_install_id()
    state = _load_merged_state(state_path=state_path, install_id=install_id, now=current_time, create_trial=create_trial)

    trial_started_at = _parse_ts(str(state.get("trial_started_at", ""))) or current_time
    last_seen_at = _parse_ts(str(state.get("last_seen_at", ""))) or trial_started_at
    effective_now = current_time if current_time >= last_seen_at else last_seen_at

    activated_at = str(state.get("activated_at", ""))
    license_token = str(state.get("license_token", ""))
    invalid_token_message = ""
    if license_token:
        try:
            license_info = _verify_license_token(license_token, install_id=install_id, now=effective_now)
        except ManagedControlsLicenseError as exc:
            invalid_token_message = str(exc)
            license_token = ""
            license_info = None
    else:
        license_info = None
    unlocked = license_info is not None or _dev_unlock_enabled()

    expires_at = trial_started_at + timedelta(seconds=TRIAL_SECONDS)
    remaining_seconds = max(0, int((expires_at - effective_now).total_seconds()))
    expired = remaining_seconds <= 0
    can_use = unlocked or not expired
    purchase_prompt = (
        "Trial expired. Purchase an activation key for this install id, then run "
        "`python -m sts_bot.cli activate-managed-controls --license-key <KEY>`."
    )
    purchase_url = get_managed_controls_purchase_url(install_id)
    if unlocked and license_info is not None:
        plan = str(license_info.get("plan", "standard") or "standard")
        licensee = str(license_info.get("licensee", "") or "")
        license_expires_at = str(license_info.get("expires_at", "") or "")
        if license_expires_at:
            message = f"Activated for {licensee or 'licensed user'} on plan={plan} until {license_expires_at}."
        else:
            message = f"Activated for {licensee or 'licensed user'} on plan={plan}."
    elif unlocked:
        plan = "developer"
        licensee = ""
        license_expires_at = ""
        message = "Developer override enabled."
    elif expired:
        plan = ""
        licensee = ""
        license_expires_at = ""
        message = purchase_prompt
    else:
        plan = ""
        licensee = ""
        license_expires_at = ""
        minutes = max(1, remaining_seconds // 60)
        message = f"Trial active: about {minutes} minute(s) remaining."
    if invalid_token_message:
        message = f"Stored activation was invalid and has been cleared. {message}"

    updated_state = {
        "install_id": install_id,
        "trial_started_at": _format_ts(trial_started_at),
        "last_seen_at": _format_ts(effective_now),
        "activated_at": activated_at,
        "license_token": license_token,
        "licensee": licensee,
        "plan": plan,
        "license_expires_at": license_expires_at,
    }
    _save_state(state_path, updated_state)
    _save_registry_state(updated_state)

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
        licensee=licensee,
        plan=plan,
        license_expires_at=license_expires_at,
        machine_id_source=machine_id_source,
        purchase_url=purchase_url,
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
    token = license_key.strip()
    license_info = _verify_license_token(token, install_id=status.install_id, now=current_time)
    if license_info is None:
        raise ManagedControlsLicenseError(
            "invalid activation key. request a valid signed key for this install id: "
            f"{status.install_id}"
        )

    state = _load_merged_state(state_path=state_path, install_id=status.install_id, now=current_time, create_trial=True)
    state["activated_at"] = _format_ts(current_time)
    state["license_token"] = token
    state["licensee"] = str(license_info.get("licensee", "") or "")
    state["plan"] = str(license_info.get("plan", "standard") or "standard")
    state["license_expires_at"] = str(license_info.get("expires_at", "") or "")
    state["last_seen_at"] = _format_ts(current_time)
    _save_state(state_path, state)
    _save_registry_state(state)
    return get_managed_controls_license_status("activate-managed-controls", storage_dir=root, now=current_time, create_trial=True)


def issue_managed_controls_license(
    *,
    install_id: str,
    licensee: str,
    private_key_pem: str | bytes,
    now: datetime | None = None,
    plan: str = "standard",
    expires_at: datetime | None = None,
) -> str:
    current_time = _utc_now() if now is None else _ensure_utc(now)
    key = _load_private_key(private_key_pem)
    payload = {
        "version": LICENSE_VERSION,
        "product": PRODUCT_CODE,
        "install_id": install_id.strip().upper(),
        "licensee": licensee.strip(),
        "plan": plan.strip() or "standard",
        "issued_at": _format_ts(current_time),
        "not_before": _format_ts(current_time),
        "expires_at": _format_ts(expires_at) if expires_at is not None else "",
    }
    payload_bytes = _canonical_license_payload(payload)
    signature = key.sign(payload_bytes)
    return f"{LICENSE_VERSION}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def get_managed_controls_purchase_url(install_id: str) -> str:
    base_url = os.environ.get(ENV_PURCHASE_URL, "").strip()
    if not base_url:
        return ""
    parsed = urllib.parse.urlparse(base_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["install_id"] = install_id
    query.setdefault("product", PRODUCT_CODE)
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def _derive_install_id() -> tuple[str, str]:
    override = os.environ.get(ENV_MACHINE_ID, "").strip()
    if override:
        fingerprint = override
        source = "env"
    else:
        machine_guid = _read_machine_guid()
        if machine_guid:
            fingerprint = machine_guid
            source = "windows_machine_guid"
        else:
            fingerprint = "|".join(
                part
                for part in (
                    platform.node().strip(),
                    os.environ.get("PROCESSOR_IDENTIFIER", "").strip(),
                    os.environ.get("SYSTEMDRIVE", "").strip(),
                    str(Path.home()).strip(),
                )
                if part
            )
            source = "host_fallback"
    digest = hashlib.sha256(f"{PRODUCT_CODE}:{fingerprint}".encode("utf-8")).hexdigest().upper()
    return f"SMC-{digest[:16]}", source


def _load_merged_state(
    *,
    state_path: Path,
    install_id: str,
    now: datetime,
    create_trial: bool,
) -> dict[str, str]:
    file_state = _load_state(state_path) or {}
    registry_state = _load_registry_state()
    merged = _merge_states(file_state, registry_state, install_id=install_id, now=now)
    if merged:
        return merged
    if not create_trial:
        raise ManagedControlsLicenseError(f"license state not found: {state_path}")
    state = {
        "install_id": install_id,
        "trial_started_at": _format_ts(now),
        "last_seen_at": _format_ts(now),
        "activated_at": "",
        "license_token": "",
        "licensee": "",
        "plan": "",
        "license_expires_at": "",
    }
    _save_state(state_path, state)
    _save_registry_state(state)
    return state


def _merge_states(
    file_state: dict[str, Any],
    registry_state: dict[str, Any],
    *,
    install_id: str,
    now: datetime,
) -> dict[str, str]:
    first_seen_candidates = [
        _parse_ts(str(file_state.get("trial_started_at", ""))),
        _parse_ts(str(registry_state.get("trial_started_at", ""))),
    ]
    last_seen_candidates = [
        _parse_ts(str(file_state.get("last_seen_at", ""))),
        _parse_ts(str(registry_state.get("last_seen_at", ""))),
    ]
    trial_started_at = min((item for item in first_seen_candidates if item is not None), default=None)
    last_seen_at = max((item for item in last_seen_candidates if item is not None), default=None)
    if trial_started_at is None and last_seen_at is None and not file_state and not registry_state:
        return {}
    return {
        "install_id": install_id,
        "trial_started_at": _format_ts(trial_started_at or now),
        "last_seen_at": _format_ts(last_seen_at or trial_started_at or now),
        "activated_at": _select_text(file_state, registry_state, "activated_at"),
        "license_token": _select_text(file_state, registry_state, "license_token"),
        "licensee": _select_text(file_state, registry_state, "licensee"),
        "plan": _select_text(file_state, registry_state, "plan"),
        "license_expires_at": _select_text(file_state, registry_state, "license_expires_at"),
    }


def _select_text(file_state: dict[str, Any], registry_state: dict[str, Any], key: str) -> str:
    for state in (file_state, registry_state):
        value = str(state.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _verify_license_token(token: str, *, install_id: str, now: datetime) -> dict[str, str] | None:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != LICENSE_VERSION:
        raise ManagedControlsLicenseError("invalid activation key format")
    payload_bytes = _b64url_decode(parts[1])
    signature = _b64url_decode(parts[2])
    public_key = _load_public_key()
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise ManagedControlsLicenseError("activation key signature verification failed") from exc
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ManagedControlsLicenseError("activation key payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ManagedControlsLicenseError("activation key payload is invalid")
    if str(payload.get("product", "")) != PRODUCT_CODE:
        raise ManagedControlsLicenseError("activation key product mismatch")
    if str(payload.get("install_id", "")).upper() != install_id.upper():
        raise ManagedControlsLicenseError(f"activation key was issued for a different install id: {install_id}")
    not_before = _parse_ts(str(payload.get("not_before", "")))
    expires_at = _parse_ts(str(payload.get("expires_at", "")))
    if not_before is not None and now < not_before:
        raise ManagedControlsLicenseError("activation key is not valid yet")
    if expires_at is not None and now > expires_at:
        raise ManagedControlsLicenseError("activation key has expired")
    return {
        "licensee": str(payload.get("licensee", "") or ""),
        "plan": str(payload.get("plan", "standard") or "standard"),
        "expires_at": _format_ts(expires_at) if expires_at is not None else "",
    }


def _canonical_license_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _load_public_key() -> ed25519.Ed25519PublicKey:
    public_key_pem = os.environ.get(ENV_PUBLIC_KEY_PEM, "").strip() or PUBLIC_KEY_PEM
    key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ManagedControlsLicenseError("configured public key is not an Ed25519 key")
    return key


def _load_private_key(value: str | bytes) -> ed25519.Ed25519PrivateKey:
    payload = Path(value).read_bytes() if isinstance(value, str) and Path(value).exists() else value
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ManagedControlsLicenseError("configured private key is not an Ed25519 key")
    return key


def _read_machine_guid() -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as handle:
            value, _value_type = winreg.QueryValueEx(handle, "MachineGuid")
            return str(value).strip()
    except OSError:
        return ""


def _registry_enabled() -> bool:
    value = os.environ.get(ENV_DISABLE_REGISTRY, "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def _load_registry_state() -> dict[str, str]:
    if winreg is None or not _registry_enabled():
        return {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_SUBKEY) as handle:
            result: dict[str, str] = {}
            for name in (
                "install_id",
                "trial_started_at",
                "last_seen_at",
                "activated_at",
                "license_token",
                "licensee",
                "plan",
                "license_expires_at",
            ):
                try:
                    value, _value_type = winreg.QueryValueEx(handle, name)
                except FileNotFoundError:
                    continue
                result[name] = str(value)
            return result
    except OSError:
        return {}


def _save_registry_state(state: dict[str, str]) -> None:
    if winreg is None or not _registry_enabled():
        return
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_SUBKEY, 0, winreg.KEY_SET_VALUE) as handle:
            for key, value in state.items():
                winreg.SetValueEx(handle, key, 0, winreg.REG_SZ, str(value))
    except OSError:
        return


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


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ManagedControlsLicenseError("invalid activation key encoding") from exc
