"""Multi-mailbox Gmail account registry (shared by alerts + rejections).

Each feature keeps a JSON registry under the active profile and one OAuth
token file per connected Google account. Legacy single-token files
(``gmail_token.json`` / ``gmail_rejections_token.json``) are migrated
idempotently on first use.
"""
from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Callable

from . import config

_lock = threading.Lock()

# Feature keys
ALERTS = "alerts"
REJECTIONS = "rejections"

_FEATURE = {
    ALERTS: {
        "registry": "gmail_alerts_accounts.json",
        "token_dir": "gmail_tokens",
        "legacy_token": "gmail_token.json",
        "legacy_id": "legacy",
        "default_labels_fn": lambda: _split_labels(
            getattr(config, "GMAIL_LABEL", "") or "linkedin-jobs"),
        "status_prefix": "gmail_alerts",
    },
    REJECTIONS: {
        "registry": "gmail_rejections_accounts.json",
        "token_dir": "gmail_rejections_tokens",
        "legacy_token": "gmail_rejections_token.json",
        "legacy_id": "legacy",
        "default_labels_fn": lambda: _split_labels(
            getattr(config, "GMAIL_REJECTION_LABEL", "") or "job-rejection"),
        "status_prefix": "gmail_rejections",
    },
}


def _split_labels(raw: str) -> list[str]:
    names = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return names


def _meta(feature: str) -> dict:
    if feature not in _FEATURE:
        raise ValueError(f"Unknown Gmail feature: {feature}")
    return _FEATURE[feature]


def _registry_path(feature: str) -> Path:
    return Path(config.PROFILE_DIR) / _meta(feature)["registry"]


def _read_registry(feature: str) -> dict:
    path = _registry_path(feature)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"accounts": []}


def _write_registry(feature: str, data: dict) -> None:
    path = _registry_path(feature)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def token_path_for(feature: str, account: dict) -> Path:
    rel = (account.get("token_file") or "").strip()
    if not rel:
        raise FileNotFoundError("Account has no token_file")
    p = Path(rel)
    if p.is_absolute():
        return p
    return Path(config.PROFILE_DIR) / rel


def list_accounts(feature: str, *, enabled_only: bool = False) -> list[dict]:
    ensure_migrated(feature)
    # One-shot enrich for legacy rows still showing unknown@legacy.
    try:
        with _lock:
            needs = any(
                (a.get("email") or "").endswith("@legacy")
                for a in (_read_registry(feature).get("accounts") or [])
            )
        if needs:
            _enrich_legacy_email(feature)
    except Exception:
        pass
    with _lock:
        accounts = list(_read_registry(feature).get("accounts") or [])
    if enabled_only:
        accounts = [a for a in accounts if a.get("enabled", True)]
    return accounts


def get_account(feature: str, account_id: str) -> dict | None:
    for acct in list_accounts(feature):
        if acct.get("id") == account_id:
            return acct
    return None


def email_map(feature: str) -> dict[str, str]:
    """mailbox_id → email for UI chips."""
    return {
        a["id"]: (a.get("email") or a["id"])
        for a in list_accounts(feature)
        if a.get("id")
    }


def is_connected(feature: str) -> bool:
    for acct in list_accounts(feature, enabled_only=True):
        try:
            if token_path_for(feature, acct).exists():
                return True
        except (OSError, FileNotFoundError):
            continue
    return False


def status_key(feature: str, account_id: str) -> str:
    return f"{_meta(feature)['status_prefix']}:{account_id}"


def default_labels(feature: str) -> list[str]:
    labels = _meta(feature)["default_labels_fn"]()
    if feature == REJECTIONS:
        return labels[:1] or ["job-rejection"]
    return labels or ["linkedin-jobs"]


def ensure_migrated(feature: str) -> None:
    """Idempotent: promote legacy single-token file into the registry."""
    meta = _meta(feature)
    with _lock:
        data = _read_registry(feature)
        if data.get("accounts"):
            return
        legacy = Path(config.PROFILE_DIR) / meta["legacy_token"]
        if not legacy.exists():
            return
        account = {
            "id": meta["legacy_id"],
            "email": "unknown@legacy",
            "token_file": meta["legacy_token"],
            "labels": default_labels(feature),
            "enabled": True,
        }
        data["accounts"] = [account]
        _write_registry(feature, data)

    # Best-effort: resolve the real email for the legacy account.
    try:
        _enrich_legacy_email(feature)
    except Exception:
        pass


def _enrich_legacy_email(feature: str) -> None:
    acct = get_account(feature, _meta(feature)["legacy_id"])
    if not acct or not (acct.get("email") or "").endswith("@legacy"):
        return
    try:
        creds = load_credentials(feature, acct, on_inactive=None)
        email = fetch_profile_email(creds)
    except Exception:
        return
    if not email:
        return
    with _lock:
        data = _read_registry(feature)
        accounts = []
        for a in data.get("accounts") or []:
            if a.get("id") == acct["id"]:
                a = dict(a)
                a["email"] = email
            accounts.append(a)
        data["accounts"] = accounts
        _write_registry(feature, data)


