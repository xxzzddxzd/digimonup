"""Guild maintenance for the hourly ``auto`` command.

1.2.0 live protocol:

- attendance: ``POST /api/camp/attendance {}``
- training/dojo reward state and claim: ``/api/boss-damage/* {_type}``
- training/dojo auto-complete: ``POST /api/dungeon/camp-sweep {_key}``

The sweep count comes from the live guild-dungeon ticket inventory.  Each
successful sweep consumes one ticket, so no daily count is hard-coded.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .apis import dungeon as dungeon_api
from .apis import farm as farm_api
from .apis import misc as misc_api
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006

GUILD_DUNGEONS: tuple[dict[str, Any], ...] = (
    {
        "name": "training",
        "label": "训练场",
        "dungeon_key": 15,
        "rank_type": 4,
        "ticket_goods": 107,
    },
    {
        "name": "dojo",
        "label": "道场",
        "dungeon_key": 16,
        "rank_type": 5,
        "ticket_goods": 108,
    },
)


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


def _code(body: Any) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    value = body.get("_code")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _raise_if_kick(body: Any, where: str) -> None:
    if _code(body) == SESSION_KICK:
        raise SessionKicked(where, body=body)


def _find_bool(payload: Any, key: str) -> bool | None:
    """Find a protocol flag whether it is top-level or nested in a param."""
    if isinstance(payload, dict):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool):
                return value
            if value in (0, 1, "0", "1"):
                return bool(int(value))
        for value in payload.values():
            found = _find_bool(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_bool(value, key)
            if found is not None:
                return found
    return None


def _list_items(container: Any) -> list[dict[str, Any]]:
    if isinstance(container, dict):
        container = container.get("_list", container.get("list"))
    if not isinstance(container, list):
        return []
    return [row for row in container if isinstance(row, dict)]


def _reward_items(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    reward_all = body.get("_rewardAllList") or body.get("rewardAllList") or {}
    if not isinstance(reward_all, dict):
        return []
    rows = _list_items(reward_all.get("_rewardList") or reward_all.get("rewardList"))
    return [
        {
            "type": row.get("_type", row.get("type")),
            "value": row.get("_value", row.get("value")),
            "count": row.get("_count", row.get("count")),
        }
        for row in rows
    ]


def _goods_map(body: Any) -> dict[int, int]:
    if not isinstance(body, dict):
        return {}
    goods = body.get("_goodsList") or body.get("goodsList") or {}
    rows = _list_items(goods)
    out: dict[int, int] = {}
    for row in rows:
        try:
            goods_type = int(row.get("_type", row.get("type")))
            value = int(row.get("_value", row.get("value", 0)) or 0)
        except (TypeError, ValueError):
            continue
        out[goods_type] = value
    return out


def _claim_attendance(session: GameSession, *, log: LogFn) -> dict[str, Any]:
    info = misc_api.camp_info(session.client)
    _raise_if_kick(info, "camp/info")
    info_code = _code(info)
    attended = _find_bool(info, "_isAttendance")
    result: dict[str, Any] = {
        "ok": info_code in (0, None),
        "info_code": info_code,
        "attended_before": attended,
        "claimed": False,
    }
    if info_code not in (0, None):
        result["error"] = "camp_info_failed"
        log(f"[-] guild attendance info failed code={info_code}")
        return result
    if attended is True:
        result["skipped_reason"] = "already_attended"
        log("[*] guild attendance skip: already attended")
        return result
    if attended is None:
        result["ok"] = False
        result["error"] = "attendance_state_missing"
        log("[-] guild attendance skip: _isAttendance missing from camp/info")
        return result

    body = misc_api.camp_attendance(session.client)
    _raise_if_kick(body, "camp/attendance")
    result["code"] = _code(body)
    result["message"] = body.get("_message") or body.get("_details")
    result["rewards"] = _reward_items(body)
    result["claimed"] = result["code"] in (0, None)
    result["ok"] = result["claimed"]
    if result["claimed"]:
        log(f"[+] guild attendance claimed rewards={result['rewards']}")
    else:
        log(f"[-] guild attendance failed code={result['code']}")
    return result


def _claim_dungeon_reward(
    session: GameSession,
    *,
    activity: dict[str, Any],
    log: LogFn,
) -> dict[str, Any]:
    rank_type = int(activity["rank_type"])
    label = str(activity["label"])
    request = {"_type": rank_type}

    info = misc_api.boss_damage_info(session.client, request)
    _raise_if_kick(info, f"boss-damage/info type={rank_type}")
    info_code = _code(info)
    claimed = _find_bool(info, "_isGetRewardBox")
    result: dict[str, Any] = {
        "ok": info_code in (0, None),
        "name": activity["name"],
        "label": label,
        "type": rank_type,
        "info_code": info_code,
        "claimed_before": claimed,
        "claimed": False,
    }
    if info_code not in (0, None):
        result["error"] = "reward_info_failed"
        log(f"[-] guild {label} reward info failed code={info_code}")
        return result
    if claimed is True:
        result["skipped_reason"] = "already_claimed"
        log(f"[*] guild {label} reward skip: already claimed")
        return result

    reward_list = misc_api.boss_damage_reward_list(session.client, request)
    _raise_if_kick(reward_list, f"boss-damage/reward-list type={rank_type}")
    result["reward_list_code"] = _code(reward_list)
    listed_claimed = _find_bool(reward_list, "_isGetRewardBox")
    if listed_claimed is not None:
        claimed = listed_claimed
        result["claimed_before"] = claimed
    if result["reward_list_code"] not in (0, None):
        result["ok"] = False
        result["error"] = "reward_list_failed"
        log(f"[-] guild {label} reward-list failed code={result['reward_list_code']}")
        return result
    if claimed is True:
        result["skipped_reason"] = "already_claimed"
        log(f"[*] guild {label} reward skip: already claimed")
        return result
    if claimed is None:
        result["ok"] = False
        result["error"] = "reward_state_missing"
        log(f"[-] guild {label} reward skip: _isGetRewardBox missing")
        return result

    body = misc_api.boss_damage_reward(session.client, request)
    _raise_if_kick(body, f"boss-damage/reward type={rank_type}")
    result["code"] = _code(body)
    result["message"] = body.get("_message") or body.get("_details")
    result["rewards"] = _reward_items(body)
    result["claimed"] = result["code"] in (0, None)
    result["ok"] = result["claimed"]
    if result["claimed"]:
        log(f"[+] guild {label} reward claimed rewards={result['rewards']}")
    else:
        log(f"[-] guild {label} reward failed code={result['code']}")
    return result


def _run_sweeps(
    session: GameSession,
    *,
    activity: dict[str, Any],
    remaining: int,
    log: LogFn,
) -> dict[str, Any]:
    label = str(activity["label"])
    key = int(activity["dungeon_key"])
    total = max(0, int(remaining))
    result: dict[str, Any] = {
        "ok": True,
        "name": activity["name"],
        "label": label,
        "key": key,
        "ticket_goods": int(activity["ticket_goods"]),
        "remaining_start": total,
        "attempted": 0,
        "completed": 0,
        "failed": 0,
        "runs": [],
    }
    if total <= 0:
        result["skipped_reason"] = "no_remaining_attempts"
        log(f"[*] guild {label} auto-complete skip: remaining=0")
        return result

    log(f"[*] guild {label} auto-complete remaining={total}")
    for index in range(1, total + 1):
        body = dungeon_api.dungeon_camp_sweep(session.client, key=key)
        _raise_if_kick(body, f"dungeon/camp-sweep key={key}")
        code = _code(body)
        ok = code in (0, None)
        row = {
            "index": index,
            "total": total,
            "ok": ok,
            "code": code,
            "message": body.get("_message") or body.get("_details"),
            "rewards": _reward_items(body),
            "boss_damage": body.get("_bossDamage"),
        }
        result["runs"].append(row)
        result["attempted"] += 1
        if ok:
            result["completed"] += 1
            log(f"[+] guild {label} auto-complete {index}/{total}")
            continue
        result["failed"] += 1
        result["ok"] = False
        log(f"[-] guild {label} auto-complete {index}/{total} failed code={code}")
        break
    return result


def run_guild_auto_care(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict[str, Any]:
    """Attendance, reward claims, then consume all live guild attempts."""
    result: dict[str, Any] = {
        "ok": True,
        "attendance": None,
        "rewards": {},
        "sweeps": {},
        "total_remaining": 0,
        "total_completed": 0,
        "total_failed": 0,
    }

    attendance = _claim_attendance(session, log=log)
    result["attendance"] = attendance
    if not attendance.get("ok"):
        result["ok"] = False

    for activity in GUILD_DUNGEONS:
        reward = _claim_dungeon_reward(session, activity=activity, log=log)
        result["rewards"][activity["name"]] = reward
        if not reward.get("ok"):
            result["ok"] = False

    goods_body = farm_api.goods_list(session.client)
    _raise_if_kick(goods_body, "goods/list[guild]")
    goods_code = _code(goods_body)
    result["goods_code"] = goods_code
    stocks = _goods_map(goods_body)
    if goods_code not in (0, None):
        result["ok"] = False
        result["error"] = "goods_list_failed"
        log(f"[-] guild auto-complete inventory failed code={goods_code}")
        return result

    for activity in GUILD_DUNGEONS:
        remaining = max(0, int(stocks.get(int(activity["ticket_goods"]), 0) or 0))
        sweep = _run_sweeps(
            session,
            activity=activity,
            remaining=remaining,
            log=log,
        )
        result["sweeps"][activity["name"]] = sweep
        result["total_remaining"] += remaining
        result["total_completed"] += int(sweep.get("completed") or 0)
        result["total_failed"] += int(sweep.get("failed") or 0)
        if not sweep.get("ok"):
            result["ok"] = False

    log(
        f"[*] guild-auto summary ok={result['ok']} "
        f"attendance={attendance.get('claimed')} "
        f"reward_claims={sum(bool(x.get('claimed')) for x in result['rewards'].values())} "
        f"sweeps={result['total_completed']}/{result['total_remaining']} "
        f"failed={result['total_failed']}"
    )
    return result
