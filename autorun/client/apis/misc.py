"""Other APIs present in the capture (each callable)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..http_client import ApiClient


def rune_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/rune/list", {})


def preset_equip_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/preset/equip-list", {})


def soul_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/soul/list", {})


def soul_equip_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/soul/equip-list", {})


def all_mail_check(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/all-mail/check", {})


def notice_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/notice/list", {})


def dimensional_box_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/dimensional-box/info", {})


def camp_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/camp/info", {})


def camp_member_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/camp/member-list", {})


def camp_help_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/camp/help-list", {})


def gift_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/gift/list", {})


def post_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/post/list", {})


def purchase_reward_webstore(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/purchase/reward-webstore", {})


def boss_damage_info(client: "ApiClient", body: dict | None = None) -> dict:
    return client.post_encrypted("/api/boss-damage/info", body or {})


def boss_damage_reward_list(client: "ApiClient", body: dict | None = None) -> dict:
    return client.post_encrypted("/api/boss-damage/reward-list", body or {})


def item_spawn_and_sell(client: "ApiClient", body: dict[str, Any]) -> dict:
    return client.post_encrypted("/api/item/spawn-and-sell", body)
