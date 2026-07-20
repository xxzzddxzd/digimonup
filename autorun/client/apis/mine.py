"""Mine / 探查数码世界 APIs.

  POST /api/mine/list              {}
  POST /api/mine/cell-move         {_col,_row,_moveType}  0=Cell 1=Dash
  POST /api/mine/cell-broken       {_col,_row,_brokenType} 0=Drill
  POST /api/mine/reward            {}  distance milestone claim
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient

MOVE_CELL = 0
MOVE_DASH = 1
BROKEN_DRILL = 0

GOODS_STAMINA = 150
GOODS_DRILL = 151
GOODS_DASH = 152  # Mine_Teleport / dash charge item


def mine_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/mine/list", {})


def cell_move(
    client: "ApiClient",
    *,
    col: int,
    row: int,
    move_type: int = MOVE_CELL,
) -> dict:
    return client.post_encrypted(
        "/api/mine/cell-move",
        {
            "_col": int(col),
            "_row": int(row),
            "_moveType": int(move_type),
        },
    )


def cell_broken(
    client: "ApiClient",
    *,
    col: int,
    row: int,
    broken_type: int = BROKEN_DRILL,
) -> dict:
    return client.post_encrypted(
        "/api/mine/cell-broken",
        {
            "_col": int(col),
            "_row": int(row),
            "_brokenType": int(broken_type),
        },
    )


def distance_reward(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/mine/reward", {})
