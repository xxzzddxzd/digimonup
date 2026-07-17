"""Low-level HTTP transport matching UnityWebRequest headers."""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable, Optional

import requests

from .config import ClientConfig
from .crypto import aes_decrypt, aes_encrypt, wrap_encrypted
from .runtime_state import STATE


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class ApiClient:
    def __init__(self, config: ClientConfig, *, log: bool = True, logger: Callable[[str], None] | None = None):
        self.config = config
        self.session = requests.Session()
        self.session_key: Optional[str] = None
        self.hex_key: Optional[str] = None
        self.hex_iv: Optional[str] = None
        self.data_no: str = config.account.data_no
        self.base_url = config.base_url.rstrip("/")
        self.log_enabled = log
        self._logger = logger or print
        self.state = STATE
        self._lock = threading.RLock()

    def set_crypto(self, hex_key: str, hex_iv: str) -> None:
        self.hex_key = hex_key
        self.hex_iv = hex_iv

    def set_session_key(self, session_key: str) -> None:
        self.session_key = session_key

    def _log(self, msg: str) -> None:
        if self.log_enabled:
            self._logger(msg)

    def _headers(self) -> dict:
        auth = f"Bearer {self.session_key}" if self.session_key else "Bearer"
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": self.config.accept_language,
            "X-Unity-Version": self.config.unity_version,
            "User-Agent": self.config.user_agent,
            "Authorization": auth,
            "x-idempotency-key": str(uuid.uuid4()),
        }

    def _post(self, path: str, body: dict) -> tuple[dict, float, str, int]:
        with self._lock:
            return self._post_unlocked(path, body)

    def _post_unlocked(self, path: str, body: dict) -> tuple[dict, float, str, int]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        self._log(f"=> POST {url}")
        if getattr(self, "state", None) is not None:
            self.state.add_http_req(url)
        else:
            STATE.add_http_req(url)

        t0 = time.time()
        resp = self.session.post(
            url,
            headers=self._headers(),
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            timeout=self.config.timeout,
        )
        ms = (time.time() - t0) * 1000.0
        self._log(f"<= HTTP {resp.status_code} {url} ({ms:.0f}ms)")
        if getattr(self, "state", None) is not None:
            self.state.add_http_resp(url, resp.status_code, ms)
        else:
            STATE.add_http_resp(url, resp.status_code, ms)

        try:
            data = resp.json()
        except Exception as exc:
            raise ApiError(
                f"non-json response for {path}: HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:500],
            ) from exc

        if resp.status_code >= 400:
            raise ApiError(
                f"HTTP {resp.status_code} for {path}",
                status=resp.status_code,
                body=data,
            )
        return data, ms, url, resp.status_code

    def post_raw(self, path: str, body: dict) -> dict:
        data, _ms, _url, _status = self._post(path, body)
        return data

    def post_plain(self, path: str, body: dict) -> dict:
        """No AES (IsNoEncrypt APIs)."""
        payload = dict(body)
        payload.setdefault("_version", self.config.version)
        if "_dataNo" not in payload:
            payload["_dataNo"] = self.data_no
        data, _ms, _url, _status = self._post(path, payload)
        return data

    def post_encrypted(self, path: str, body: dict | None = None) -> dict:
        if not self.hex_key or not self.hex_iv:
            raise ApiError("AES key/iv not initialized")
        req = dict(body or {})
        req.setdefault("_version", self.config.version)
        req.setdefault("_dataNo", self.data_no)
        plain = json.dumps(req, separators=(",", ":"), ensure_ascii=False)
        cipher = aes_encrypt(self.hex_key, self.hex_iv, plain)
        outer = wrap_encrypted(self.data_no, cipher)
        data, _ms, _url, _status = self._post(path, outer)
        return self._decode_response(data)

    def _decode_response(self, body: dict) -> dict:
        if not isinstance(body, dict):
            return body
        data = body.get("_data")
        if data and self.hex_key and self.hex_iv:
            plain = aes_decrypt(self.hex_key, self.hex_iv, data)
            decoded = json.loads(plain)
            # Keep transport-level fields if present.
            if "_code" in body and "_code" not in decoded:
                decoded["_code"] = body["_code"]
            return decoded
        return body
