"""One-shot maintenance: farm + lab + mine + dbox + furnace + qmd + afk.

Scheduling is external (crontab hourly). No intimacy-cooldown sleep loop.
No heartbeat. Session kick (-19006): wait then re-auth and finish once.
Each run appends result lines to logs/auto.log.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .apis import afk as afk_api
from .apis import partner as partner_api
from .partner_care import (
    extract_partner_collect,
    list_care_status,
    partner_from_payload,
    run_qmd,
)
from .session import GameSession
from .farm_care import run_farm_maintain
from .farm_care import SessionKicked as FarmSessionKicked
from .lab_care import run_lab_care
from .lab_care import SessionKicked as LabSessionKicked
from .mine_care import run_mine_care
from .mine_care import SessionKicked as MineSessionKicked
from .dbox_care import run_dbox_care
from .dbox_care import SessionKicked as DboxSessionKicked
from .item_spawner_care import run_item_spawner_care
from .item_spawner_care import SessionKicked as FurnaceSessionKicked

LogFn = Callable[[str], None]

SESSION_KICK_CODE = -19006
KICK_RECOVER_WAIT_SEC = 600.0
DEFAULT_RESULT_LOG = Path("logs") / "auto.log"


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resp_code(body: Any) -> Optional[int]:
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
    if _resp_code(body) == SESSION_KICK_CODE:
        raise SessionKicked(where, body=body)


def _append_result_log(
    path: Path,
    *,
    result: str,
    detail: str = "",
    log: LogFn | None = None,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{_now_str()} result={result}"
        if detail:
            line += f" {detail}"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if log is not None:
            log(f"[*] result log -> {path}: {result}")
    except Exception as exc:
        if log is not None:
            log(f"[!] write result log failed: {exc}")


def _login(session: GameSession, *, log: LogFn) -> float:
    session.run_login_pipeline()
    log("[+] login pipeline ok")
    return time.time()


def _care_status(session: GameSession, *, login_wall: float | None, log: LogFn):
    """Multi-partner overview: ready if *any* partner's intimacy cooldown is done."""
    cl = partner_api.collect_list(session.client)
    _raise_if_kick(cl, "collect-list")
    partners = extract_partner_collect(cl)
    if not partners:
        one = partner_from_payload(cl)
        partners = [one] if one else []
    st = list_care_status(partners, session, login_wall=login_wall)
    log(f"[*] qmd status: {st.summary()}")
    return st, cl


def _run_afk(session: GameSession, *, log: LogFn) -> dict:
    out: dict = {}

    listed = afk_api.reward_list(session.client)
    _raise_if_kick(listed, "afk/reward-list")
    out["reward_list"] = {"code": listed.get("_code"), "message": listed.get("_message")}
    log(f"[*] afk/reward-list code={listed.get('_code')} msg={listed.get('_message')}")

    obtained = afk_api.reward_obtain(session.client)
    _raise_if_kick(obtained, "afk/reward")
    out["reward"] = {"code": obtained.get("_code"), "message": obtained.get("_message")}
    log(f"[*] afk/reward code={obtained.get('_code')} msg={obtained.get('_message')}")

    ad = afk_api.ad_view(session.client)
    _raise_if_kick(ad, "afk/ad-view")
    out["ad_view"] = {"code": ad.get("_code"), "message": ad.get("_message")}
    log(f"[*] afk/ad-view code={ad.get('_code')} msg={ad.get('_message')}")
    return out


def _raise_if_kick_blob(blob: Any, where: str) -> None:
    if isinstance(blob, dict) and blob.get("code") == SESSION_KICK_CODE:
        raise SessionKicked(where, body=blob)


def _run_qmd_checked(session: GameSession, *, log: LogFn) -> dict:
    """Claim intimacy for every ready partner (change-character + relation-exp each)."""
    care = run_qmd(session, wait_cooldown=False, log=log)
    # Flat (legacy) fields
    _raise_if_kick_blob(care.get("relation_exp"), "relation-exp")
    for item in care.get("relation_rewards") or []:
        _raise_if_kick_blob(item, "relation-reward")
    # Per-partner multi results
    for row in care.get("partners") or []:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        _raise_if_kick_blob(row.get("change_character"), f"change-character[{key}]")
        _raise_if_kick_blob(row.get("relation_exp"), f"relation-exp[{key}]")
        for item in row.get("relation_rewards") or []:
            _raise_if_kick_blob(item, f"relation-reward[{key}]")
    return care


def _recover_session(
    make_session: Callable[[], GameSession],
    *,
    log: LogFn,
    http_log: bool,
    where: str,
) -> tuple[GameSession, float]:
    log(
        f"[!] recover triggered: session kick at {where}; "
        f"sleep {KICK_RECOVER_WAIT_SEC:.0f}s then re-auth"
    )
    time.sleep(KICK_RECOVER_WAIT_SEC)
    session = make_session()
    session.client.log_enabled = http_log
    login_wall = _login(session, log=log)
    return session, login_wall


