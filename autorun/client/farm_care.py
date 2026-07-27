"""肉田 (farm field) maintain: water near-done plots, harvest, re-seed.

Protocol:
  list:     POST /api/farm/list {}
  info:     POST /api/farm/info {}
  harvest:  POST /api/farm/harvest {"_index"}
  seed:     POST /api/farm/seed {"_index","_type"}
  watering: POST /api/farm/watering {"_index","_type","_count"}
  goods:    POST /api/goods/list {}  (watering can / seed stock)

  seed_ad:  POST /api/farm/ad-view {}
            PS_FarmSeedADView — empty body
            remaining: `_farm._adCount` (also `_farmADCount` on daily reset)
  water_ad: POST /api/farm/watering-ad-view {}
            PS_FarmFieldWateringADView — empty body
            remaining: `_farmWatering._adCount` (also `_farmWateringADCount`)

E_FARM_FIELD_STATE: Seed=0 Growing=1 GrowingComplete=2 ...
Seeds: 200/201/202
Watering cans: Farm_WateringCan1=203 (live: -1800s each), Farm_WateringCan2=204

Policy (user):
  1. If seed-ad / watering-ad remaining > 0, claim them first to replenish stock.
  2. If a growing plot has remaining time < 1 hour and watering stock is enough,
     use ceil(left/30min) waterings, then re-check and harvest.
  3. Keep unlocked plots planted.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from .apis import farm as farm_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

STATE_SEED = 0
STATE_GROWING = 1
STATE_GROWING_COMPLETE = 2

SEED_TYPES = (200, 201, 202)
WATER_TYPE_SMALL = 203  # -30 min (measured)
WATER_TYPE_LARGE = 204
WATER_REDUCE_SEC = 1800.0
WATER_THRESHOLD_SEC = 3600.0  # only water when left < 1h

CODE_LOCKED = -37001
CODE_BAD_PLANT_STATE = -48006
CODE_NOT_READY = -48009
SESSION_KICK = -19006


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


def _fields_from_list(payload: dict) -> list[dict]:
    fl = payload.get("_farmFieldList") or payload.get("farmFieldList") or {}
    lst = fl.get("_list") or fl.get("list") or []
    if not isinstance(lst, list):
        return []
    out = [it for it in lst if isinstance(it, dict)]
    out.sort(key=lambda x: int(x.get("_index") or x.get("index") or 0))
    return out


def _field_summary(f: dict, server_ms: int | None = None) -> str:
    end = int(f.get("_endTime") or f.get("endTime") or 0)
    left = None
    if server_ms is not None and end:
        left = (end - server_ms) / 1000.0
    base = (
        f"idx={f.get('_index', f.get('index'))} "
        f"state={f.get('_state', f.get('state'))} "
        f"end={end} amt={f.get('_harvestAmount', f.get('harvestAmount'))}"
    )
    if left is not None:
        base += f" left={left:.0f}s"
    return base


def _goods_value(goods_payload: dict, goods_type: int) -> int:
    gl = goods_payload.get("_goodsList") or goods_payload.get("goodsList") or {}
    lst = gl.get("_list") or gl.get("list") or []
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
        raw = it.get("_value", it.get("value", 0))
        try:
            return int(float(raw))
        except Exception:
            return 0
    return 0



def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _reward_goods_delta(body: Any, goods_type: int) -> int:
    """Sum reward delta for a goods type from `_rewardAllList._rewardList`."""
    if not isinstance(body, dict):
        return 0
    rewards = body.get("_rewardAllList") or {}
    reward_list = rewards.get("_rewardList") or {}
    ritems = reward_list.get("_list") or reward_list.get("list") or []
    total = 0
    if isinstance(ritems, list):
        for it in ritems:
            if not isinstance(it, dict):
                continue
            # reward entry: {_type:1,_value:<goodsType>,_count:<delta>}
            if _as_int(it.get("_value") or it.get("value")) == int(goods_type):
                total += _as_int(it.get("_count") or it.get("count"))
    return total


def _claim_seed_ads(
    session: GameSession,
    *,
    remaining: int,
    log: LogFn,
    result: dict,
) -> int:
    """Claim remaining seed ads via /api/farm/ad-view. Returns remaining after claims."""
    left = max(0, int(remaining))
    claimed = 0
    # Hard cap to remaining to avoid infinite loops if server omits decrement.
    for _ in range(left):
        if left <= 0:
            break
        body = farm_api.seed_ad_view(session.client)
        _raise_if_kick(body, "farm/ad-view")
        code = _code(body)
        entry = {
            "kind": "seed_ad",
            "code": code,
            "message": body.get("_message") if isinstance(body, dict) else None,
            "before": left,
        }
        if code not in (0, None):
            log(f"[-] farm seed-ad fail code={code} msg={entry['message']}")
            result["errors"].append(entry)
            break
        farm = body.get("_farm") if isinstance(body, dict) else None
        if isinstance(farm, dict) and farm.get("_adCount") is not None:
            after = max(0, _as_int(farm.get("_adCount")))
        else:
            after = max(0, left - 1)
        got = 0
        for typ in SEED_TYPES:
            got = _reward_goods_delta(body, typ)
            if got:
                break
        entry.update({"after": after, "reward_count": got})
        result["seed_ads"].append(entry)
        claimed += 1
        # Always make progress even if server returns stale remaining.
        left = min(after, max(0, left - 1))
        log(f"[+] farm seed-ad ok remaining={left} reward~={got}")
    result["seed_ads_claimed"] = claimed
    return left


def _claim_watering_ads(
    session: GameSession,
    *,
    remaining: int,
    log: LogFn,
    result: dict,
) -> int:
    """Claim remaining watering-can ads via /api/farm/watering-ad-view."""
    left = max(0, int(remaining))
    claimed = 0
    for _ in range(left):
        if left <= 0:
            break
        body = farm_api.watering_ad_view(session.client)
        _raise_if_kick(body, "farm/watering-ad-view")
        code = _code(body)
        entry = {
            "kind": "watering_ad",
            "code": code,
            "message": body.get("_message") if isinstance(body, dict) else None,
            "before": left,
        }
        if code not in (0, None):
            log(f"[-] farm watering-ad fail code={code} msg={entry['message']}")
            result["errors"].append(entry)
            break
        fw = body.get("_farmWatering") if isinstance(body, dict) else None
        if isinstance(fw, dict) and fw.get("_adCount") is not None:
            after = max(0, _as_int(fw.get("_adCount")))
        else:
            after = max(0, left - 1)
        got = _reward_goods_delta(body, WATER_TYPE_SMALL)
        entry.update(
            {
                "after": after,
                "reward_count": got,
                "viewCount": (fw or {}).get("_viewCount") if isinstance(fw, dict) else None,
            }
        )
        result["watering_ads"].append(entry)
        claimed += 1
        left = min(after, max(0, left - 1))
        log(f"[+] farm watering-ad ok remaining={left} cans+={got}")
    result["watering_ads_claimed"] = claimed
    return left



def _is_harvestable(f: dict, server_ms: int) -> bool:
    state = int(f.get("_state") or f.get("state") or 0)
    if state == STATE_GROWING_COMPLETE:
        return True
    if state == STATE_GROWING:
        end = int(f.get("_endTime") or f.get("endTime") or 0)
        if end and end <= server_ms:
            return True
    return False


def _is_empty_seed(f: dict) -> bool:
    return int(f.get("_state") or f.get("state") or 0) == STATE_SEED


def _left_sec(f: dict, server_ms: int) -> Optional[float]:
    end = int(f.get("_endTime") or f.get("endTime") or 0)
    if not end:
        return None
    return (end - server_ms) / 1000.0


def _try_seed(session: GameSession, index: int, *, log: LogFn) -> dict:
    last: dict = {"code": None, "message": None}
    for typ in SEED_TYPES:
        resp = farm_api.seed(session.client, index=index, seed_type=typ)
        _raise_if_kick(resp, f"farm/seed[{index}]")
        code = _code(resp)
        last = {
            "code": code,
            "message": resp.get("_message"),
            "type": typ,
            "field": resp.get("_farmField"),
        }
        if code in (0, None):
            log(f"[+] farm seed idx={index} type={typ} ok")
            return last
        if code == CODE_LOCKED:
            log(f"[*] farm seed idx={index} locked (-37001)")
            return last
        if code == CODE_BAD_PLANT_STATE:
            log(f"[*] farm seed idx={index} bad state (-48006)")
            return last
        log(f"[*] farm seed idx={index} type={typ} code={code} msg={resp.get('_message')}")
    return last


def _harvest_and_plant(session: GameSession, index: int, *, log: LogFn, result: dict) -> None:
    hr = farm_api.harvest(session.client, index=index)
    _raise_if_kick(hr, f"farm/harvest[{index}]")
    hcode = _code(hr)
    item = {"index": index, "code": hcode, "message": hr.get("_message")}
    if hcode in (0, None):
        log(f"[+] farm harvest idx={index} ok")
        result["harvested"].append(item)
        plant = _try_seed(session, index, log=log)
        result["planted"].append({"index": index, **plant})
    elif hcode == CODE_NOT_READY:
        log(f"[*] farm harvest idx={index} not ready")
        result["skipped"].append(item)
    else:
        log(f"[!] farm harvest idx={index} code={hcode} msg={hr.get('_message')}")
        result["errors"].append(item)


def _maybe_water_field(
    session: GameSession,
    f: dict,
    *,
    server_ms: int,
    water_stock: int,
    log: LogFn,
    result: dict,
) -> tuple[dict, int]:
    """If left < 1h and stock enough, water ceil(left/30m) times. Returns (updated_field, stock_left)."""
    idx = int(f.get("_index") or f.get("index") or 0)
    state = int(f.get("_state") or f.get("state") or 0)
    if state != STATE_GROWING:
        return f, water_stock

    left = _left_sec(f, server_ms)
    if left is None:
        return f, water_stock
    if left <= 0:
        return f, water_stock
    if left >= WATER_THRESHOLD_SEC:
        return f, water_stock

    need = max(1, int(math.ceil(left / WATER_REDUCE_SEC)))
    if water_stock < need:
        log(
            f"[*] farm water skip idx={idx} left={left:.0f}s need={need} stock={water_stock}"
        )
        result["skipped"].append(
            {
                "index": idx,
                "reason": "water_not_enough",
                "left_sec": left,
                "need": need,
                "stock": water_stock,
            }
        )
        return f, water_stock

    log(
        f"[*] farm water idx={idx} left={left:.0f}s <1h need={need} "
        f"type={WATER_TYPE_SMALL} stock={water_stock}"
    )
    wr = farm_api.watering(
        session.client,
        index=idx,
        water_type=WATER_TYPE_SMALL,
        count=need,
    )
    _raise_if_kick(wr, f"farm/watering[{idx}]")
    wcode = _code(wr)
    entry = {
        "index": idx,
        "code": wcode,
        "message": wr.get("_message"),
        "type": WATER_TYPE_SMALL,
        "count": need,
        "left_before": left,
    }
    if wcode in (0, None):
        ff = wr.get("_farmField") if isinstance(wr.get("_farmField"), dict) else {}
        end_after = int(ff.get("_endTime") or 0)
        end_before = int(f.get("_endTime") or 0)
        reduced = (end_before - end_after) / 1000.0 if end_after and end_before else need * WATER_REDUCE_SEC
        entry["reduced_sec"] = reduced
        entry["field"] = {
            k: ff.get(k)
            for k in ("_index", "_state", "_endTime", "_harvestAmount", "_durationTime")
        }
        result["watered"].append(entry)
        water_stock = max(0, water_stock - need)
        log(f"[+] farm water idx={idx} x{need} ok reduced~={reduced:.0f}s stock_left={water_stock}")
        # prefer server field snapshot
        if ff:
            return ff, water_stock
        # fallback mutate local
        f = dict(f)
        if end_after:
            f["_endTime"] = end_after
        return f, water_stock

    log(f"[!] farm water idx={idx} code={wcode} msg={wr.get('_message')}")
    result["errors"].append(entry)
    return f, water_stock


def run_farm_maintain(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
) -> dict:
    """Water near-ready plots, harvest ripe, plant empty unlocked fields."""
    result: dict = {
        "ok": False,
        "fields_before": [],
        "fields_after": [],
        "watered": [],
        "harvested": [],
        "planted": [],
        "skipped": [],
        "errors": [],
        "water_stock": {},
        "seed_stock": {},
        "seed_ads": [],
        "watering_ads": [],
        "seed_ads_claimed": 0,
        "watering_ads_claimed": 0,
        "seed_ad_remaining_before": 0,
        "watering_ad_remaining_before": 0,
        "seed_ad_remaining_after": 0,
        "watering_ad_remaining_after": 0,
    }
    if login_wall is None:
        login_wall = time.time()

    listed = farm_api.farm_list(session.client)
    _raise_if_kick(listed, "farm/list")
    if _code(listed) not in (0, None):
        result["errors"].append(
            f"farm/list code={listed.get('_code')} msg={listed.get('_message')}"
        )
        log(f"[-] farm/list failed code={listed.get('_code')} msg={listed.get('_message')}")
        return result

    info = farm_api.farm_info(session.client)
    _raise_if_kick(info, "farm/info")
    farm = info.get("_farm") if isinstance(info, dict) else None
    result["farm"] = farm
    seed_ad_left = 0
    if isinstance(farm, dict):
        seed_ad_left = _as_int(farm.get("_adCount"))
        log(
            f"[*] farm info level={farm.get('_level')} exp={farm.get('_exp')} "
            f"point={farm.get('_point')} seedAd={seed_ad_left} "
            f"adAccel={farm.get('_adAccelCount')}"
        )
    fw = info.get("_farmWatering") if isinstance(info, dict) else None
    watering_ad_left = 0
    if isinstance(fw, dict):
        watering_ad_left = _as_int(fw.get("_adCount"))
        log(
            f"[*] farm watering viewCount={fw.get('_viewCount')} "
            f"adCount={watering_ad_left}"
        )
    result["seed_ad_remaining_before"] = seed_ad_left
    result["watering_ad_remaining_before"] = watering_ad_left

    # Claim remaining ads before stock-sensitive work.
    if seed_ad_left > 0:
        log(f"[*] farm claim seed ads remaining={seed_ad_left}")
        seed_ad_left = _claim_seed_ads(
            session, remaining=seed_ad_left, log=log, result=result
        )
    else:
        log("[*] farm seed-ad remaining=0 skip")
    if watering_ad_left > 0:
        log(f"[*] farm claim watering ads remaining={watering_ad_left}")
        watering_ad_left = _claim_watering_ads(
            session, remaining=watering_ad_left, log=log, result=result
        )
    else:
        log("[*] farm watering-ad remaining=0 skip")
    result["seed_ad_remaining_after"] = seed_ad_left
    result["watering_ad_remaining_after"] = watering_ad_left

    goods = farm_api.goods_list(session.client)
    _raise_if_kick(goods, "goods/list")
    stock203 = _goods_value(goods, WATER_TYPE_SMALL) if _code(goods) in (0, None) else 0
    stock204 = _goods_value(goods, WATER_TYPE_LARGE) if _code(goods) in (0, None) else 0
    seed_stock = {
        str(t): (_goods_value(goods, t) if _code(goods) in (0, None) else 0)
        for t in SEED_TYPES
    }
    result["water_stock"] = {str(WATER_TYPE_SMALL): stock203, str(WATER_TYPE_LARGE): stock204}
    result["seed_stock"] = seed_stock
    log(
        f"[*] farm water stock can1(203)={stock203} can2(204)={stock204} "
        f"seeds={seed_stock}"
    )

    fields = _fields_from_list(listed)
    result["fields_before"] = [
        {
            k: f.get(k)
            for k in (
                "_index",
                "_state",
                "_endTime",
                "_harvestAmount",
                "_expectedAmount",
                "_harvestMonType",
            )
        }
        for f in fields
    ]
    server_ms = current_server_ms(session, login_wall=login_wall)
    log(f"[*] farm fields={len(fields)} server_ms={server_ms}")
    for f in fields:
        log(f"    {_field_summary(f, server_ms)}")

    water_stock = stock203

    for f in fields:
        idx = int(f.get("_index") or f.get("index") or 0)

        # Refresh server_ms lightly for long loops
        server_ms = current_server_ms(session, login_wall=login_wall)

        # Water if growing and <1h left and stock enough
        if int(f.get("_state") or 0) == STATE_GROWING:
            f, water_stock = _maybe_water_field(
                session,
                f,
                server_ms=server_ms,
                water_stock=water_stock,
                log=log,
                result=result,
            )
            server_ms = current_server_ms(session, login_wall=login_wall)

        # Harvest if ready (including after watering pushed endTime into past)
        if _is_harvestable(f, server_ms):
            _harvest_and_plant(session, idx, log=log, result=result)
            continue

        # Plant empty unlocked
        if _is_empty_seed(f):
            plant = _try_seed(session, idx, log=log)
            entry = {"index": idx, **plant}
            if plant.get("code") in (0, None):
                result["planted"].append(entry)
            elif plant.get("code") in (CODE_LOCKED, CODE_BAD_PLANT_STATE):
                result["skipped"].append(entry)
            else:
                result["errors"].append(entry)
            continue

        result["skipped"].append(
            {
                "index": idx,
                "reason": "in_progress",
                "state": f.get("_state"),
                "endTime": f.get("_endTime"),
                "left_sec": _left_sec(f, server_ms),
            }
        )

    listed2 = farm_api.farm_list(session.client)
    _raise_if_kick(listed2, "farm/list(after)")
    fields2 = _fields_from_list(listed2) if _code(listed2) in (0, None) else []
    result["fields_after"] = [
        {
            k: f.get(k)
            for k in (
                "_index",
                "_state",
                "_endTime",
                "_harvestAmount",
                "_expectedAmount",
                "_harvestMonType",
            )
        }
        for f in fields2
    ]
    server_ms = current_server_ms(session, login_wall=login_wall)
    for f in fields2:
        log(f"    after {_field_summary(f, server_ms)}")

    result["ok"] = True
    log(
        f"[*] farm done watered={len(result['watered'])} harvested={len(result['harvested'])} "
        f"planted={len(result['planted'])} skipped={len(result['skipped'])} "
        f"errors={len(result['errors'])} "
        f"seedAds={result['seed_ads_claimed']} waterAds={result['watering_ads_claimed']}"
    )
    return result
