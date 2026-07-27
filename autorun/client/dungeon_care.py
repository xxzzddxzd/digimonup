"""Dungeon care: list progress, claim ad tickets, clear FB1/FB2 battles.

Live capture 2026-07-27:
  POST /api/dungeon/ad-view  encrypted {_key: dungeonKey}
  Response:
    _dungeon: {_key,_level,_challengeLevel,_isGetReward,_adCount}
    _rewardAllList._goodsList: ticket goods (e.g. type 100 for lamp / key 1)

Keys:
  - dungeon `_key` is from API `/api/dungeon/list` / init-data (not invented).
  - stageKey (e.g. 10000) comes from GameData.Dungeon mapping table.
  - API `_level` = highest cleared floor; challenge uses `_level+1`.
FB aliases (user mapping from live runs):
  fb 1 -> dungeon key 1 (Lamp, stageKey 10000)
  fb 2 -> dungeon key 2 (Lava, stageKey 10020)
  fb 3 -> dungeon key 3 (Flame, stageKey 10010)

auto dungeon policy:
  1. claim remaining ad tickets by `_adCount`
  2. burn ticket stock on clearable dungeons
  3. never battle stageKey 10000 / 10020 (keys 1/2) — ad only
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .apis import battle as battle_api
from .apis import dungeon as dungeon_api
from .apis import farm as farm_api
from .farm import _extract_spawn_waves
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006

# User-facing aliases -> dungeon progress _key
FB_KEY_ALIASES: dict[str, int] = {
    "1": 1,
    "fb1": 1,
    "lamp": 1,
    "2": 2,
    "fb2": 2,
    "lava": 2,
    "3": 3,
    "fb3": 3,
    "flame": 3,
}

# auto: only fill ads for these stages; never battle them.
AD_ONLY_STAGE_KEYS = {10000, 10020}  # Lamp / Lava
AD_ONLY_DUNGEON_KEYS = {1, 2}

# costTypes that use /api/dungeon/camp-sweep instead of /api/dungeon/sweep
CAMP_SWEEP_COST_TYPES = {
    "DungeonTicket_GuildDungeon1",
    "DungeonTicket_GuildDungeon2",
}

# Live: firewall sweep returns -29004 (not sweepable); battle start still -10001.
# Skip burning these until battle start is fixed.
NO_SWEEP_COST_TYPES = {
    "DungeonTicket_Firewall",
}

# E_GOODS_TYPE dungeon tickets (from client enum)
COST_TYPE_TO_GOODS: dict[str, int] = {
    "DungeonTicket_Lamp": 100,
    "DungeonTicket_Flame": 101,
    "DungeonTicket_Lava": 102,
    "DungeonTicket_TimeTower": 103,
    "DungeonReset_Trial": 104,
    "DungeonTicket_Defense": 105,
    "DungeonTicket_Firewall": 106,
    "DungeonTicket_GuildDungeon1": 107,
    "DungeonTicket_GuildDungeon2": 108,
    "PVPTicket": 356,
    "PVPTicket_Season": 357,
}

# Not consumed by /api/dungeon/* clear flow in auto.
SKIP_BATTLE_COST_TYPES = {
    "PVPTicket",
    "PVPTicket_Season",
    "DungeonReset_Trial",
    "",
}

SAFETY_MAX_CLEARS_PER_KEY = 20

DEFAULT_KEY_STAGE_PATH = Path(__file__).resolve().parent.parent / "dungeon_key_stage.json"


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


def _code(body: Any) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    c = body.get("_code")
    if c is None:
        return 0
    try:
        return int(c)
    except Exception:
        return None


def _raise_if_kick(body: Any, where: str) -> None:
    if _code(body) == SESSION_KICK:
        raise SessionKicked(where, body=body)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def load_key_stage_map(path: Path | str | None = None) -> dict[int, int]:
    """key (dungeon progress key) -> stageKey used by /api/dungeon/start _stage."""
    p = Path(path) if path else DEFAULT_KEY_STAGE_PATH
    if not p.exists():
        return {
            1: 10000,
            2: 10020,
            3: 10010,
            4: 10030,
            5: 10500,
        }
    doc = json.loads(p.read_text(encoding="utf-8"))
    raw = doc.get("map") or {}
    out: dict[int, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = int(v)
            except Exception:
                continue
    return out


def load_key_meta(path: Path | str | None = None) -> dict[int, dict]:
    p = Path(path) if path else DEFAULT_KEY_STAGE_PATH
    out: dict[int, dict] = {}
    if not p.exists():
        return out
    doc = json.loads(p.read_text(encoding="utf-8"))
    for row in doc.get("rows") or []:
        if not isinstance(row, dict):
            continue
        try:
            key = int(row.get("key"))
        except Exception:
            continue
        out[key] = row
    return out


def resolve_fb_key(alias_or_key: str | int) -> int:
    if isinstance(alias_or_key, int):
        return int(alias_or_key)
    s = str(alias_or_key).strip().lower()
    if s in FB_KEY_ALIASES:
        return FB_KEY_ALIASES[s]
    return int(s)


def stage_key_for(dungeon_key: int, key_stage: dict[int, int] | None = None) -> int:
    m = key_stage if key_stage is not None else load_key_stage_map()
    if int(dungeon_key) not in m:
        raise KeyError(f"no stageKey mapping for dungeon key={dungeon_key}")
    return int(m[int(dungeon_key)])


def fetch_dungeon_rows(session: GameSession, *, prefer_list: bool = True) -> tuple[list[dict], dict]:
    """Return (rows, source_body). Prefer live /api/dungeon/list, fall back to init-data."""
    body: dict = {}
    if prefer_list:
        body = dungeon_api.dungeon_list(session.client)
        _raise_if_kick(body, "dungeon/list")
        rows = dungeon_api.extract_dungeon_list(body)
        if rows:
            return rows, body
    # fallback: init snapshot (type 8)
    rows = dungeon_api.extract_dungeon_list(session.init_data or {})
    return rows, body or (session.init_data or {})


def progress_for(rows: Sequence[dict], key: int) -> dict | None:
    return dungeon_api.find_dungeon_progress(rows, key)


def pick_level(progress: dict | None, *, override: int | None = None) -> int:
    """Pick dungeon difficulty for start._repeat.

    API `_level` is the highest **cleared** floor (e.g. 59). The next challenge is
    `_level + 1` (60). Brand-new rows report `_level=0` -> play 1.

    Prefer explicit override when provided.
    """
    if override is not None:
        return max(1, int(override))
    if not progress:
        return 1
    level = _int(progress.get("_level") or progress.get("level"), 0)
    # next uncleared floor
    return max(1, level + 1)


def pick_sweep_level(progress: dict | None, *, override: int | None = None) -> int | None:
    """Level for /api/dungeon/sweep: must be an already-cleared floor.

    Live: sweep `_level` = progress `_level` (highest cleared). Returns None if
    nothing has been cleared yet (`_level=0`) — then battle clear is required.
    """
    if override is not None:
        return max(1, int(override))
    if not progress:
        return None
    level = _int(progress.get("_level") or progress.get("level"), 0)
    if level <= 0:
        return None
    return level


def summarize_row(row: dict, meta: dict[int, dict] | None = None) -> dict[str, Any]:
    key = _int(row.get("_key") or row.get("key"))
    m = (meta or {}).get(key) or {}
    return {
        "key": key,
        "name": m.get("name"),
        "stageKey": m.get("stageKey"),
        "level": _int(row.get("_level") or row.get("level")),
        "challengeLevel": _int(row.get("_challengeLevel") or row.get("challengeLevel")),
        "adCount": _int(row.get("_adCount") or row.get("adCount")),
        "isGetReward": bool(row.get("_isGetReward", row.get("isGetReward", False))),
        "costType": m.get("costType"),
        "adMax": m.get("adCount"),
        "playCount": m.get("playCount"),
    }


def _goods_value(goods_payload: dict, goods_type: int) -> int:
    gl = goods_payload.get("_goodsList") or goods_payload.get("goodsList") or {}
    lst = gl.get("_list") if isinstance(gl, dict) else gl
    if not isinstance(lst, list):
        return 0
    for it in lst:
        if not isinstance(it, dict):
            continue
        t = it.get("_type", it.get("type"))
        try:
            if int(t) != int(goods_type):
                continue
        except Exception:
            continue
        return _int(it.get("_value") or it.get("value") or it.get("_count") or it.get("count"))
    return 0


def _extract_goods_deltas(body: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return out
    ra = body.get("_rewardAllList") or body.get("rewardAllList") or {}
    if not isinstance(ra, dict):
        return out
    gl = ra.get("_goodsList") or ra.get("goodsList") or {}
    lst = gl.get("_list") if isinstance(gl, dict) else gl
    if not isinstance(lst, list):
        return out
    for it in lst:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "type": it.get("_type", it.get("type")),
                "value": it.get("_value", it.get("value")),
                "accUseValue": it.get("_accUseValue", it.get("accUseValue")),
                "count": it.get("_count", it.get("count")),
            }
        )
    return out


def _extract_reward_list(body: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ra = body.get("_rewardAllList") or {}
    if not isinstance(ra, dict):
        return out
    rl = ra.get("_rewardList") or {}
    lst = rl.get("_list") if isinstance(rl, dict) else rl
    if not isinstance(lst, list):
        return out
    for it in lst:
        if isinstance(it, dict):
            out.append(
                {
                    "type": it.get("_type", it.get("type")),
                    "value": it.get("_value", it.get("value")),
                    "count": it.get("_count", it.get("count")),
                }
            )
    return out


def claim_ad_ticket(
    session: GameSession,
    *,
    key: int = 1,
    log: LogFn = print,
) -> dict[str, Any]:
    """Claim one dungeon ticket via /api/dungeon/ad-view {_key}.

    Live body: only `_key` (dungeon progress key). No ad SDK token is required by
    the game API itself — client UI just gates the button.
    """
    key = int(key)
    result: dict[str, Any] = {"ok": False, "key": key, "path": "/api/dungeon/ad-view"}

    rows, listed = fetch_dungeon_rows(session)
    result["list_code"] = _code(listed) if listed else None
    before = progress_for(rows, key)
    result["before"] = summarize_row(before or {"_key": key}, load_key_meta())
    log(
        f"[*] dungeon ad-view key={key} "
        f"before.adCount={result['before'].get('adCount')} "
        f"before.level={result['before'].get('level')}"
    )

    body = dungeon_api.dungeon_ad_view(session.client, key=key)
    _raise_if_kick(body, f"dungeon/ad-view key={key}")
    result["code"] = _code(body)
    result["message"] = body.get("_message") or body.get("_details")
    result["raw"] = body

    dungeon = body.get("_dungeon") if isinstance(body.get("_dungeon"), dict) else None
    if dungeon:
        result["after"] = summarize_row(dungeon, load_key_meta())
    result["goods"] = _extract_goods_deltas(body)
    result["rewards"] = _extract_reward_list(body)

    ok = result["code"] in (0, None)
    result["ok"] = ok
    if ok:
        g = result["goods"]
        log(
            f"[+] ad-view ok key={key} adCount="
            f"{(result.get('after') or {}).get('adCount')} goods={g} rewards={result['rewards']}"
        )
    else:
        log(f"[-] ad-view fail key={key} code={result['code']} msg={result['message']}")
    return result


def run_dungeon_sweep(
    session: GameSession,
    *,
    key: int,
    level: int | None = None,
    sector: int = 1,
    log: LogFn = print,
) -> dict[str, Any]:
    """One ticket clear via /api/dungeon/sweep (no start/end battle).

    Requires an already-cleared floor. Body: {_key, _sector, _level=cleared}.
    Live 2026-07-27: flame key=3 level=42 -> code=0, goods 101 3->2.
    """
    key = int(key)
    meta = load_key_meta()
    rows, listed = fetch_dungeon_rows(session)
    prog = progress_for(rows, key)
    use_level = pick_sweep_level(prog, override=level)

    result: dict[str, Any] = {
        "ok": False,
        "mode": "sweep",
        "key": key,
        "sector": int(sector),
        "level": use_level,
        "stageKey": (meta.get(key) or {}).get("stageKey"),
        "progress_before": summarize_row(prog or {"_key": key}, meta),
        "list_code": _code(listed) if listed else None,
    }
    if use_level is None:
        result["error"] = "no_cleared_level"
        log(f"[-] dungeon/sweep skip key={key}: no cleared level yet")
        return result

    log(
        f"[*] dungeon sweep key={key} sector={sector} level={use_level} "
        f"name={(meta.get(key) or {}).get('name')}"
    )
    body = dungeon_api.dungeon_sweep(
        session.client, key=key, sector=int(sector), level=int(use_level)
    )
    _raise_if_kick(body, "dungeon/sweep")
    result["code"] = _code(body)
    result["message"] = body.get("_message") or body.get("_details")
    result["raw"] = body
    result["goods"] = _extract_goods_deltas(body)
    result["rewards"] = _extract_reward_list(body)
    d = body.get("_dungeon") if isinstance(body.get("_dungeon"), dict) else None
    if d:
        result["progress_after"] = summarize_row(d, meta)
    else:
        rows2, _ = fetch_dungeon_rows(session)
        result["progress_after"] = summarize_row(
            progress_for(rows2, key) or {"_key": key}, meta
        )

    ok = result["code"] in (0, None)
    result["ok"] = ok
    if ok:
        log(
            f"[+] dungeon/sweep ok key={key} level={use_level} "
            f"goods={result['goods']} rewards={len(result.get('rewards') or [])}"
        )
    else:
        log(
            f"[-] dungeon/sweep fail key={key} code={result['code']} "
            f"msg={result['message']}"
        )
    return result


def run_dungeon_camp_sweep(
    session: GameSession,
    *,
    key: int,
    level: int | None = None,
    sector: int = 1,
    log: LogFn = print,
) -> dict[str, Any]:
    """Guild dungeon ticket clear via /api/dungeon/camp-sweep.

    Live: key=15/16 level=1 works even when progress `_level=0`.
    """
    key = int(key)
    meta = load_key_meta()
    rows, listed = fetch_dungeon_rows(session)
    prog = progress_for(rows, key)
    # camp-sweep accepts level=1 with no prior clear
    use_level = int(level) if level is not None else (pick_sweep_level(prog) or 1)

    result: dict[str, Any] = {
        "ok": False,
        "mode": "camp_sweep",
        "key": key,
        "sector": int(sector),
        "level": use_level,
        "stageKey": (meta.get(key) or {}).get("stageKey"),
        "progress_before": summarize_row(prog or {"_key": key}, meta),
        "list_code": _code(listed) if listed else None,
    }
    log(
        f"[*] dungeon camp-sweep key={key} sector={sector} level={use_level} "
        f"name={(meta.get(key) or {}).get('name')}"
    )
    body = dungeon_api.dungeon_camp_sweep(
        session.client, key=key, sector=int(sector), level=int(use_level)
    )
    _raise_if_kick(body, "dungeon/camp-sweep")
    result["code"] = _code(body)
    result["message"] = body.get("_message") or body.get("_details")
    result["raw"] = body
    result["goods"] = _extract_goods_deltas(body)
    result["rewards"] = _extract_reward_list(body)
    d = body.get("_dungeon") if isinstance(body.get("_dungeon"), dict) else None
    if d:
        result["progress_after"] = summarize_row(d, meta)
    else:
        rows2, _ = fetch_dungeon_rows(session)
        result["progress_after"] = summarize_row(
            progress_for(rows2, key) or {"_key": key}, meta
        )
    ok = result["code"] in (0, None)
    result["ok"] = ok
    if ok:
        log(
            f"[+] dungeon/camp-sweep ok key={key} level={use_level} "
            f"goods={result['goods']}"
        )
    else:
        log(
            f"[-] dungeon/camp-sweep fail key={key} code={result['code']} "
            f"msg={result['message']}"
        )
    return result


def run_dungeon_clear(
    session: GameSession,
    *,
    key: int,
    level: int | None = None,
    sector: int = 1,
    region: int = dungeon_api.REGION_DUNGEON,
    log: LogFn = print,
) -> dict[str, Any]:
    """One clear: dungeon/start -> kill-mob waves -> dungeon/end(CLEAR)."""
    key = int(key)
    key_stage = load_key_stage_map()
    meta = load_key_meta()
    stage = stage_key_for(key, key_stage)
    rows, listed = fetch_dungeon_rows(session)
    prog = progress_for(rows, key)
    use_level = pick_level(prog, override=level)

    result: dict[str, Any] = {
        "ok": False,
        "key": key,
        "stageKey": stage,
        "sector": int(sector),
        "level": use_level,
        "region": int(region),
        "progress_before": summarize_row(prog or {"_key": key}, meta),
        "list_code": _code(listed) if listed else None,
    }
    log(
        f"[*] dungeon clear key={key} stageKey={stage} sector={sector} "
        f"level(repeat)={use_level} name={(meta.get(key) or {}).get('name')}"
    )

    start = dungeon_api.dungeon_start(
        session.client,
        stage=stage,
        sector=int(sector),
        level=use_level,
        region=int(region),
        wave=0,
        state=battle_api.STATE_FORWARD,
        attr=battle_api.ATTR_PLAY,
    )
    _raise_if_kick(start, "dungeon/start")
    result["start_code"] = _code(start)
    result["start"] = {
        "code": _code(start),
        "message": start.get("_message") or start.get("_details"),
        "battle": start.get("_battle"),
    }
    if result["start_code"] not in (0, None):
        log(
            f"[-] dungeon/start fail code={result['start_code']} "
            f"msg={result['start']['message']}"
        )
        result["raw_start"] = start
        return result

    waves = _extract_spawn_waves(start.get("_spawnMobList") or {})
    result["waves"] = [(w, len(m)) for w, m in waves]
    log(f"[+] dungeon/start ok waves={result['waves']}")

    killed = 0
    for wave_no, mobs in waves:
        km = battle_api.battle_kill_mob(
            session.client,
            wave=wave_no,
            mob_uid_list=mobs,
            reason=battle_api.REASON_NONE,
        )
        _raise_if_kick(km, f"battle/kill-mob wave={wave_no}")
        kcode = _code(km)
        if kcode not in (0, None):
            result["kill_fail"] = {
                "wave": wave_no,
                "code": kcode,
                "message": km.get("_message") or km.get("_details"),
            }
            log(f"[-] kill-mob fail wave={wave_no} code={kcode}")
            result["raw_kill"] = km
            return result
        killed += len(mobs)
    result["mobs_killed"] = killed

    end = dungeon_api.dungeon_end(
        session.client,
        region=int(region),
        reason=battle_api.REASON_CLEAR,
        state=battle_api.STATE_FORWARD,
        damage="0",
        send_damage="0",
        receive_damage="0",
        speed=1.0,
    )
    _raise_if_kick(end, "dungeon/end")
    result["end_code"] = _code(end)
    result["end_message"] = end.get("_message") or end.get("_details")
    result["end_battle"] = end.get("_battle")
    result["end_goods"] = _extract_goods_deltas(end)
    result["end_rewards"] = _extract_reward_list(end)
    result["raw_end"] = end

    # refresh progress
    rows2, listed2 = fetch_dungeon_rows(session)
    prog2 = progress_for(rows2, key)
    result["progress_after"] = summarize_row(prog2 or {"_key": key}, meta)
    result["list_code_after"] = _code(listed2) if listed2 else None

    ok = result["end_code"] in (0, None)
    result["ok"] = ok
    if ok:
        log(
            f"[+] dungeon/end ok key={key} level_after="
            f"{result['progress_after'].get('level')} goods={result['end_goods']}"
        )
    else:
        log(f"[-] dungeon/end fail code={result['end_code']} msg={result['end_message']}")
    return result


def run_fb(
    session: GameSession,
    *,
    alias: str | int,
    level: int | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    key = resolve_fb_key(alias)
    rows, _ = fetch_dungeon_rows(session)
    prog = progress_for(rows, key)
    if level is None and pick_sweep_level(prog) is not None:
        out = run_dungeon_sweep(session, key=key, log=log)
    else:
        out = run_dungeon_clear(session, key=key, level=level, log=log)
    out["alias"] = str(alias)
    out["fb_key"] = key
    return out



def run_dungeon_ad_care(
    session: GameSession,
    *,
    keys: Sequence[int] | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Claim remaining dungeon ad tickets for each key with `_adCount > 0`.

    For each dungeon row from /api/dungeon/list:
      remaining = row._adCount
      if remaining <= 0: skip
      else call /api/dungeon/ad-view exactly `remaining` times (stop early on fail)

    `keys` optionally restricts which dungeon keys to process; default = all with ads.
    """
    rows, listed = fetch_dungeon_rows(session)
    meta = load_key_meta()
    result: dict[str, Any] = {
        "ok": True,
        "list_code": _code(listed) if listed else None,
        "claimed": [],
        "skipped": [],
        "failed": [],
        "total_ok": 0,
        "total_fail": 0,
    }

    allow = None if keys is None else {int(k) for k in keys}
    # process stable key order
    ordered = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: _int(r.get("_key") or r.get("key")),
    )

    for row in ordered:
        key = _int(row.get("_key") or row.get("key"))
        if allow is not None and key not in allow:
            continue
        remaining = _int(row.get("_adCount") or row.get("adCount"), 0)
        name = (meta.get(key) or {}).get("name")
        if remaining <= 0:
            result["skipped"].append({"key": key, "adCount": remaining, "name": name, "reason": "no_ad_count"})
            continue

        log(f"[*] dungeon-ad key={key} name={name} remaining={remaining}")
        for i in range(remaining):
            one = claim_ad_ticket(session, key=key, log=log)
            entry = {
                "key": key,
                "index": i + 1,
                "of": remaining,
                "ok": bool(one.get("ok")),
                "code": one.get("code"),
                "adCount_after": (one.get("after") or {}).get("adCount"),
                "goods": one.get("goods"),
            }
            if one.get("ok"):
                result["claimed"].append(entry)
                result["total_ok"] += 1
            else:
                result["failed"].append(entry)
                result["total_fail"] += 1
                result["ok"] = False
                log(f"[-] dungeon-ad stop key={key} at {i+1}/{remaining} code={one.get('code')}")
                break

    log(
        f"[*] dungeon-ad summary ok={result['total_ok']} fail={result['total_fail']} "
        f"skipped={len(result['skipped'])}"
    )
    return result



