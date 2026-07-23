"""Arena PVP care: regular + season.

Policy (user):
  When running pvp, consume ALL of both ticket types:
    - goods 356 PVPTicket        -> /api/arena/*
    - goods 357 PVPTicket_Season -> /api/arena-season/*
  Each mode: from matching list pick lowest _user._combat, challenge until tickets gone.
  Battle report always submits _isWin=false (force lose) by default.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .apis import arena as arena_api
from .apis import farm as farm_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
GOODS_PVP_TICKET = arena_api.GOODS_PVP_TICKET
GOODS_PVP_TICKET_SEASON = arena_api.GOODS_PVP_TICKET_SEASON

SAFETY_MAX_BATTLES = 500  # hard stop if tickets never drop
REQUEST_GAP_SEC = 0.2


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


@dataclass(frozen=True)
class PvpMode:
    name: str
    ticket_type: int
    stage: int
    pvp_type: int
    info_key: str  # response field for rank snapshot
    battle_info_key: str  # rank field on battle response


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


def _list_of(container: Any) -> list:
    if container is None:
        return []
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in ("_list", "list"):
            val = container.get(key)
            if isinstance(val, list):
                return val
    return []


def _goods_value(goods_payload: dict, goods_type: int) -> int:
    gl = goods_payload.get("_goodsList") or goods_payload.get("goodsList") or {}
    for it in _list_of(gl):
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


def _ticket_from_battle_resp(body: dict, ticket_type: int) -> Optional[int]:
    ra = body.get("_rewardAllList") if isinstance(body, dict) else None
    if not isinstance(ra, dict):
        return None
    for it in _list_of(ra.get("_goodsList") or {}):
        if not isinstance(it, dict):
            continue
        if _int(it.get("_type")) == int(ticket_type):
            try:
                return int(float(it.get("_value") or 0))
            except Exception:
                return None
    return None


def _parse_matching(body: dict) -> list[dict]:
    ml = body.get("_matchingList") or {}
    out: list[dict] = []
    for it in _list_of(ml):
        if not isinstance(it, dict):
            continue
        user = it.get("_user") if isinstance(it.get("_user"), dict) else {}
        uid = str(user.get("_uid") or it.get("_uid") or "").strip()
        if not uid:
            continue
        combat = _int(user.get("_combat"), 0)
        out.append(
            {
                "uid": uid,
                "nick": str(user.get("_nickName") or user.get("nickName") or ""),
                "combat": combat,
                "score": _int(it.get("_score") or user.get("_score"), 0),
                "level": _int(user.get("_level"), 0),
                "raw": it,
            }
        )
    out.sort(key=lambda x: (x["combat"], x["score"], x["uid"]))
    return out


def _build_battle_info(*, server_ms: int, pvp_type: int, is_win: bool = False) -> str:
    obj = {
        "_uid": str(uuid.uuid4()),
        "_pvp_type": int(pvp_type),
        "_battleStartTime": int(server_ms) - 8000,
        "_battleTime": 8.0,
        "_seed": 1,
        "_isWin": bool(is_win),
        "_isAttack": True,
        "ownerAddScore": 0,
        "otherAddScore": 0,
        "ownerScore": 0,
        "otherScore": 0,
    }
    return json.dumps(obj, separators=(",", ":"))


def _mode_regular() -> PvpMode:
    return PvpMode(
        name="arena",
        ticket_type=GOODS_PVP_TICKET,
        stage=arena_api.ARENA_STAGE_KEY,
        pvp_type=arena_api.PVP_TYPE_ARENA,
        info_key="_arena",
        battle_info_key="_arena",
    )


def _mode_season() -> PvpMode:
    return PvpMode(
        name="arena_season",
        ticket_type=GOODS_PVP_TICKET_SEASON,
        stage=arena_api.ARENA_SEASON_STAGE_KEY,
        pvp_type=arena_api.PVP_TYPE_SEASON,
        info_key="_arenaSeason",
        battle_info_key="_arenaSeason",
    )


def _fetch_info(session: GameSession, mode: PvpMode) -> dict:
    if mode.name == "arena_season":
        return arena_api.season_info(session.client)
    return arena_api.info(session.client)


def _fetch_matching(session: GameSession, mode: PvpMode) -> dict:
    if mode.name == "arena_season":
        return arena_api.season_matching(session.client, is_refresh=False)
    return arena_api.matching(session.client)


def _do_battle(
    session: GameSession,
    mode: PvpMode,
    *,
    target_uid: str,
    battle_info: str,
    is_win: bool,
) -> dict:
    if mode.name == "arena_season":
        return arena_api.season_battle(
            session.client,
            target_uid=target_uid,
            is_win=is_win,
            battle_info=battle_info,
            stage=mode.stage,
        )
    return arena_api.battle(
        session.client,
        target_uid=target_uid,
        is_win=is_win,
        battle_info=battle_info,
        stage=mode.stage,
    )


def _run_mode(
    session: GameSession,
    mode: PvpMode,
    *,
    log: LogFn,
    is_win: bool,
    load_profile: bool,
) -> dict:
    """Drain one ticket type until tickets are gone."""
    result: dict[str, Any] = {
        "mode": mode.name,
        "ticket_type": mode.ticket_type,
        "ok": False,
        "ticket_start": 0,
        "ticket_end": 0,
        "battles": 0,
        "wins": 0,
        "fails": 0,
        "skipped_reason": None,
        "info_before": None,
        "info_after": None,
        "history": [],
        "errors": [],
    }

    goods = farm_api.goods_list(session.client)
    _raise_if_kick(goods, f"goods/list[{mode.name}]")
    if _code(goods) not in (0, None):
        result["skipped_reason"] = f"goods_list_code={_code(goods)}"
        result["errors"].append(goods.get("_message") or result["skipped_reason"])
        log(f"[-] pvp[{mode.name}] goods/list fail code={_code(goods)}")
        return result

    tickets = _goods_value(goods, mode.ticket_type)
    result["ticket_start"] = tickets
    result["ticket_end"] = tickets
    log(f"[*] pvp[{mode.name}] tickets={tickets} (goods {mode.ticket_type})")

    if tickets <= 0:
        result["ok"] = True
        result["skipped_reason"] = "no_ticket"
        log(f"[*] pvp[{mode.name}] skip: no tickets")
        return result

    info = _fetch_info(session, mode)
    _raise_if_kick(info, f"{mode.name}/info")
    if _code(info) in (0, None):
        snap = info.get(mode.info_key)
        result["info_before"] = snap
        if isinstance(snap, dict):
            log(
                f"[*] pvp[{mode.name}] rank={snap.get('_rank')} "
                f"score={snap.get('_score')} tier={snap.get('_tier')} "
                f"season={snap.get('_seasonKey')} winCount={snap.get('_winCount')}"
            )
    elif _code(info) not in (0, None):
        # season may be locked; still try matching/battle if tickets exist
        log(
            f"[!] pvp[{mode.name}] info code={_code(info)} msg={info.get('_message')}"
        )

    battles = 0
    stagnant = 0
    last_ticket = tickets
    while tickets > 0 and battles < SAFETY_MAX_BATTLES:
        matching = _fetch_matching(session, mode)
        _raise_if_kick(matching, f"{mode.name}/matching")
        if _code(matching) not in (0, None):
            msg = matching.get("_message") or f"code={_code(matching)}"
            result["errors"].append(f"matching:{msg}")
            log(f"[-] pvp[{mode.name}] matching fail: {msg}")
            break

        targets = _parse_matching(matching)
        if not targets:
            result["errors"].append("matching_empty")
            log(f"[-] pvp[{mode.name}] matching list empty")
            break

        target = targets[0]
        log(
            f"[*] pvp[{mode.name}] pick lowest combat={target['combat']} "
            f"nick={target['nick']!r} score={target['score']} "
            f"lv={target['level']} ticket={tickets} pool={len(targets)}"
        )
        if load_profile:
            ul = arena_api.user_list(session.client, [target["uid"]])
            _raise_if_kick(ul, f"user/list[{mode.name}]")

        server_ms = current_server_ms(session) or int(time.time() * 1000)
        bi = _build_battle_info(
            server_ms=int(server_ms), pvp_type=mode.pvp_type, is_win=is_win
        )
        body = _do_battle(
            session,
            mode,
            target_uid=target["uid"],
            battle_info=bi,
            is_win=is_win,
        )
        _raise_if_kick(body, f"{mode.name}/battle")
        battles += 1
        code = _code(body)
        snap = body.get(mode.battle_info_key) if isinstance(body, dict) else None
        row = {
            "n": battles,
            "uid": target["uid"],
            "nick": target["nick"],
            "combat": target["combat"],
            "code": code,
            "message": body.get("_message"),
            "target_score": body.get("_targetScore"),
            "info": snap,
        }

        if code in (0, None):
            if is_win:
                result["wins"] += 1
            row["reported_win"] = bool(is_win)
            tickets_resp = _ticket_from_battle_resp(body, mode.ticket_type)
            if tickets_resp is not None:
                tickets = tickets_resp
            else:
                goods = farm_api.goods_list(session.client)
                _raise_if_kick(goods, f"goods/list[{mode.name}-loop]")
                tickets = _goods_value(goods, mode.ticket_type)
            result["ticket_end"] = tickets
            if isinstance(snap, dict):
                result["info_after"] = snap
            row["ticket_after"] = tickets
            outcome = "win" if is_win else "lose"
            log(
                f"[+] pvp[{mode.name}] {outcome} vs {target['nick']!r} "
                f"combat={target['combat']} "
                f"score={(snap or {}).get('_score')} "
                f"rank={(snap or {}).get('_rank')} "
                f"tier={(snap or {}).get('_tier')} ticket={tickets}"
            )
            if tickets >= last_ticket and tickets > 0:
                stagnant += 1
                if stagnant >= 3:
                    log(f"[-] pvp[{mode.name}] stop: ticket not decreasing after wins")
                    result["history"].append(row)
                    break
            else:
                stagnant = 0
            last_ticket = tickets
        else:
            result["fails"] += 1
            result["errors"].append(
                f"battle[{target['nick']}]:code={code} {body.get('_message')}"
            )
            log(
                f"[-] pvp[{mode.name}] battle fail vs {target['nick']!r} "
                f"code={code} msg={body.get('_message')}"
            )
            goods = farm_api.goods_list(session.client)
            _raise_if_kick(goods, f"goods/list[{mode.name}-fail]")
            tickets = _goods_value(goods, mode.ticket_type)
            result["ticket_end"] = tickets
            if tickets == last_ticket:
                stagnant += 1
            if stagnant >= 3:
                log(f"[-] pvp[{mode.name}] stop: ticket not decreasing after fails")
                result["history"].append(row)
                break
            last_ticket = tickets

        result["history"].append(row)
        result["battles"] = battles
        if REQUEST_GAP_SEC > 0:
            time.sleep(REQUEST_GAP_SEC)

    if battles >= SAFETY_MAX_BATTLES and tickets > 0:
        result["errors"].append(f"hit_safety_max={SAFETY_MAX_BATTLES}")
        log(
            f"[!] pvp[{mode.name}] hit safety max={SAFETY_MAX_BATTLES} "
            f"with tickets left={tickets}"
        )

    result["battles"] = battles
    result["ticket_end"] = tickets
    result["ok"] = result["fails"] == 0 or result["wins"] > 0
    if result["skipped_reason"] is None and tickets <= 0 and battles == 0:
        result["skipped_reason"] = "no_ticket"
    log(
        f"[*] pvp[{mode.name}] done battles={battles} wins={result['wins']} "
        f"fails={result['fails']} ticket={result['ticket_start']}->{result['ticket_end']}"
    )
    return result


def run_pvp_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    is_win: bool = False,
    load_profile: bool = False,
    do_regular: bool = True,
    do_season: bool = True,
) -> dict:
    """Challenge lowest-combat targets until BOTH ticket types are gone."""
    _ = login_wall
    modes: list[PvpMode] = []
    if do_regular:
        modes.append(_mode_regular())
    if do_season:
        modes.append(_mode_season())

    result: dict[str, Any] = {
        "ok": False,
        "battles": 0,
        "wins": 0,
        "fails": 0,
        "ticket_start": {},
        "ticket_end": {},
        "modes": {},
        "skipped_reason": None,
        "errors": [],
    }

    for mode in modes:
        sub = _run_mode(
            session,
            mode,
            log=log,
            is_win=is_win,
            load_profile=load_profile,
        )
        result["modes"][mode.name] = sub
        result["ticket_start"][str(mode.ticket_type)] = sub.get("ticket_start")
        result["ticket_end"][str(mode.ticket_type)] = sub.get("ticket_end")
        result["battles"] += int(sub.get("battles") or 0)
        result["wins"] += int(sub.get("wins") or 0)
        result["fails"] += int(sub.get("fails") or 0)
        if sub.get("errors"):
            result["errors"].extend(
                f"{mode.name}:{e}" for e in (sub.get("errors") or [])
            )
        if sub.get("skipped_reason") not in (None, "no_ticket"):
            if result["skipped_reason"] is None:
                result["skipped_reason"] = f"{mode.name}:{sub.get('skipped_reason')}"

    if result["battles"] == 0 and all(
        (result["modes"].get(m.name) or {}).get("skipped_reason") == "no_ticket"
        for m in modes
    ):
        result["ok"] = True
        result["skipped_reason"] = "no_ticket"
    else:
        result["ok"] = result["fails"] == 0 or result["wins"] > 0

    log(
        f"[*] pvp all done battles={result['battles']} wins={result['wins']} "
        f"fails={result['fails']} tickets={result['ticket_start']}->{result['ticket_end']}"
    )
    return result


def run_pvp(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict:
    """Standalone entry used by `main.py pvp` (caller already logged in)."""
    return run_pvp_care(session, log=log)
