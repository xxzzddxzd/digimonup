"""Farm / 肉田 APIs."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient


def farm_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/farm/list", {})


def farm_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/farm/info", {})


def harvest(client: "ApiClient", *, index: int) -> dict:
    return client.post_encrypted("/api/farm/harvest", {"_index": int(index)})


def seed(client: "ApiClient", *, index: int, seed_type: int) -> dict:
    return client.post_encrypted(
        "/api/farm/seed",
        {"_index": int(index), "_type": int(seed_type)},
    )


def watering(client: "ApiClient", *, index: int, water_type: int, count: int) -> dict:
    """POST /api/farm/watering {_index,_type,_count}. Type 203 reduces ~1800s each."""
    return client.post_encrypted(
        "/api/farm/watering",
        {
            "_index": int(index),
            "_type": int(water_type),
            "_count": int(count),
        },
    )


def seed_ad_view(client: "ApiClient") -> dict:
    """Watch ad to obtain farm seeds when seed stock is empty.

    Live capture 2026-07-21 (seed 用尽 UI → 看广告获取):
      POST /api/farm/ad-view  encrypted body {}  (no extra fields)

    IL: PS_FarmSeedADView.Request()  — empty RequestData
    Response (PS_FarmSeedADView.ResponseData):
      _farm: FarmLevelInfoParam  (includes _adCount / ad seed-ad quota)
      _rewardAllList: AllRewardInfoParam  (seed goods granted)

    UI: UIFarmSeedSelect → PS_FarmSeedADView.Request
    Not wired into auto / CLI yet — API only.

    Related (not implemented): PS_FarmFieldWateringADView (watering-can ad).
    """
    return client.post_encrypted("/api/farm/ad-view", {})


# Alias matching AFK naming style.
ad_view = seed_ad_view


def goods_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/goods/list", {})