def goods_type_for_dungeon(key: int, meta: dict[int, dict] | None = None) -> int | None:
    m = (meta or load_key_meta()).get(int(key)) or {}
    cost_type = str(m.get("costType") or "")
    if not cost_type:
        return None
    return COST_TYPE_TO_GOODS.get(cost_type)


def fetch_goods_map(session: GameSession) -> dict[int, int]:
    body = farm_api.goods_list(session.client)
    _raise_if_kick(body, "goods/list")
    out: dict[int, int] = {}
    gl = body.get("_goodsList") or body.get("goodsList") or {}
    lst = gl.get("_list") if isinstance(gl, dict) else gl
    if not isinstance(lst, list):
        return out
    for it in lst:
        if not isinstance(it, dict):
            continue
        try:
            t = int(it.get("_type") or it.get("type"))
        except Exception:
            continue
        out[t] = _int(it.get("_value") or it.get("value") or it.get("_count") or it.get("count"))
    return out


def ticket_stock_for_key(
    key: int,
    goods_map: dict[int, int],
    meta: dict[int, dict] | None = None,
) -> int:
    gt = goods_type_for_dungeon(key, meta)
    if gt is None:
        return 0
    return int(goods_map.get(int(gt), 0) or 0)


def is_ad_only_dungeon(key: int, stage_key: int | None = None) -> bool:
    if int(key) in AD_ONLY_DUNGEON_KEYS:
        return True
    if stage_key is not None and int(stage_key) in AD_ONLY_STAGE_KEYS:
        return True
    return False


