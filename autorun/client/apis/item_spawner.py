"""Item spawner / 装备炉 (全息装备生成器) APIs.

IL2CPP + GameData ItemSpawner:
  POST /api/item-spawner/info       {}
  POST /api/item-spawner/add-gold   {}
  POST /api/item-spawner/level-up   {}
  POST /api/item-spawner/complete   {}   # LevelupComplete
  POST /api/item-spawner/accel      {"_useCount": int}
  POST /api/item-spawner/accel-ad   {}
  POST /api/item/spawn-and-sell
      {"_count", "_filterGrade", "_filterMatchCount", "_filterStatTypeList"}
  POST /api/item/change-filter-option
      {"_filterGrade", "_filterMatchCount", "_filterStatTypeList"}

E_ITEM_SPAWNER_STATUS: Ready=0, In_Progress=1, Completed=2
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient

STATUS_READY = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETED = 2


def item_spawner_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/item-spawner/info", {})


def item_spawner_add_gold(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/item-spawner/add-gold", {})


def item_spawner_level_up(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/item-spawner/level-up", {})


def item_spawner_complete(client: "ApiClient") -> dict:
    """Finish timed level-up (PS_ItemSpawner_LevelupComplete)."""
    return client.post_encrypted("/api/item-spawner/complete", {})


def item_spawner_accel(client: "ApiClient", *, use_count: int = 1) -> dict:
    return client.post_encrypted(
        "/api/item-spawner/accel",
        {"_useCount": int(use_count)},
    )


def item_spawner_accel_ad(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/item-spawner/accel-ad", {})


def item_spawn_and_sell(
    client: "ApiClient",
    *,
    count: int = 8,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    filter_stat_type_list: Optional[Sequence[int]] = None,
) -> dict:
    body: dict[str, Any] = {
        "_count": int(count),
        "_filterGrade": int(filter_grade),
        "_filterMatchCount": int(filter_match_count),
        "_filterStatTypeList": list(filter_stat_type_list or []),
    }
    return client.post_encrypted("/api/item/spawn-and-sell", body)


def item_change_filter_option(
    client: "ApiClient",
    *,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    filter_stat_type_list: Optional[Sequence[int]] = None,
) -> dict:
    return client.post_encrypted(
        "/api/item/change-filter-option",
        {
            "_filterGrade": int(filter_grade),
            "_filterMatchCount": int(filter_match_count),
            "_filterStatTypeList": list(filter_stat_type_list or []),
        },
    )


def item_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/item/list", {})


def item_equip(
    client: "ApiClient",
    *,
    item_uid: str,
    is_equip: bool = True,
    is_guide: bool = False,
) -> dict:
    """POST /api/item/equip — PS_ItemEquip {_itemUid,_isEquip,_isGuide}."""
    return client.post_encrypted(
        "/api/item/equip",
        {
            "_itemUid": str(item_uid),
            "_isEquip": bool(is_equip),
            "_isGuide": bool(is_guide),
        },
    )


def item_sell(client: "ApiClient", *, item_uids: list[str] | str) -> dict:
    """POST /api/item/sell — PS_ItemSell {_itemUIDList}."""
    if isinstance(item_uids, str):
        uids = [item_uids]
    else:
        uids = [str(u) for u in item_uids if u]
    return client.post_encrypted("/api/item/sell", {"_itemUIDList": uids})

