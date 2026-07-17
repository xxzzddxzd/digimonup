"""High-level session: version -> public-key -> auth -> login -> battle."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .apis import account, battle, misc
from .config import ClientConfig
from .crypto import build_encrypted_key, generate_hex_iv, generate_hex_key
from .http_client import ApiClient, ApiError


@dataclass
class GameSession:
    config: ClientConfig = field(default_factory=ClientConfig)
    client: ApiClient = field(init=False)
    public_key: Optional[str] = None
    encrypted_key: Optional[str] = None
    auth_info: dict = field(default_factory=dict)
    login_info: dict = field(default_factory=dict)
    account_info: dict = field(default_factory=dict)
    init_data: dict = field(default_factory=dict)
    battle_info: dict = field(default_factory=dict)
    last_battle_start: dict = field(default_factory=dict)
    last_battle_end: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = ApiClient(self.config)

    def bootstrap(self) -> dict:
        ver = account.app_version_check(self.client)
        # Keep endpoint from server if provided.
        endpoint = ver.get("_serverEndpoint")
        if endpoint:
            self.client.base_url = endpoint.rstrip("/")
        if ver.get("_dataVersion") is not None:
            # data_no is content hash; keep capture value unless empty.
            pass
        pk = account.get_public_key(self.client)
        self.public_key = pk.get("_publicKey")
        if not self.public_key:
            raise ApiError("public key missing", body=pk)

        hex_key = generate_hex_key()
        hex_iv = generate_hex_iv()
        self.client.set_crypto(hex_key, hex_iv)
        self.encrypted_key = build_encrypted_key(self.public_key, hex_key, hex_iv)

        self.auth_info = account.auth(self.client, self.encrypted_key)
        session_key = self.auth_info.get("_sessionKey")
        if not session_key:
            raise ApiError("auth failed: no sessionKey", body=self.auth_info)
        if self.auth_info.get("_code", 0) not in (0, None):
            raise ApiError(f"auth failed code={self.auth_info.get('_code')}", body=self.auth_info)
        self.client.set_session_key(session_key)
        return self.auth_info

    def load_account(self) -> dict:
        self.account_info = account.account_info(self.client)
        return self.account_info

    def list_servers(self) -> dict:
        return account.character_server_list(self.client)

    def login(self, server_num: Optional[int] = None) -> dict:
        acc = self.config.account
        sn = server_num if server_num is not None else (
            self.auth_info.get("_serverNum") or acc.preferred_server_num
        )
        self.login_info = account.login(
            self.client,
            region_type=acc.region_type,
            server_num=int(sn),
            operating_system=acc.operating_system,
            ad_id=acc.ad_id,
        )
        return self.login_info

    def init_game_data(self) -> dict:
        self.init_data = account.init_data(self.client)
        # Try extract battle snapshot if present.
        self.battle_info = self._extract_battle_info(self.init_data)
        return self.init_data

    def heartbeat(self) -> dict:
        return account.heartbeat(self.client)

    def battle_start(
        self,
        *,
        region: int,
        stage: int,
        sector: int = 0,
        repeat: int = 0,
        wave: int = 0,
        state: int = 0,
        attr: int = 1,
    ) -> dict:
        self.last_battle_start = battle.battle_start(
            self.client,
            region=region,
            stage=stage,
            sector=sector,
            repeat=repeat,
            wave=wave,
            state=state,
            attr=attr,
        )
        # Update cached battle if returned.
        b = self.last_battle_start.get("_battle")
        if isinstance(b, dict):
            self.battle_info = b
        return self.last_battle_start

    def battle_kill_mob(
        self,
        *,
        wave: int,
        mob_uid_list: list[str],
        reason: int = 0,
    ) -> dict:
        return battle.battle_kill_mob(
            self.client,
            wave=wave,
            mob_uid_list=mob_uid_list,
            reason=reason,
        )

    def battle_end(
        self,
        *,
        region: int,
        reason: int = 1,
        state: int = 0,
        damage: str = "0",
    ) -> dict:
        self.last_battle_end = battle.battle_end(
            self.client,
            region=region,
            reason=reason,
            state=state,
            damage=damage,
        )
        return self.last_battle_end

    def clear_session_crypto(self) -> None:
        """Drop bearer/session crypto so the next bootstrap does a fresh auth."""
        self.client.session_key = None
        self.client.hex_key = None
        self.client.hex_iv = None
        self.public_key = None
        self.encrypted_key = None
        self.auth_info = {}
        self.login_info = {}
        self.account_info = {}
        self.init_data = {}
        # keep battle_info only as a hint; reauth will overwrite from init-data

    def reauth_pipeline(self) -> dict:
        """Full recover from kick: public-key + auth + login + init-data.

        -19006 (duplicate login) invalidates the sessionKey from a prior auth.
        Only re-calling /account/login is not enough; must re-auth first.
        """
        self.clear_session_crypto()
        return self.run_login_pipeline()

    def run_login_pipeline(self) -> dict:
        self.bootstrap()
        self.load_account()
        servers = self.list_servers()
        self.login()
        self.init_game_data()
        return {
            "auth": self.auth_info,
            "servers": servers,
            "login": self.login_info,
            "battle_info": self.battle_info,
            "init_keys": sorted(self.init_data.keys()) if isinstance(self.init_data, dict) else [],
        }

    @staticmethod
    def _extract_battle_info(data: Any) -> dict:
        if not isinstance(data, dict):
            return {}
        if isinstance(data.get("_battle"), dict):
            return data["_battle"]
        init = data.get("_initData")
        if isinstance(init, dict):
            items = init.get("_list") or []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    payload = item.get("_data")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = None
                    if isinstance(payload, dict) and isinstance(payload.get("_battle"), dict):
                        return payload["_battle"]
        return {}

    def dump_state(self) -> str:
        return json.dumps(
            {
                "base_url": self.client.base_url,
                "session_key": self.client.session_key,
                "data_no": self.client.data_no,
                "auth": self.auth_info,
                "login": self.login_info,
                "battle_info": self.battle_info,
                "last_battle_start_code": (self.last_battle_start or {}).get("_code"),
                "last_battle_end_code": (self.last_battle_end or {}).get("_code"),
            },
            ensure_ascii=False,
            indent=2,
        )
