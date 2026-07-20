"""Dimensional Box (异次元 box) APIs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient


def info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/dimensional-box/info", {})


def search(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/dimensional-box/search", {})


def public_info(client: "ApiClient", owner_uid: str) -> dict:
    return client.post_encrypted(
        "/api/dimensional-box/public-info",
        {"_ownerUID": str(owner_uid)},
    )


def device_info(client: "ApiClient", *, target_uid: str, key: int) -> dict:
    return client.post_encrypted(
        "/api/dimensional-box/device-info",
        {"_targetUid": str(target_uid), "_key": int(key)},
    )


def device_disconnect(client: "ApiClient", keys: Sequence[int]) -> dict:
    """Claim rewards + unplace supporters. Body: {"_keys":[101,201]}"""
    return client.post_encrypted(
        "/api/dimensional-box/device-disconnect",
        {"_keys": [int(k) for k in keys]},
    )


def device_connect(
    client: "ApiClient",
    *,
    owner_uid: str,
    key: int,
    equip_index: int,
    is_pay_fee: bool = False,
    is_use_protect: bool = False,
) -> dict:
    """Place supporter on private box slot."""
    return client.post_encrypted(
        "/api/dimensional-box/device-connect",
        {
            "_ownerUID": str(owner_uid),
            "_key": int(key),
            "_equipIndex": int(equip_index),
            "_isPayFee": bool(is_pay_fee),
            "_isUseProtect": bool(is_use_protect),
        },
    )


def public_device_connect(
    client: "ApiClient",
    *,
    owner_uid: str,
    key: int,
    equip_index: int,
) -> dict:
    """Place supporter on public box slot. owner_uid is '0'..'4'."""
    return client.post_encrypted(
        "/api/dimensional-box/public-device-connect",
        {
            "_ownerUID": str(owner_uid),
            "_key": int(key),
            "_equipIndex": int(equip_index),
        },
    )


def battle(
    client: "ApiClient",
    *,
    target_uid: str,
    attack_req_uid: str,
    is_win: bool,
    battle_info: str,
    owner_uid: str,
    equip_index: int,
    is_public: bool,
    attacker_received_damage: str,
    damage: str,
) -> dict:
    return client.post_encrypted(
        "/api/dimensional-box/battle",
        {
            "_targetUID": str(target_uid),
            "_attackReqUID": str(attack_req_uid),
            "_isWin": bool(is_win),
            "_battleInfo": str(battle_info),
            "_ownerUID": str(owner_uid),
            "_equipIndex": int(equip_index),
            "_isPublic": bool(is_public),
            "_attackerReceivedDamage": str(attacker_received_damage),
            "_damage": str(damage),
        },
    )


def battle_history(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/dimensional-box/battle-history", {})