def _do_qmd_and_afk(
    session: GameSession, login_wall: float | None, *, log: LogFn
) -> tuple[dict, dict, Any]:
    care = _run_qmd_checked(session, log=log)
    log(
        f"[*] auto qmd ok={care.get('ok')} "
        f"claimed={care.get('claimed')} skipped={care.get('skipped')} "
        f"failed={care.get('failed')} "
        f"code={(care.get('relation_exp') or {}).get('code')} "
        f"err={care.get('error')}"
    )
    afk = _run_afk(session, log=log)
    log(
        "[*] auto afk done codes="
        f"{[(afk.get(k) or {}).get('code') for k in ('reward_list', 'reward', 'ad_view')]}"
    )
    st2, _ = _care_status(session, login_wall=login_wall, log=log)
    return care, afk, st2


def _format_claim_detail(care: dict, afk: dict, st2: Any) -> str:
    after = care.get("after") or {}
    # Per-partner compact: key:ok/skip/fail
    per_bits = []
    for row in care.get("partners") or []:
        if not isinstance(row, dict):
            continue
        k = row.get("key")
        if row.get("skipped"):
            per_bits.append(f"{k}:skip")
        elif row.get("ok"):
            gained = row.get("exp_gained")
            per_bits.append(f"{k}:ok+{gained if gained is not None else '?'}")
        else:
            code = (row.get("relation_exp") or {}).get("code")
            per_bits.append(f"{k}:fail({code})")
    parts = [
        f"qmd_ok={bool(care.get('ok'))}",
        f"claimed={care.get('claimed')} skipped={care.get('skipped')} failed={care.get('failed')}",
        f"per=[{','.join(per_bits)}]" if per_bits else "per=[]",
        f"qmd_code={(care.get('relation_exp') or {}).get('code')}",
        f"exp={after.get('exp_before')}->{after.get('exp_after')}",
        f"gained={after.get('exp_gained')}",
        f"afk_list={(afk.get('reward_list') or {}).get('code')}",
        f"afk_reward={(afk.get('reward') or {}).get('code')}",
        f"afk_ad={(afk.get('ad_view') or {}).get('code')}",
        f"ready={getattr(st2, 'ready_count', None)}/{getattr(st2, 'total_count', None)}",
        f"next={getattr(st2, 'next_str', '-')}",
        f"left={getattr(st2, 'left_sec', None)}",
    ]
    if care.get("error"):
        parts.append(f"err={care.get('error')}")
    return " ".join(str(p) for p in parts)


