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
    """Watch ad to obtain farm seeds.

    Live capture + IL:
      POST /api/farm/ad-view  encrypted body {}
      PS_FarmSeedADView.Request() — empty RequestData
    Response:
      _farm: FarmLevelInfoParam (_adCount remaining seed-ad quota)
      _rewardAllList: seed goods granted

    Remaining count sources:
      - farm/info|harvest/list farm payload: `_farm._adCount`
      - daily reset/other payload: `_farmADCount`
    """
    return client.post_encrypted("/api/farm/ad-view", {})


def watering_ad_view(client: "ApiClient") -> dict:
    """Watch ad to obtain watering cans.

    Live capture 2026-07-27 + IL:
      POST /api/farm/watering-ad-view  encrypted body {}
      PS_FarmFieldWateringADView.Request() — empty RequestData
    Response:
      _farmWatering: {_viewCount, _adCount}
      _rewardAllList: watering-can goods (type 203)

    Remaining count sources:
      - farm/info / watering responses: `_farmWatering._adCount`
      - daily reset/other payload: `_farmWateringADCount`
    """
    return client.post_encrypted("/api/farm/watering-ad-view", {})


# Alias matching AFK naming style.
ad_view = seed_ad_view


def goods_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/goods/list", {})
