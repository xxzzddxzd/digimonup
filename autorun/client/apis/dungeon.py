"""Dungeon APIs under /api/dungeon/* (PS_Dungeon* / PS_Battle*_Dungeon).

## Path map (from global-metadata + IL2CPP class names)

| Path | Client class | Request body |
| --- | --- | --- |
| POST /api/dungeon/list | PS_DungeonInfos | `{}` |
| POST /api/dungeon/start | PS_BattleStart_Dungeon | same as battle/start |
| POST /api/dungeon/end | PS_BattleEnd_Dungeon | same as battle/end |
| POST /api/dungeon/sweep | PS_DungeonSweep | `_key`, `_sector`, `_level` |
| POST /api/dungeon/camp-sweep | PS_GuildDungeonSweep | `_key` |
| POST /api/dungeon/ad-view | PS_DungeonADView | `_key` |
| POST /api/dungeon/trial-reset | PS_DungeonTrialReset | `_key` |
| POST /api/dungeon/matching | PS_DungeonDefenseMatching | `_key`, `_count` |

`PS_DungeonMatchingUserList` exists (`_userUIDList`) but its path is not in the
`/api/dungeon/*` string table; exposed as `matching_user_list` with the
defense-matching follow-up path only if you discover a live capture.

## Capture flow (Charles)

Typical dungeon run (not main-story stage):

1. `/api/dungeon/start`  — enter dungeon battle session
2. `/api/battle/kill-mob` — clear waves (shared battle API)
3. `/api/dungeon/end`    — settle dungeon battle

Main-story uses `/api/battle/start|end` instead. Dungeon start/end share the
same JSON field layout as battle start/end; only the URL differs
(`PS_BattleStart_Dungeon` / `PS_BattleEnd_Dungeon` override `get_API`).

## start / end field meanings (dungeon context)

`GameInfo.PlayDungeon(stageKey, sector, level)` calls
`PlayBattleDungeonInternal(region=Dungeon=2, stageKey, sector, level)` then
`SetBattleInfo(type, stageKey, sector, repeat=level)`.

So for a normal dungeon:

| JSON field | Meaning in dungeon | Typical value |
| --- | --- | --- |
| `_region` | `E_REGION_TYPE` | `2` Dungeon (`4` Soul, `5` Guild) |
| `_stage` | stageKey (from dungeon stage sheet) | dungeon stage id |
| `_sector` | sector index | often `1` (UI hardcodes 1 for common ready UI) |
| `_repeat` | **selected dungeon level** (not story repeat) | e.g. current `_level` or `_level+1` |
| `_wave` | wave index at start | `0` |
| `_state` | `E_BATTLE_STATE` | `0` FORWARD |
| `_attr` | `E_BATTLE_ATTRIBUTE` | `1` PLAY / `3` IN_DUNGEON |

`dungeon/end` body (`PS_BattleEnd.RequestData`):

| JSON field | Meaning |
| --- | --- |
| `_region` | same region used for start |
| `_reason` | `E_BATTLE_END_REASON` (1 CLEAR, 4 FAILED, …) |
| `_state` | `E_BATTLE_STATE` |
| `_damage` | total damage string |
| `_sendDamage` | optional string |
| `_receiveDamage` | optional string |
| `_speed` | optional float (client `Time.timeScale`) |

## Progress snapshot (init-data type 8 = DungeonList)

```json
{
  "_playList": {"_list": [11, 6, 2, ...]},
  "_dungeonList": {"_list": [
    {"_key": 1, "_level": 33, "_challengeLevel": 0, "_isGetReward": true, "_adCount": 2}
  ]}
}
```

- `_key`: dungeon table key (`DungeonInfoParam` / `DataInfoDungeon.key`)
- `_level`: progress / selectable level
- sweep / ad-view / trial-reset / matching use this `_key` as `_key`

Note: start's `_stage` is the **stageKey** from the stage linked to that dungeon
row, which may equal or differ from progress `_key` depending on table data.
Prefer stageKey from client tables or from a prior successful start response
`_battle._stage` when available.

## Related (not under /api/dungeon)

- Waves still use `/api/battle/kill-mob`
- Boss damage ranking uses `/api/boss-damage/*` (`PS_DungeonBossDamage` hits that family)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from . import battle as battle_api

if TYPE_CHECKING:
    from ..http_client import ApiClient


# E_REGION_TYPE (dungeon family)
REGION_NONE = 0
REGION_STAGE = 1
REGION_DUNGEON = 2
REGION_PVP = 3
REGION_SOUL_DUNGEON = 4
REGION_GUILD_DUNGEON = 5

# Lost Tower (失落之塔) keeps the soul dungeon Stage.Index=1 and cycles only
# its sector every 10 floors. repeat stays the full display floor.
LOST_TOWER_DUNGEON_KEY = 9
LOST_TOWER_REGION = 100000
LOST_TOWER_STAGE = 1
LOST_TOWER_SECTORS_PER_STAGE = 10


def lost_tower_battle_position(level: int) -> tuple[int, int]:
    """Map display floor to dungeon/start (stage, sector)."""
    floor = int(level)
    if floor < 1:
        raise ValueError(f"lost tower level must be >= 1, got {floor}")
    sector = ((floor - 1) % LOST_TOWER_SECTORS_PER_STAGE) + 1
    return LOST_TOWER_STAGE, sector

# Re-export common battle enums for dungeon callers.
ATTR_INIT = battle_api.ATTR_INIT
ATTR_PLAY = battle_api.ATTR_PLAY
ATTR_WORLD_MAP = battle_api.ATTR_WORLD_MAP
ATTR_IN_DUNGEON = battle_api.ATTR_IN_DUNGEON
ATTR_OUT_DUNGEON = battle_api.ATTR_OUT_DUNGEON

REASON_NONE = battle_api.REASON_NONE
REASON_CLEAR = battle_api.REASON_CLEAR
REASON_TIME_OVER = battle_api.REASON_TIME_OVER
REASON_ALL_DEAD = battle_api.REASON_ALL_DEAD
REASON_FAILED = battle_api.REASON_FAILED

STATE_FORWARD = battle_api.STATE_FORWARD
STATE_FAILED_BOSS = battle_api.STATE_FAILED_BOSS


# ---------------------------------------------------------------------------
# list / progress
# ---------------------------------------------------------------------------


def dungeon_list(client: "ApiClient") -> dict:
    """POST /api/dungeon/list  (PS_DungeonInfos) — empty encrypted body.

    Response (typical):
      _dungeonList._list[]: {_key,_level,_challengeLevel,_isGetReward,_adCount}
      _playList._list[]: int dungeon keys recently played
    Also present in login init-data as type=8 (DungeonList).
    """
    return client.post_encrypted("/api/dungeon/list", {})


# ---------------------------------------------------------------------------
# start / end  (same bodies as battle/*, different URL)
# ---------------------------------------------------------------------------


def dungeon_start(
    client: "ApiClient",
    *,
    stage: int,
    sector: int = 1,
    level: int = 0,
    region: int = REGION_DUNGEON,
    wave: int = 0,
    state: int = STATE_FORWARD,
    attr: int = ATTR_PLAY,
) -> dict:
    """POST /api/dungeon/start  (PS_BattleStart_Dungeon).

    Args map to BattleInfoParam / battle start fields:
      stage  -> _stage   (stageKey)
      sector -> _sector  (often 1)
      level  -> _repeat  (dungeon difficulty level; NOT main-story repeat)
      region -> _region  (2 Dungeon / 4 Soul / 5 Guild)
    """
    battle_api._battle_delay()
    return client.post_encrypted(
        "/api/dungeon/start",
        {
            "_region": int(region),
            "_stage": int(stage),
            "_sector": int(sector),
            "_repeat": int(level),
            "_wave": int(wave),
            "_state": int(state),
            "_attr": int(attr),
        },
    )


def dungeon_end(
    client: "ApiClient",
    *,
    region: int = REGION_DUNGEON,
    reason: int = REASON_CLEAR,
    state: int = STATE_FORWARD,
    damage: str | int = "0",
    send_damage: str | int | None = None,
    receive_damage: str | int | None = None,
    speed: float | None = None,
) -> dict:
    """POST /api/dungeon/end  (PS_BattleEnd_Dungeon).

    Minimal body matches live stage battle_end usage. Optional damage split and
    speed mirror full PS_BattleEnd.RequestData when needed.
    """
    battle_api._battle_delay()
    body: dict = {
        "_region": int(region),
        "_reason": int(reason),
        "_state": int(state),
        "_damage": str(damage),
    }
    if send_damage is not None:
        body["_sendDamage"] = str(send_damage)
    if receive_damage is not None:
        body["_receiveDamage"] = str(receive_damage)
    if speed is not None:
        body["_speed"] = float(speed)
    return client.post_encrypted("/api/dungeon/end", body)


# ---------------------------------------------------------------------------
# sweep / ad / trial / matching
# ---------------------------------------------------------------------------


def dungeon_sweep(
    client: "ApiClient",
    *,
    key: int,
    sector: int = 1,
    level: int,
) -> dict:
    """POST /api/dungeon/sweep  (PS_DungeonSweep).

    Request: {_key: dungeonKey, _sector: sectorIndex, _level: level}
    Response typically includes _dungeon, _questList, _rewardAllList.
    """
    return client.post_encrypted(
        "/api/dungeon/sweep",
        {
            "_key": int(key),
            "_sector": int(sector),
            "_level": int(level),
        },
    )


def dungeon_camp_sweep(
    client: "ApiClient",
    *,
    key: int,
) -> dict:
    """POST /api/dungeon/camp-sweep (PS_GuildDungeonSweep).

    1.2.0 live capture and RequestData both show that this endpoint accepts only
    the guild dungeon key. Adding `_sector`/`_level` makes the server reject it.
    """
    return client.post_encrypted(
        "/api/dungeon/camp-sweep",
        {"_key": int(key)},
    )


def dungeon_ad_view(client: "ApiClient", *, key: int) -> dict:
    """POST /api/dungeon/ad-view  (PS_DungeonADView).

    Live capture 2026-07-27 14:49:46 (iOS plugin plaintext):
      REQ  {"_key": 1}
      RESP {
        "_code": 0,
        "_dungeon": {"_key":1,"_level":59,"_challengeLevel":0,"_isGetReward":true,"_adCount":1},
        "_rewardAllList": {
          "_rewardList": {"_list":[{"_type":1,"_value":100,"_count":1,...}]},
          "_goodsList":  {"_list":[{"_type":100,"_value":"1","_accUseValue":"59",...}]}
        }
      }

    Notes:
      - `_key` is dungeon progress key (same as list/start mapping table), not stageKey.
      - Server decrements remaining `_adCount` and grants the dungeon ticket goods.
      - No ad-network token is present on the API body; client UI only gates the action.
    """
    return client.post_encrypted("/api/dungeon/ad-view", {"_key": int(key)})


def dungeon_trial_reset(client: "ApiClient", *, key: int) -> dict:
    """POST /api/dungeon/trial-reset  (PS_DungeonTrialReset) {_key}."""
    return client.post_encrypted("/api/dungeon/trial-reset", {"_key": int(key)})


def dungeon_matching(
    client: "ApiClient",
    *,
    key: int,
    count: int,
) -> dict:
    """POST /api/dungeon/matching  (PS_DungeonDefenseMatching).

    Request: {_key: dungeonKey, _count: count}
    Response: {_key, _userUIDList: [str, ...]}
    """
    return client.post_encrypted(
        "/api/dungeon/matching",
        {
            "_key": int(key),
            "_count": int(count),
        },
    )


def dungeon_matching_user_list(
    client: "ApiClient",
    *,
    user_uid_list: Sequence[str],
    path: str = "/api/dungeon/matching",
) -> dict:
    """PS_DungeonMatchingUserList: {_userUIDList: [...]}.

    Path is not confirmed in the `/api/dungeon/*` string table (defense matching
    already owns `/api/dungeon/matching`). Pass an explicit `path` if a capture
    shows a distinct endpoint; default keeps the call site usable for probes.
    """
    return client.post_encrypted(
        path,
        {"_userUIDList": [str(u) for u in user_uid_list]},
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def extract_dungeon_list(init_or_list_response: dict) -> list[dict]:
    """Pull dungeon progress rows from init-data type=8 or /api/dungeon/list body."""
    if not isinstance(init_or_list_response, dict):
        return []

    # Direct list response
    dlist = init_or_list_response.get("_dungeonList")
    if isinstance(dlist, dict):
        rows = dlist.get("_list") or []
        return [r for r in rows if isinstance(r, dict)]
    if isinstance(dlist, list):
        return [r for r in dlist if isinstance(r, dict)]

    # init-data envelope: {_initData: {_list: [{_type:8,_data:{...}}, ...]}}
    init = init_or_list_response.get("_initData") or init_or_list_response
    items = None
    if isinstance(init, dict):
        items = init.get("_list")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            if int(it.get("_type") or -1) != 8:
                continue
            data = it.get("_data") or {}
            return extract_dungeon_list(data if isinstance(data, dict) else {})
    return []


def extract_dungeon_play_list(init_or_list_response: dict) -> list[int]:
    """Pull today's enabled dungeon keys from init-data or dungeon/list."""
    if not isinstance(init_or_list_response, dict):
        return []

    play_list = init_or_list_response.get("_playList")
    if isinstance(play_list, dict):
        play_list = play_list.get("_list") or []
    if isinstance(play_list, list):
        result: list[int] = []
        for value in play_list:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        return result

    # init-data envelope: {_initData: {_list: [{_type:8,_data:{...}}, ...]}}
    init = init_or_list_response.get("_initData") or init_or_list_response
    items = init.get("_list") if isinstance(init, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                item_type = int(item.get("_type") or -1)
            except (TypeError, ValueError):
                continue
            if item_type != 8:
                continue
            data = item.get("_data") or {}
            return extract_dungeon_play_list(data if isinstance(data, dict) else {})
    return []


def find_dungeon_progress(rows: Sequence[dict], key: int) -> dict | None:
    """Return the progress row for dungeon `_key`, or None."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("_key") or -1) == int(key):
                return row
        except Exception:
            continue
    return None