def run_dungeon_auto_care(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict[str, Any]:
    """auto dungeon flow:

    1. claim remaining ads by `_adCount` (all keys, including 10000/10020)
    2. for dungeons with ticket stock: prefer /api/dungeon/sweep on cleared floors
       until tickets are gone (fallback battle clear if never cleared)
    3. stageKey 10000 / 10020 (keys 1/2): ads only, never battle/sweep
    """
    result: dict[str, Any] = {
        "ok": True,
        "ad": None,
        "clears": [],
        "skipped_battle": [],
        "total_clears": 0,
        "total_clear_fail": 0,
    }

    ad = run_dungeon_ad_care(session, log=log)
    result["ad"] = {
        "ok": ad.get("ok"),
        "total_ok": ad.get("total_ok"),
        "total_fail": ad.get("total_fail"),
        "skipped": len(ad.get("skipped") or []),
        "claimed_keys": sorted({c.get("key") for c in (ad.get("claimed") or [])}),
    }
    if not ad.get("ok"):
        result["ok"] = False

    meta = load_key_meta()
    key_stage = load_key_stage_map()
    rows, listed = fetch_dungeon_rows(session)
    goods_map = fetch_goods_map(session)

    ordered = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: _int(r.get("_key") or r.get("key")),
    )

    for row in ordered:
        key = _int(row.get("_key") or row.get("key"))
        stage = key_stage.get(key) or (meta.get(key) or {}).get("stageKey")
        name = (meta.get(key) or {}).get("name")
        cost_type = str((meta.get(key) or {}).get("costType") or "")
        cost = _int((meta.get(key) or {}).get("cost"), 1) or 1
        stock = ticket_stock_for_key(key, goods_map, meta)

        if is_ad_only_dungeon(key, stage):
            result["skipped_battle"].append(
                {
                    "key": key,
                    "stageKey": stage,
                    "name": name,
                    "tickets": stock,
                    "reason": "ad_only_10000_10020",
                }
            )
            log(f"[*] dungeon skip-battle key={key} stage={stage} name={name} tickets={stock} (ad only)")
            continue

        if cost_type in SKIP_BATTLE_COST_TYPES or not cost_type:
            result["skipped_battle"].append(
                {
                    "key": key,
                    "stageKey": stage,
                    "name": name,
                    "tickets": stock,
                    "reason": f"costType={cost_type or 'none'}",
                }
            )
            continue

        if stock <= 0:
            result["skipped_battle"].append(
                {
                    "key": key,
                    "stageKey": stage,
                    "name": name,
                    "tickets": 0,
                    "reason": "no_tickets",
                }
            )
            continue

        if cost_type in NO_SWEEP_COST_TYPES:
            result["skipped_battle"].append(
                {
                    "key": key,
                    "stageKey": stage,
                    "name": name,
                    "tickets": stock,
                    "reason": f"no_sweep:{cost_type}",
                }
            )
            log(
                f"[*] dungeon skip-burn key={key} stage={stage} name={name} "
                f"tickets={stock} reason=no_sweep ({cost_type})"
            )
            continue

        log(
            f"[*] dungeon burn key={key} stage={stage} name={name} "
            f"tickets={stock} cost={cost} costType={cost_type}"
        )
        clears_done = 0
        fails = 0
        # re-check stock each clear; also cap for safety
        while clears_done < SAFETY_MAX_CLEARS_PER_KEY:
            goods_map = fetch_goods_map(session)
            stock = ticket_stock_for_key(key, goods_map, meta)
            if stock < cost:
                break
            # Route: camp-sweep (guild) / sweep (cleared floor) / battle fallback.
            rows_now, _ = fetch_dungeon_rows(session)
            prog_now = progress_for(rows_now, key)
            if cost_type in CAMP_SWEEP_COST_TYPES:
                one = run_dungeon_camp_sweep(session, key=key, log=log)
                entry = {
                    "key": key,
                    "stageKey": stage,
                    "name": name,
                    "mode": "camp_sweep",
                    "ok": bool(one.get("ok")),
                    "level": one.get("level"),
                    "code": one.get("code"),
                    "tickets_before": stock,
                }
            else:
                sweep_lv = pick_sweep_level(prog_now)
                if sweep_lv is not None:
                    one = run_dungeon_sweep(session, key=key, level=sweep_lv, log=log)
                    entry = {
                        "key": key,
                        "stageKey": stage,
                        "name": name,
                        "mode": "sweep",
                        "ok": bool(one.get("ok")),
                        "level": one.get("level"),
                        "code": one.get("code"),
                        "tickets_before": stock,
                    }
                else:
                    one = run_dungeon_clear(session, key=key, log=log)
                    entry = {
                        "key": key,
                        "stageKey": stage,
                        "name": name,
                        "mode": "battle",
                        "ok": bool(one.get("ok")),
                        "level": one.get("level"),
                        "start_code": one.get("start_code"),
                        "end_code": one.get("end_code"),
                        "tickets_before": stock,
                    }
            result["clears"].append(entry)
            if one.get("ok"):
                clears_done += 1
                result["total_clears"] += 1
            else:
                fails += 1
                result["total_clear_fail"] += 1
                result["ok"] = False
                log(
                    f"[-] dungeon clear stop key={key} mode={entry.get('mode')} "
                    f"code={one.get('code') or one.get('start_code')} "
                    f"end={one.get('end_code')}"
                )
                break

        # refresh stock after this key
        goods_map = fetch_goods_map(session)
        stock_after = ticket_stock_for_key(key, goods_map, meta)
        log(
            f"[*] dungeon burn done key={key} clears={clears_done} fail={fails} "
            f"tickets_after={stock_after}"
        )

    log(
        f"[*] dungeon-auto summary ad_ok={result['ad'].get('total_ok')} "
        f"clears={result['total_clears']} clear_fail={result['total_clear_fail']} "
        f"skip_battle={len(result['skipped_battle'])}"
    )
    return result


def list_dungeons(session: GameSession, *, log: LogFn = print) -> dict[str, Any]:
    meta = load_key_meta()
    key_stage = load_key_stage_map()
    rows, body = fetch_dungeon_rows(session)
    items = []
    for row in rows:
        s = summarize_row(row, meta)
        if s.get("stageKey") is None and s.get("key") in key_stage:
            s["stageKey"] = key_stage[int(s["key"])]
        items.append(s)
    items.sort(key=lambda x: int(x.get("key") or 0))
    result = {
        "ok": _code(body) in (0, None) if body else True,
        "code": _code(body) if body else 0,
        "count": len(items),
        "items": items,
        "playList": ((body or {}).get("_playList") or {}).get("_list")
        if isinstance(body, dict)
        else None,
    }
    for it in items:
        log(
            f"[*] dungeon key={it['key']:>2} stage={it.get('stageKey')} "
            f"lv={it.get('level')} ad={it.get('adCount')}/{it.get('adMax')} "
            f"name={it.get('name')} cost={it.get('costType')}"
        )
    return result
