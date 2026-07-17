"""Import account/auth fields from capture or JSON; persist to local account.json."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .config import AccountConfig, ClientConfig

# Local account file next to main.py / package parent (autorun/account.json).
DEFAULT_ACCOUNT_PATH = Path(__file__).resolve().parent.parent / "account.json"

_AUTH_FIELD_MAP = {
    "_clientId": "client_id",
    "_deviceId": "device_id",
    "_platformUserId": "platform_user_id",
    "_deviceModel": "device_model",
    "_operatingSystem": "operating_system",
    "_adId": "ad_id",
    "_pushToken": "push_token",
    "_isGuest": "is_guest",
    "_country": "country",
    "_storeRegionCode": "store_region_code",
    "_regionType": "region_type",
    "_dataNo": "data_no",
}


def account_path() -> Path:
    return DEFAULT_ACCOUNT_PATH


def _header_map(entry: dict) -> dict[str, str]:
    req = entry.get("request") or {}
    header = req.get("header") or req.get("headers") or {}
    out: dict[str, str] = {}
    if isinstance(header, dict):
        items = header.get("headers")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("name"):
                    out[str(it["name"])] = str(it.get("value") or "")
        else:
            for k, v in header.items():
                if k in ("firstLine", "headers"):
                    continue
                out[str(k)] = str(v)
    elif isinstance(header, list):
        for it in header:
            if isinstance(it, dict) and it.get("name"):
                out[str(it["name"])] = str(it.get("value") or "")
    return out


def _request_json_body(entry: dict) -> Optional[dict]:
    req = entry.get("request") or {}
    body = req.get("body")
    text = None
    if isinstance(body, dict):
        text = body.get("text") or body.get("data")
    elif isinstance(body, str):
        text = body
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _response_json_body(entry: dict) -> Optional[dict]:
    resp = entry.get("response") or {}
    body = resp.get("body")
    text = None
    if isinstance(body, dict):
        text = body.get("text") or body.get("data")
    elif isinstance(body, str):
        text = body
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _entry_path(entry: dict) -> str:
    path = entry.get("path") or ""
    if path:
        return str(path)
    url = str(entry.get("url") or "")
    if "/api/" in url:
        return "/api/" + url.split("/api/", 1)[1].split("?", 1)[0]
    return url


def _account_from_auth_body(body: dict, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    acc: dict[str, Any] = {}
    for src, dst in _AUTH_FIELD_MAP.items():
        if src in body and body[src] is not None:
            acc[dst] = body[src]
    if body.get("_version"):
        acc["version"] = str(body["_version"])
    headers = headers or {}
    if headers.get("X-Unity-Version"):
        acc["unity_version"] = headers["X-Unity-Version"]
    if headers.get("Accept-Language"):
        acc["accept_language"] = headers["Accept-Language"]
    host = headers.get("Host")
    if host:
        acc["base_url"] = f"https://{host}"
    ua = headers.get("User-Agent") or ""
    m = re.search(r"Unity/([^;]+)", ua)
    if m and "unity_version" not in acc:
        acc["unity_version"] = m.group(1).strip()
    return acc


def _merge_auth_response(acc: dict[str, Any], resp: dict | None) -> None:
    if not isinstance(resp, dict):
        return
    if resp.get("_serverNum") is not None:
        acc["preferred_server_num"] = int(resp["_serverNum"])
    if resp.get("_publicUid"):
        acc["public_uid"] = str(resp["_publicUid"])
    if resp.get("_clientId") and not acc.get("client_id"):
        acc["client_id"] = str(resp["_clientId"])


def parse_account_input(path: str | Path) -> dict[str, Any]:
    """Parse Charles .chlsj / JSON capture / plain account JSON into account fields."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"input file not found: {p}")
    raw_text = p.read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"input is not valid JSON: {p}: {exc}") from exc

    # Already an account store / flat auth fields.
    if isinstance(raw, dict):
        if any(k in raw for k in ("client_id", "_clientId", "account")):
            if isinstance(raw.get("account"), dict):
                acc = dict(raw["account"])
                for k in ("base_url", "version", "unity_version", "accept_language"):
                    if k in raw and k not in acc:
                        acc[k] = raw[k]
                return acc
            if "_clientId" in raw or "_deviceId" in raw:
                return _account_from_auth_body(raw)
            return dict(raw)

    entries: list[dict] = []
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]
    elif isinstance(raw, dict):
        for key in ("entries", "log", "transactions", "requests"):
            if isinstance(raw.get(key), list):
                entries = [e for e in raw[key] if isinstance(e, dict)]
                break

    if not entries:
        raise ValueError(f"no parseable auth entries in {p}")

    auth_entry = None
    for e in entries:
        if _entry_path(e).rstrip("/").endswith("/account/auth"):
            auth_entry = e
            break
    if auth_entry is None:
        # Fallback: first body that looks like auth.
        for e in entries:
            body = _request_json_body(e)
            if body and "_clientId" in body and "_deviceId" in body:
                auth_entry = e
                break
    if auth_entry is None:
        raise ValueError(f"no /api/account/auth request found in {p}")

    body = _request_json_body(auth_entry)
    if not body:
        raise ValueError(f"auth request body missing/unparseable in {p}")
    acc = _account_from_auth_body(body, headers=_header_map(auth_entry))
    _merge_auth_response(acc, _response_json_body(auth_entry))

    # Optional: login request may be encrypted; still try plain.
    for e in entries:
        if _entry_path(e).rstrip("/").endswith("/account/login"):
            login_body = _request_json_body(e)
            if login_body and login_body.get("_serverNum") is not None:
                acc["preferred_server_num"] = int(login_body["_serverNum"])
            if login_body and login_body.get("_dataNo") and not acc.get("data_no"):
                acc["data_no"] = str(login_body["_dataNo"])
            break

    required = ("client_id", "device_id", "platform_user_id")
    missing = [k for k in required if not acc.get(k)]
    if missing:
        raise ValueError(f"auth extract missing fields: {missing}")
    acc["source_file"] = str(p)
    return acc


