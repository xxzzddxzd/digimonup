"""Account / auth related APIs."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient


def app_version_check(client: "ApiClient") -> dict:
    # Capture shows empty dataNo for first call.
    return client.post_plain(
        "/api/app-version/check",
        {"_version": client.config.version, "_dataNo": ""},
    )


def get_public_key(client: "ApiClient") -> dict:
    return client.post_plain(
        "/api/account/public-key",
        {"_version": client.config.version, "_dataNo": client.data_no},
    )


def auth(client: "ApiClient", encrypted_key: str) -> dict:
    a = client.config.account
    body = {
        "_version": client.config.version,
        "_dataNo": client.data_no,
        "_encryptedKey": encrypted_key,
        "_regionType": a.region_type,
        "_clientId": a.client_id,
        "_isGuest": a.is_guest,
        "_country": a.country,
        "_deviceModel": a.device_model,
        "_deviceId": a.device_id,
        "_platformUserId": a.platform_user_id,
        "_pushToken": a.push_token,
        "_storeRegionCode": a.store_region_code,
        "_operatingSystem": a.operating_system,
        "_adId": a.ad_id,
    }
    return client.post_plain("/api/account/auth", body)


def account_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/account/info", {})


def character_server_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/character-server/list", {})


def login(
    client: "ApiClient",
    *,
    region_type: int,
    server_num: int,
    operating_system: str,
    ad_id: str,
) -> dict:
    return client.post_encrypted(
        "/api/account/login",
        {
            "_regionType": region_type,
            "_serverNum": server_num,
            "_operatingSystem": operating_system,
            "_adId": ad_id,
        },
    )


def init_data(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/account/init-data", {})


def heartbeat(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/account/heartbeat", {})


def reset(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/account/reset", {})
