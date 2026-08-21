"""Automatically advance the recurring Guide quest chain (keys 9000-9014).

The chain is deliberately allow-listed.  It may open equipment, kill ordinary
story mobs without clearing the boss, spawn skills/members, and clear the two
quest dungeons.  SectorClear is claimed only when already complete; this module
never advances the player's story frontier.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .apis import battle as battle_api
from .apis import quest as quest_api
from .apis import shop as shop_api
from .dungeon_care import (
    SessionKicked as DungeonSessionKicked,
    fetch_goods_map,
    run_dungeon_clear,
    ticket_stock_for_key,
)
from .farm import _extract_spawn_waves
from .item_spawner_care import (
    SessionKicked as ItemSpawnerSessionKicked,
    fetch_item_ticket_stock,
    run_spawn_batches,
)
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
SERIES_KEYS = frozenset(range(9000, 9015))
DEFAULT_MAX_STEPS = 64

# Recurring Guide chain copied from GameData.Quest.  Keep this small allow-list
# in source so auto does not depend on a local decrypted GameData dump.
_SERIES_ROWS = (
    (9000, "GT_Quest_Spawn_Item", "Spawn", "Item", 50, 0, 9001),
    (9001, "GT_Quest_KillMobCount", "KillMobCount", None, 50, 0, 9002),
    (9002, "GT_Quest_Spawn_Skill", "Spawn", "Skill", 30, 0, 9003),
    (9003, "GT_Quest_Spawn_Member", "Spawn", "Member", 30, 0, 9004),
    (9004, "GT_Quest_Spawn_Item", "Spawn", "Item", 50, 0, 9005),
    (9005, "GT_Quest_KillMobCount", "KillMobCount", None, 100, 0, 9006),
    (9006, "GT_Quest_DungeonClear_Lamp", "DungeonClear", "Dungeon_Lamp", 2, 0, 9007),
    (9007, "GT_Quest_DungeonClear_Lava", "DungeonClear", "Dungeon_Lava", 2, 0, 9008),
    (9008, "GT_Quest_Spawn_Item", "Spawn", "Item", 50, 0, 9009),
    (9009, "GT_Quest_KillMobCount", "KillMobCount", None, 50, 0, 9010),
    (9010, "GT_Quest_Spawn_Skill", "Spawn", "Skill", 30, 0, 9011),
    (9011, "GT_Quest_Spawn_Member", "Spawn", "Member", 30, 0, 9012),
    (9012, "GT_Quest_Spawn_Item", "Spawn", "Item", 50, 0, 9013),
    (9013, "GT_Quest_KillMobCount", "KillMobCount", None, 100, 0, 9014),
    (9014, "GT_Quest_SectorClear", "SectorClear", None, 210, 5, 9000),
)
SERIES_DEFINITIONS: dict[int, dict[str, Any]] = {
    key: {
        "Name": name,
        "Type": quest_type,
        "SubType1": subtype,
        "DestValue": destination,
        "IncreaseValue": increase,
        "Next": next_key,
    }
    for key, name, quest_type, subtype, destination, increase, next_key in _SERIES_ROWS
}

# E_GOODS_TYPE values and /api/shop/spawn entries verified from GameData.Shop.
SPAWN_SHOP: dict[str, dict[str, int]] = {
    "Skill": {"shop_key": 12, "goods_type": 51, "cost": 30, "count": 35},
    "Member": {"shop_key": 112, "goods_type": 52, "cost": 30, "count": 35},
}

DUNGEON_BY_SUBTYPE = {
    "Dungeon_Lamp": 1,
    "Dungeon_Lava": 2,
}


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _rows(payload: Any, key: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key) or payload.get(key.removeprefix("_")) or {}
    if isinstance(value, dict):
        value = value.get("_list") or value.get("list") or []
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def load_series_definitions() -> dict[int, dict[str, Any]]:
    """Return an isolated copy of the recurring Guide task allow-list."""
    return {key: dict(row) for key, row in SERIES_DEFINITIONS.items()}


def quest_target(definition: dict[str, Any], level: int) -> int:
    """Return the cycle-adjusted target used by recurring Guide quests."""
    base = max(0, _int(definition.get("DestValue")))
    increase = max(0, _int(definition.get("IncreaseValue")))
    return base + increase * max(0, int(level))


def _task_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    key = _int(row.get("_key", row.get("key")), -1)
    definition = load_series_definitions().get(key)
    if definition is None:
        return None
    level = max(0, _int(row.get("_level", row.get("level"))))
    value = max(0, _int(row.get("_value", row.get("value"))))
    target = quest_target(definition, level)
    quest_type = str(definition.get("Type") or "")
    subtype = str(definition.get("SubType1") or "")
    return {
        "key": key,
        "level": level,
        "value": value,
        "target": target,
        "remaining": max(0, target - value),
        "complete": value >= target,
        "type": quest_type,
        "subtype": subtype,
        "kind": f"{quest_type}:{subtype}" if subtype else quest_type,
        "next": _int(definition.get("Next")),
        "name": definition.get("Name"),
    }


def fetch_current_series_task(session: GameSession) -> tuple[dict[str, Any] | None, dict]:
    body = quest_api.quest_list(session.client)
    _raise_if_kick(body, "quest/list")
    code = _code(body)
    if code not in (0, None):
        raise RuntimeError(
            f"quest/list failed code={code} msg={body.get('_message') or body.get('_details')}"
        )
    for row in _rows(body, "_questList"):
        task = _task_from_row(row)
        if task is not None:
            return task, body
    return None, body


def _goods_stock(session: GameSession, goods_type: int) -> int:
    goods = fetch_goods_map(session)
    return max(0, int(goods.get(int(goods_type), 0) or 0))


def _run_item_spawn(
    session: GameSession, *, remaining: int, log: LogFn
) -> dict[str, Any]:
    _body, stock = fetch_item_ticket_stock(session)
    planned = min(max(0, int(remaining)), max(0, int(stock)))
    if planned <= 0:
        return {
            "ok": True,
            "kind": "Spawn:Item",
            "attempted": False,
            "stock": stock,
            "stop_reason": "item_ticket_exhausted",
        }
    opened = run_spawn_batches(
        session,
        total=planned,
        item_ticket_start=stock,
        workers=1,
        auto_equip=True,
        auto_sell=True,
        log=log,
    )
    action = {
        "ok": bool(opened.get("ok")),
        "kind": "Spawn:Item",
        "attempted": True,
        "stock": stock,
        "planned": planned,
        "opened": _int(opened.get("items_ok")),
        "batches": _int(opened.get("batches_ok")),
    }
    if planned < remaining:
        action["stop_reason"] = "item_ticket_exhausted"
    elif not action["ok"]:
        action["stop_reason"] = "item_spawn_failed"
    return action


def _run_shop_spawn(
    session: GameSession, *, subtype: str, remaining: int, log: LogFn
) -> dict[str, Any]:
    config = SPAWN_SHOP[subtype]
    stock = _goods_stock(session, config["goods_type"])
    needed = max(0, int(remaining))
    purchases = (needed + config["count"] - 1) // config["count"]
    affordable = stock // config["cost"]
    planned = min(purchases, affordable)
    action: dict[str, Any] = {
        "ok": True,
        "kind": f"Spawn:{subtype}",
        "attempted": planned > 0,
        "stock": stock,
        "cost_each": config["cost"],
        "planned": planned,
        "completed": 0,
    }
    if planned <= 0:
        action["stop_reason"] = f"{subtype.lower()}_ticket_exhausted"
        return action

    for _ in range(planned):
        body = shop_api.shop_spawn(session.client, key=config["shop_key"])
        _raise_if_kick(body, f"shop/spawn key={config['shop_key']}")
        code = _code(body)
        if code not in (0, None):
            action["ok"] = False
            action["code"] = code
            action["message"] = body.get("_message") or body.get("_details")
            action["stop_reason"] = f"{subtype.lower()}_spawn_failed"
            break
        action["completed"] += 1
        log(f"[+] series quest spawn {subtype.lower()} ok key={config['shop_key']}")

    if action["ok"] and planned < purchases:
        action["stop_reason"] = f"{subtype.lower()}_ticket_exhausted"
    return action


def run_shop_spawn_calls(
    session: GameSession,
    *,
    subtype: str,
    times: int,
    log: LogFn = print,
) -> dict[str, Any]:
    """Run N standalone /api/shop/spawn calls for a series shop banner.

    Same action the series quest uses for Spawn:Skill / Spawn:Member.  Each
    call consumes ``cost`` tickets (30 = one 30-pull) and yields ``count``
    items.  Stops early when tickets run out or the server rejects a call.
    """
    config = SPAWN_SHOP[subtype]
    stock = _goods_stock(session, config["goods_type"])
    affordable = stock // config["cost"]
    planned = min(max(0, int(times)), affordable)
    action: dict[str, Any] = {
        "ok": True,
        "kind": f"Spawn:{subtype}",
        "shop_key": config["shop_key"],
        "goods_type": config["goods_type"],
        "cost_each": config["cost"],
        "yield_each": config["count"],
        "stock": stock,
        "requested": max(0, int(times)),
        "planned": planned,
        "completed": 0,
    }
    if planned <= 0:
        action["stop_reason"] = f"{subtype.lower()}_ticket_exhausted"
        return action

    for index in range(planned):
        if index > 0:
            _heartbeat(session)
        body = shop_api.shop_spawn(session.client, key=config["shop_key"])
        _raise_if_kick(body, f"shop/spawn key={config['shop_key']}")
        code = _code(body)
        if code not in (0, None):
            action["ok"] = False
            action["code"] = code
            action["message"] = (
                (body.get("_message") or body.get("_details"))
                if isinstance(body, dict)
                else None
            )
            action["stop_reason"] = f"{subtype.lower()}_spawn_failed"
            return action
        action["completed"] += 1
        log(
            f"[+] gacha {subtype.lower()} spawn ok "
            f"{action['completed']}/{planned} key={config['shop_key']}"
        )

    if planned < int(times):
        action["stop_reason"] = f"{subtype.lower()}_ticket_exhausted"
    return action


def _heartbeat(session: GameSession) -> None:
    ensure = getattr(session, "ensure_heartbeat", None)
    if callable(ensure):
        ensure()


def _run_mob_kills(
    session: GameSession, *, remaining: int, log: LogFn
) -> dict[str, Any]:
    """Kill only non-boss waves, then fail-end so story progress is unchanged."""
    refresh = getattr(session, "init_game_data", None)
    if callable(refresh):
        refresh()
    battle = getattr(session, "battle_info", None) or {}
    region = _int(battle.get("_region"), 1)
    stage = _int(battle.get("_stage"))
    sector = max(1, _int(battle.get("_sector"), 1))
    repeat = max(0, _int(battle.get("_repeat")))
    action: dict[str, Any] = {
        "ok": True,
        "kind": "KillMobCount",
        "attempted": False,
        "requested": max(0, int(remaining)),
        "killed": 0,
        "runs": 0,
        "frontier": f"{region}/{stage}/{sector}/{repeat}",
    }
    if stage <= 0:
        action["stop_reason"] = "story_frontier_missing"
        return action

    # At least one mob must be killed per run, so remaining is also a safe cap.
    while action["killed"] < remaining and action["runs"] < max(1, remaining):
        _heartbeat(session)
        start = session.battle_start(
            region=region,
            stage=stage,
            sector=sector,
            repeat=repeat,
            wave=0,
            state=battle_api.STATE_FORWARD,
            attr=battle_api.ATTR_PLAY,
        )
        _raise_if_kick(start, "battle/start[series-kill]")
        start_code = _code(start)
        if start_code not in (0, None):
            action.update(
                ok=False,
                code=start_code,
                message=start.get("_message") or start.get("_details"),
                stop_reason="battle_start_failed",
            )
            return action

        action["attempted"] = True
        action["runs"] += 1
        waves = _extract_spawn_waves(start.get("_spawnMobList") or {})
        kill_waves = waves[:-1] if len(waves) >= 2 else []
        if not kill_waves:
            end = session.battle_end(
                region=region,
                reason=battle_api.REASON_ALL_DEAD,
                state=battle_api.STATE_FAILED_BOSS,
                damage="0",
            )
            _raise_if_kick(end, "battle/end[series-no-small-mobs]")
            action["stop_reason"] = "no_non_boss_mobs"
            return action

        for wave_no, mobs in kill_waves:
            need = remaining - action["killed"]
            selected = list(mobs)[:need]
            if not selected:
                break
            _heartbeat(session)
            killed = session.battle_kill_mob(
                wave=wave_no,
                mob_uid_list=selected,
                reason=battle_api.REASON_NONE,
            )
            _raise_if_kick(killed, "battle/kill-mob[series]")
            kill_code = _code(killed)
            if kill_code not in (0, None):
                action.update(
                    ok=False,
                    code=kill_code,
                    message=killed.get("_message") or killed.get("_details"),
                    stop_reason="battle_kill_mob_failed",
                )
                return action
            action["killed"] += len(selected)
            if action["killed"] >= remaining:
                break

        _heartbeat(session)
        end = session.battle_end(
            region=region,
            reason=battle_api.REASON_ALL_DEAD,
            state=battle_api.STATE_FAILED_BOSS,
            damage="0",
        )
        _raise_if_kick(end, "battle/end[series-kill]")
        end_code = _code(end)
        if end_code not in (0, None):
            action.update(
                ok=False,
                code=end_code,
                message=end.get("_message") or end.get("_details"),
                stop_reason="battle_end_failed",
            )
            return action
        log(
            f"[+] series quest small mobs killed={action['killed']}/{remaining} "
            f"runs={action['runs']} (no story advance)"
        )
    return action


def _run_dungeon(
    session: GameSession, *, subtype: str, remaining: int, log: LogFn
) -> dict[str, Any]:
    key = DUNGEON_BY_SUBTYPE[subtype]
    action: dict[str, Any] = {
        "ok": True,
        "kind": f"DungeonClear:{subtype}",
        "attempted": False,
        "dungeon_key": key,
        "requested": max(0, int(remaining)),
        "cleared": 0,
    }
    for _ in range(max(0, int(remaining))):
        goods = fetch_goods_map(session)
        stock = ticket_stock_for_key(key, goods)
        action["ticket_stock"] = stock
        if stock <= 0:
            action["stop_reason"] = "dungeon_no_attempts"
            action["dungeon_no_attempts"] = True
            return action
        one = run_dungeon_clear(session, key=key, log=log)
        action["attempted"] = True
        if not one.get("ok"):
            action["ok"] = False
            action["code"] = one.get("start_code", one.get("end_code"))
            action["message"] = one.get("error") or one.get("end_message")
            action["stop_reason"] = "dungeon_clear_failed"
            return action
        action["cleared"] += 1
    return action


def _perform_task(
    session: GameSession, task: dict[str, Any], *, log: LogFn
) -> dict[str, Any]:
    quest_type = task["type"]
    subtype = task["subtype"]
    remaining = task["remaining"]
    if quest_type == "Spawn" and subtype == "Item":
        return _run_item_spawn(session, remaining=remaining, log=log)
    if quest_type == "Spawn" and subtype in SPAWN_SHOP:
        return _run_shop_spawn(session, subtype=subtype, remaining=remaining, log=log)
    if quest_type == "KillMobCount":
        return _run_mob_kills(session, remaining=remaining, log=log)
    if quest_type == "DungeonClear" and subtype in DUNGEON_BY_SUBTYPE:
        return _run_dungeon(session, subtype=subtype, remaining=remaining, log=log)
    # SectorClear is intentionally passive: claim it only if the server-side
    # cumulative value already satisfies the target.
    return {
        "ok": True,
        "kind": task["kind"],
        "attempted": False,
        "stop_reason": "unsupported_or_not_safe",
    }


def _same_progress(before: dict[str, Any], after: dict[str, Any] | None) -> bool:
    return bool(
        after
        and after.get("key") == before.get("key")
        and after.get("value") == before.get("value")
    )


def run_series_quest_care(
    session: GameSession,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    log: LogFn = print,
) -> dict[str, Any]:
    """Advance the current recurring Guide quest until a safe stop condition."""
    result: dict[str, Any] = {
        "ok": True,
        "start_task": None,
        "end_task": None,
        "claimed": [],
        "actions": [],
        "steps": 0,
        "stopped_reason": None,
        "dungeon_no_attempts": False,
    }
    try:
        for step in range(max(1, int(max_steps))):
            result["steps"] = step + 1
            task, _listed = fetch_current_series_task(session)
            if result["start_task"] is None and task is not None:
                result["start_task"] = dict(task)
            result["end_task"] = dict(task) if task is not None else None

            if task is None:
                result["stopped_reason"] = "no_series_task"
                break

            log(
                f"[*] series quest key={task['key']} lv={task['level']} "
                f"kind={task['kind']} value={task['value']}/{task['target']}"
            )
            if task["complete"]:
                body = quest_api.quest_complete(session.client, keys=[task["key"]])
                _raise_if_kick(body, f"quest/complete key={task['key']}")
                code = _code(body)
                if code not in (0, None):
                    result["ok"] = False
                    result["stopped_reason"] = "quest_claim_failed"
                    result["error"] = {
                        "key": task["key"],
                        "code": code,
                        "message": body.get("_message") or body.get("_details"),
                    }
                    break
                result["claimed"].append(task["key"])
                log(f"[+] series quest claimed key={task['key']}")
                continue

            action = _perform_task(session, task, log=log)
            after, _after_body = fetch_current_series_task(session)
            entry = dict(action)
            entry["task_key"] = task["key"]
            entry["before"] = task["value"]
            entry["after"] = after.get("value") if after and after.get("key") == task["key"] else None
            result["actions"].append(entry)
            result["end_task"] = dict(after) if after is not None else None

            stop_reason = action.get("stop_reason")
            if stop_reason:
                result["stopped_reason"] = stop_reason
                if stop_reason == "dungeon_no_attempts":
                    result["dungeon_no_attempts"] = True
                if not action.get("ok", False):
                    result["ok"] = False
                break
            if not action.get("ok", False):
                result["ok"] = False
                result["stopped_reason"] = "task_action_failed"
                break
            if _same_progress(task, after):
                result["stopped_reason"] = "task_made_no_progress"
                break
        else:
            result["stopped_reason"] = "safety_limit"
    except ItemSpawnerSessionKicked as exc:
        raise SessionKicked(exc.where, body=exc.body) from exc
    except DungeonSessionKicked as exc:
        raise SessionKicked(exc.where, body=exc.body) from exc
    except SessionKicked:
        raise
    except Exception as exc:
        result["ok"] = False
        result["stopped_reason"] = "series_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"[-] series quest error: {result['error']}")

    result["claimed_count"] = len(result["claimed"])
    result["action_count"] = len(result["actions"])
    log(
        f"[*] series quest summary ok={result['ok']} "
        f"claimed={result['claimed_count']} actions={result['action_count']} "
        f"stop={result['stopped_reason']}"
    )
    return result