def account_dict_to_config_fields(acc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split into AccountConfig fields vs ClientConfig fields."""
    client_keys = {"base_url", "version", "unity_version", "accept_language", "timeout"}
    account_keys = {
        "client_id",
        "device_id",
        "platform_user_id",
        "device_model",
        "operating_system",
        "ad_id",
        "push_token",
        "is_guest",
        "country",
        "store_region_code",
        "region_type",
        "data_no",
        "preferred_server_num",
        "capture_stage",
        "capture_sector",
        "capture_region",
    }
    account_fields = {k: acc[k] for k in account_keys if k in acc}
    client_fields = {k: acc[k] for k in client_keys if k in acc}
    return account_fields, client_fields


def save_account_file(acc: dict[str, Any], path: Path | None = None) -> Path:
    path = path or account_path()
    account_fields, client_fields = account_dict_to_config_fields(acc)
    payload = {
        "account": account_fields,
        **client_fields,
        "meta": {
            "source_file": acc.get("source_file"),
            "public_uid": acc.get("public_uid"),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_account_file(path: Path | None = None) -> Optional[dict[str, Any]]:
    path = path or account_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid account file: {path}")
    return data


def apply_account_to_config(config: ClientConfig, data: dict[str, Any]) -> ClientConfig:
    """Mutate and return config with account.json / extracted fields."""
    account_fields, client_fields = account_dict_to_config_fields(
        data.get("account") if isinstance(data.get("account"), dict) else data
    )
    # client-level keys may sit top-level in account.json
    for k in ("base_url", "version", "unity_version", "accept_language", "timeout"):
        if k in data:
            client_fields[k] = data[k]
    for k, v in account_fields.items():
        if hasattr(config.account, k):
            setattr(config.account, k, v)
    for k, v in client_fields.items():
        if hasattr(config, k):
            setattr(config, k, v)
    # data_no also mirrored on ApiClient later via config.account
    return config


def import_input_file(input_path: str | Path, *, dest: Path | None = None) -> dict[str, Any]:
    acc = parse_account_input(input_path)
    saved = save_account_file(acc, dest)
    acc["saved_path"] = str(saved)
    return acc
