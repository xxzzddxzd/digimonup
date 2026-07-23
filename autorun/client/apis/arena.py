"""Arena PVP APIs (regular + season).

Regular (PS_PVP*):
  POST /api/arena/info      {}
  POST /api/arena/matching  {}
  POST /api/arena/battle    {_stage,_isWin,_targetUID,_battleInfo}

Season (PS_PVP*_Season):
  POST /api/arena-season/info      {}
  POST /api/arena-season/matching  {_isRefresh: bool}
  POST /api/arena-season/battle    {_stage,_isWin,_targetUID,_battleInfo}

Tickets (E_GOODS_TYPE):
  356 PVPTicket
  357 PVPTicket_Season

Stage key for both battle APIs: 1
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient

# E_GOODS_TYPE
GOODS_PVP_TICKET = 356
GOODS_PVP_TICKET_SEASON = 357
GOODS_PVP_COIN = 400
GOODS_PVP_COIN_SEASON = 401

# stageKey used by PS_PVPBattle / PS_PVPBattle_Season
ARENA_STAGE_KEY = 1
ARENA_SEASON_STAGE_KEY = 1

# E_PVP_TYPE
PVP_TYPE_ARENA = 0
PVP_TYPE_SEASON = 1


def info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/arena/info", {})


def matching(client: "ApiClient") -> dict:
    """Challenge list (regular arena). Empty body."""
    return client.post_encrypted("/api/arena/matching", {})


def battle(
    client: "ApiClient",
    *,
    target_uid: str,
    is_win: bool = False,
    battle_info: str = "",
    stage: int = ARENA_STAGE_KEY,
) -> dict:
    return client.post_encrypted(
        "/api/arena/battle",
        {
            "_stage": int(stage),
            "_isWin": bool(is_win),
            "_targetUID": str(target_uid),
            "_battleInfo": str(battle_info or ""),
        },
    )


def season_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/arena-season/info", {})


def season_matching(client: "ApiClient", *, is_refresh: bool = False) -> dict:
    """Challenge list (season). Body requires _isRefresh."""
    return client.post_encrypted(
        "/api/arena-season/matching",
        {"_isRefresh": bool(is_refresh)},
    )


def season_battle(
    client: "ApiClient",
    *,
    target_uid: str,
    is_win: bool = False,
    battle_info: str = "",
    stage: int = ARENA_SEASON_STAGE_KEY,
) -> dict:
    return client.post_encrypted(
        "/api/arena-season/battle",
        {
            "_stage": int(stage),
            "_isWin": bool(is_win),
            "_targetUID": str(target_uid),
            "_battleInfo": str(battle_info or ""),
        },
    )


def user_list(client: "ApiClient", uids: Sequence[str]) -> dict:
    """PS_OtherUserProfileInfo — load opponent decks (optional before fight)."""
    return client.post_encrypted(
        "/api/user/list",
        {"_userUIDList": [str(u) for u in uids]},
    )


def ranking_list(
    client: "ApiClient",
    *,
    rank_type: int = 0,
    page: int = 1,
    count: int = 50,
    is_prev: bool = False,
    event_key: int = 0,
) -> dict:
    return client.post_encrypted(
        "/api/ranking/list",
        {
            "_isPrev": bool(is_prev),
            "_page": int(page),
            "_type": int(rank_type),
            "_count": int(count),
            "_eventKey": int(event_key),
        },
    )