def _new_id() -> str:
    return secrets.token_hex(4)


def add_account_from_creds(feature: str, creds_json: str, *,
                           email: str = "",
                           labels: list[str] | None = None,
                           account_id: str | None = None) -> dict:
    """Persist a new (or replaced) account token and registry entry."""
    ensure_migrated(feature)
    meta = _meta(feature)
    aid = account_id or _new_id()
    token_dir = Path(config.PROFILE_DIR) / meta["token_dir"]
    token_dir.mkdir(parents=True, exist_ok=True)
    rel = f"{meta['token_dir']}/{aid}.json"
    token_path = Path(config.PROFILE_DIR) / rel
    token_path.write_text(creds_json, encoding="utf-8")

    with _lock:
        data = _read_registry(feature)
        accounts = list(data.get("accounts") or [])
        existing = next((a for a in accounts if a.get("id") == aid), None)
        entry = {
            "id": aid,
            "email": (email or (existing or {}).get("email")
                      or "unknown@gmail.com").strip(),
            "token_file": rel,
            "labels": list(labels if labels is not None
                           else (existing or {}).get("labels")
                           or default_labels(feature)),
            "enabled": True,
        }
        if existing:
            accounts = [entry if a.get("id") == aid else a for a in accounts]
        else:
            # Drop any prior entry with the same email (reconnect-as-add).
            accounts = [
                a for a in accounts
                if (a.get("email") or "").lower() != entry["email"].lower()
            ]
            accounts.append(entry)
        data["accounts"] = accounts
        _write_registry(feature, data)
    return entry


def update_labels(feature: str, account_id: str, labels: list[str]) -> None:
    ensure_migrated(feature)
    cleaned = [p.strip() for p in labels if p and p.strip()]
    if not cleaned:
        cleaned = default_labels(feature)
    with _lock:
        data = _read_registry(feature)
        accounts = []
        found = False
        for a in data.get("accounts") or []:
            if a.get("id") == account_id:
                a = dict(a)
                a["labels"] = cleaned
                found = True
            accounts.append(a)
        if not found:
            raise KeyError(account_id)
        data["accounts"] = accounts
        _write_registry(feature, data)


def remove_account(feature: str, account_id: str) -> None:
    ensure_migrated(feature)
    with _lock:
        data = _read_registry(feature)
        kept = []
        removed = None
        for a in data.get("accounts") or []:
            if a.get("id") == account_id:
                removed = a
            else:
                kept.append(a)
        data["accounts"] = kept
        _write_registry(feature, data)
    if removed:
        try:
            token_path_for(feature, removed).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        from . import connection_status
        connection_status.clear(status_key(feature, account_id))
    except Exception:
        pass


def run_oauth(scopes: list[str]) -> object:
    """Browser OAuth; returns google Credentials."""
    secret = Path(str(config.GOOGLE_CLIENT_SECRET))
    if not secret.exists():
        raise FileNotFoundError(
            f"OAuth client file not found at {secret}. It's the same Desktop-app "
            "client JSON used for Google Sheets — set its path in Settings.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
    return flow.run_local_server(port=0, open_browser=True,
                                 authorization_prompt_message="")


def fetch_profile_email(creds) -> str:
    """Best-effort Gmail address for the signed-in account."""
    try:
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=creds)
        return (svc.users().getProfile(userId="me").execute()
                .get("emailAddress") or "").strip()
    except Exception:
        return ""


def load_credentials(feature: str, account: dict, *,
                     on_inactive: Callable[[str, str], None] | None = None):
    """Load/refresh OAuth credentials for one account.

    ``on_inactive(account_id, message)`` is called when the token is deleted.
    Raises ``FileNotFoundError`` / ``RuntimeError`` with user-readable text.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = token_path_for(feature, account)
    if not token_path.exists():
        raise FileNotFoundError(
            f"Gmail mailbox {account.get('email') or account.get('id')} "
            "isn't connected — reconnect in Settings.")
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text(encoding="utf-8")), scopes)
    aid = account.get("id") or ""
    email = account.get("email") or aid

    def _fail(msg: str):
        token_path.unlink(missing_ok=True)
        if on_inactive:
            on_inactive(aid, msg)
        raise RuntimeError(msg)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            msg = (
                f"Gmail login for {email} expired or was revoked. "
                "Open Settings → Google and reconnect that mailbox."
            )
            _fail(msg)
            raise  # pragma: no cover
    if not creds.valid:
        _fail(f"Gmail login for {email} expired — reconnect in Settings.")
    try:
        from . import connection_status
        connection_status.clear(status_key(feature, aid))
        # Also clear legacy aggregate key if present.
        connection_status.clear(_meta(feature)["status_prefix"])
    except Exception:
        pass
    return creds
