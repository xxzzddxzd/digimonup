"""Battle APIs: start / kill-mob / end."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient


# Delay before each battle-related request.
REQUEST_DELAY_SEC = 3.0

# E_BATTLE_ATTRIBUTE
ATTR_INIT = 0
ATTR_PLAY = 1
ATTR_WORLD_MAP = 2
ATTR_IN_DUNGEON = 3
ATTR_OUT_DUNGEON = 4

# E_BATTLE_END_REASON
REASON_NONE = 0
REASON_CLEAR = 1
REASON_TIME_OVER = 2
REASON_ALL_DEAD = 3
REASON_FAILED = 4

# E_BATTLE_STATE
STATE_FORWARD = 0
STATE_FAILED_BOSS = 1

# E_REGION_TYPE
REGION_STAGE = 1
REGION_DUNGEON = 2


def _battle_delay() -> None:
    if REQUEST_DELAY_SEC > 0:
        time.sleep(REQUEST_DELAY_SEC)


def battle_start(
    client: "ApiClient",
    *,
    region: int,
    stage: int,
    sector: int = 0,
    repeat: int = 0,
    wave: int = 0,
    state: int = STATE_FORWARD,
    attr: int = ATTR_PLAY,
) -> dict:
    _battle_delay()
    return client.post_encrypted(
        "/api/battle/start",
        {
            "_region": region,
            "_stage": stage,
            "_sector": sector,
            "_repeat": repeat,
            "_wave": wave,
            "_state": state,
            "_attr": attr,
        },
    )


def battle_kill_mob(
    client: "ApiClient",
    *,
    wave: int,
    mob_uid_list: Sequence[str],
    reason: int = REASON_NONE,
) -> dict:
    _battle_delay()
    return client.post_encrypted(
        "/api/battle/kill-mob",
        {
            "_wave": wave,
            "_mobUIDList": list(mob_uid_list),
            "_reason": reason,
        },
    )


def battle_end(
    client: "ApiClient",
    *,
    region: int,
    reason: int = REASON_CLEAR,
    state: int = STATE_FORWARD,
    damage: str = "0",
) -> dict:
    _battle_delay()
    return client.post_encrypted(
        "/api/battle/end",
        {
            "_region": region,
            "_reason": reason,
            "_state": state,
            "_damage": damage,
        },
    )