def run_auto_once(
    make_session: Callable[[], GameSession],
    *,
    log: LogFn = print,
    http_log: bool = True,
    result_log_path: str | Path | None = None,
) -> int:
    """Single run (crontab-friendly):

    login -> farm -> lab -> mine -> dbox -> furnace -> qmd(if ready, else skip) -> afk -> exit

    No long sleep on intimacy cooldown. On -19006: wait 600s, re-login, finish once.
    """
    result_path = Path(result_log_path) if result_log_path else DEFAULT_RESULT_LOG
    _append_result_log(result_path, result="start", detail=f"log={result_path}", log=log)

    session = make_session()
    session.client.log_enabled = http_log
    login_wall: float | None = None
    exit_code = 1

    try:
        attempts = 0
        while attempts < 3:
            attempts += 1
            try:
                if login_wall is None:
                    login_wall = _login(session, log=log)

                # farm
                try:
                    farm = run_farm_maintain(session, login_wall=login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="farm",
                        detail=(
                            f"ok={farm.get('ok')} "
                            f"watered={len(farm.get('watered') or [])} "
                            f"harvested={len(farm.get('harvested') or [])} "
                            f"planted={len(farm.get('planted') or [])} "
                            f"skipped={len(farm.get('skipped') or [])} "
                            f"stock203={(farm.get('water_stock') or {}).get('203')}"
                        ),
                        log=log,
                    )
                except FarmSessionKicked as fk:
                    raise SessionKicked(fk.where, body=fk.body) from fk

                # lab / 训练: complete finished run, restart same key, ask camp help
                try:
                    lab = run_lab_care(session, login_wall=login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="lab",
                        detail=(
                            f"ok={lab.get('ok')} "
                            f"completed={lab.get('completed_key')} "
                            f"ran={lab.get('ran_key')} "
                            f"helped={lab.get('helped')} "
                            f"skip={lab.get('skipped_reason')} "
                            f"errors={len(lab.get('errors') or [])}"
                        ),
                        log=log,
                    )
                except LabSessionKicked as lk:
                    raise SessionKicked(lk.where, body=lk.body) from lk

                # mine / 探查: spend stamina, pick chips, distance rewards
                try:
                    mine = run_mine_care(session, login_wall=login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="mine",
                        detail=(
                            f"ok={mine.get('ok')} "
                            f"moves={mine.get('moves')} dashes={mine.get('dashes')} "
                            f"drills={mine.get('drills')} chips~={mine.get('chips')} "
                            f"stamina={mine.get('stamina_start')}->{mine.get('stamina_end')} "
                            f"distClaim={mine.get('distance_claimed')} "
                            f"skip={mine.get('skipped_reason')}"
                        ),
                        log=log,
                    )
                except MineSessionKicked as mk:
                    raise SessionKicked(mk.where, body=mk.body) from mk

                # dbox (claim + self/search deploy + attack lines inside)
                try:
                    dbox = run_dbox_care(session, login_wall=login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="dbox",
                        detail=(
                            f"ok={dbox.get('ok')} "
                            f"claimed={dbox.get('claimed_keys')} "
                            f"rewards={len(dbox.get('rewards') or [])} "
                            f"self={len(dbox.get('already_self') or [])}+"
                            f"{len(dbox.get('connected_self') or [])} "
                            f"other={len(dbox.get('already_other') or [])}+"
                            f"{len(dbox.get('connected_other') or [])} "
                            f"publicLeft={len(dbox.get('already_public') or [])} "
                            f"freeSearch={dbox.get('free_for_search')} "
                            f"searchPool={len(dbox.get('search_pool') or [])} "
                            f"rounds={dbox.get('search_rounds')} "
                            f"fail={len(dbox.get('reconnect_failed') or [])} "
                            f"wins={dbox.get('wins')} fails={dbox.get('fails')} "
                            f"eligible={dbox.get('eligible')} candidates={dbox.get('candidates')} "
                            f"ovr={dbox.get('ovr_before')}->{dbox.get('ovr_after')} "
                            f"skip={dbox.get('skipped_reason')}"
                        ),
                        log=log,
                    )
                except DboxSessionKicked as dk:
                    raise SessionKicked(dk.where, body=dk.body) from dk

                # furnace / 装备炉: complete -> add-gold -> level-up (no open equip)
                try:
                    furnace = run_item_spawner_care(
                        session, login_wall=login_wall, log=log
                    )
                    before = furnace.get("before") or {}
                    after = furnace.get("after") or {}
                    _append_result_log(
                        result_path,
                        result="furnace",
                        detail=(
                            f"ok={furnace.get('ok')} "
                            f"lv={before.get('level')}->{after.get('level')} "
                            f"status={after.get('status_name')} "
                            f"deposit={after.get('count')}/{after.get('deposits_needed')} "
                            f"added={furnace.get('deposits')} "
                            f"completed={furnace.get('completed')} "
                            f"leveled={furnace.get('leveled_up')} "
                            f"bit_remain={after.get('bit_remain_for_level')} "
                            f"skip={furnace.get('skipped_reason')}"
                        ),
                        log=log,
                    )
                except FurnaceSessionKicked as fk:
                    raise SessionKicked(fk.where, body=fk.body) from fk

                # qmd: claim every partner that is ready; never sleep until cooldown
                st, _ = _care_status(session, login_wall=login_wall, log=log)
                if not st.ready:
                    _append_result_log(
                        result_path,
                        result="qmd_skip",
                        detail=(
                            f"all_cooling ready=0/{st.total_count} "
                            f"left={st.left_sec:.1f}s next={st.next_str}"
                        ),
                        log=log,
                    )
                    log(
                        f"[*] qmd skip (all cooling) ready=0/{st.total_count} "
                        f"left={st.left_sec:.1f}s next={st.next_str}"
                    )
                    afk = _run_afk(session, log=log)
                    _append_result_log(
                        result_path,
                        result="afk",
                        detail=(
                            f"list={(afk.get('reward_list') or {}).get('code')} "
                            f"reward={(afk.get('reward') or {}).get('code')} "
                            f"ad={(afk.get('ad_view') or {}).get('code')}"
                        ),
                        log=log,
                    )
                else:
                    care, afk, st2 = _do_qmd_and_afk(session, login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="ok" if care.get("ok") else "qmd_fail",
                        detail=_format_claim_detail(care, afk, st2),
                        log=log,
                    )

                _append_result_log(result_path, result="done", log=log)
                log("[+] auto done")
                exit_code = 0
                break

            except SessionKicked as kick:
                _append_result_log(
                    result_path,
                    result="kicked",
                    detail=f"where={kick.where} wait={KICK_RECOVER_WAIT_SEC:.0f}s attempt={attempts}",
                    log=log,
                )
                session, login_wall = _recover_session(
                    make_session, log=log, http_log=http_log, where=kick.where
                )
                continue
            except Exception as exc:
                log(f"[-] auto error: {exc}")
                log(traceback.format_exc())
                msg = str(exc)
                wait = KICK_RECOVER_WAIT_SEC if "19006" in msg else 30.0
                _append_result_log(
                    result_path,
                    result="error",
                    detail=f"{type(exc).__name__}: {exc} wait={wait:.0f}s",
                    log=log,
                )
                if "19006" in msg and attempts < 3:
                    time.sleep(wait)
                    session = make_session()
                    session.client.log_enabled = http_log
                    login_wall = None
                    continue
                break

    except KeyboardInterrupt:
        _append_result_log(result_path, result="stopped", detail="KeyboardInterrupt", log=log)
        log("[*] auto stopped by user")
        exit_code = 130

    return exit_code


# backward-compatible alias
run_qmdauto_loop = run_auto_once
