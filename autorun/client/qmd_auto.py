"""One-shot maintenance: farm + dbox + qmd + afk.

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
    partner_from_payload,
    run_qmd,
    status_from_partner,
)
from .session import GameSession
from .farm_care import run_farm_maintain
from .farm_care import SessionKicked as FarmSessionKicked
from .dbox_care import run_dbox_care
from .dbox_care import SessionKicked as DboxSessionKicked

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
    cl = partner_api.collect_list(session.client)
    _raise_if_kick(cl, "collect-list")
    partner = partner_from_payload(cl)
    if partner is None:
        partners = extract_partner_collect(cl)
        partner = partners[0] if partners else None
    st = status_from_partner(partner, session, login_wall=login_wall)
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


def _run_qmd_checked(session: GameSession, *, log: LogFn) -> dict:
    care = run_qmd(session, wait_cooldown=False, log=log)
    exp = care.get("relation_exp") or {}
    if exp.get("code") == SESSION_KICK_CODE:
        raise SessionKicked("relation-exp", body=exp)
    for item in care.get("relation_rewards") or []:
        if item.get("code") == SESSION_KICK_CODE:
            raise SessionKicked("relation-reward", body=item)
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
    parts = [
        f"qmd_ok={bool(care.get('ok'))}",
        f"qmd_code={(care.get('relation_exp') or {}).get('code')}",
        f"exp={after.get('exp_before')}->{after.get('exp_after')}",
        f"gained={after.get('exp_gained')}",
        f"afk_list={(afk.get('reward_list') or {}).get('code')}",
        f"afk_reward={(afk.get('reward') or {}).get('code')}",
        f"afk_ad={(afk.get('ad_view') or {}).get('code')}",
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

    login -> farm -> dbox -> qmd(if ready, else skip) -> afk -> exit

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

                # dbox (deploy + attack lines inside)
                try:
                    dbox = run_dbox_care(session, login_wall=login_wall, log=log)
                    _append_result_log(
                        result_path,
                        result="dbox",
                        detail=(
                            f"ok={dbox.get('ok')} "
                            f"claimed={dbox.get('claimed_keys')} "
                            f"rewards={len(dbox.get('rewards') or [])} "
                            f"public={len(dbox.get('already_public') or [])}+"
                            f"{len(dbox.get('reconnected') or [])}/"
                            f"{len(dbox.get('reconnect_failed') or [])} "
                            f"wins={dbox.get('wins')} fails={dbox.get('fails')} "
                            f"eligible={dbox.get('eligible')} candidates={dbox.get('candidates')} "
                            f"ovr={dbox.get('ovr_before')}->{dbox.get('ovr_after')} "
                            f"skip={dbox.get('skipped_reason')}"
                        ),
                        log=log,
                    )
                except DboxSessionKicked as dk:
                    raise SessionKicked(dk.where, body=dk.body) from dk

                # qmd: only claim if ready; never sleep until cooldown
                st, _ = _care_status(session, login_wall=login_wall, log=log)
                if not st.ready:
                    _append_result_log(
                        result_path,
                        result="qmd_skip",
                        detail=f"cooling left={st.left_sec:.1f}s next={st.next_str}",
                        log=log,
                    )
                    log(f"[*] qmd skip (cooling) left={st.left_sec:.1f}s next={st.next_str}")
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
