"""Long-running qmd + afk loop driven by server nextRelationExpTime.

No heartbeat in this flow. Session kick (-19006) from API responses:
wait, full re-login, then finish qmd+afk.

Each attempt appends one local result line to logs/qmdauto.log.
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
    DEFAULT_COOLDOWN_SEC,
    extract_partner_collect,
    partner_from_payload,
    run_qmd,
    status_from_partner,
)
from .session import GameSession
from .farm_care import run_farm_maintain
from .farm_care import SessionKicked as FarmSessionKicked

LogFn = Callable[[str], None]

SESSION_KICK_CODE = -19006
KICK_RECOVER_WAIT_SEC = 600.0  # same policy as runloop
DEFAULT_RESULT_LOG = Path("logs") / "qmdauto.log"


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
    cycle: int,
    result: str,
    detail: str = "",
    log: LogFn | None = None,
) -> None:
    """Append one local result line: date time + cycle + result + detail."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{_now_str()} cycle={cycle} result={result}"
        if detail:
            line += f" {detail}"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if log is not None:
            log(f"[*] result log -> {path}: {result}")
    except Exception as exc:
        if log is not None:
            log(f"[!] write result log failed: {exc}")


def _sleep_until(left_sec: float, *, label: str, log: LogFn, extra_buffer: float = 2.0) -> None:
    """Sleep left_sec with periodic logs. Uses wall clock end time (no drift from work)."""
    wait = max(0.0, float(left_sec)) + float(extra_buffer)
    if wait <= 0:
        return
    end = time.time() + wait
    log(f"[*] qmdauto sleep {wait:.0f}s ({label})")
    while True:
        left = end - time.time()
        if left <= 0:
            break
        if left < 15 or int(left) % 60 < 5:
            log(f"[*] qmdauto wait... {left:.0f}s")
        time.sleep(min(5.0, left))
    log("[*] qmdauto wake")


def _login(session: GameSession, *, log: LogFn) -> float:
    """Full login pipeline. Returns login wall time for server clock offset."""
    session.run_login_pipeline()
    log("[+] login pipeline ok")
    return time.time()


def _care_status(session: GameSession, *, login_wall: float | None, log: LogFn):
    """collect-list + status; raise SessionKicked on -19006."""
    cl = partner_api.collect_list(session.client)
    _raise_if_kick(cl, "collect-list")
    partner = partner_from_payload(cl)
    if partner is None:
        partners = extract_partner_collect(cl)
        partner = partners[0] if partners else None
    st = status_from_partner(partner, session, login_wall=login_wall)
    log(f"[*] qmdauto status: {st.summary()}")
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
        f"[!] session kick detected ({where}); "
        f"sleep {KICK_RECOVER_WAIT_SEC:.0f}s then full re-auth and finish flow"
    )
    _sleep_until(KICK_RECOVER_WAIT_SEC, label="kick recover", log=log, extra_buffer=0.0)
    session = make_session()
    session.client.log_enabled = http_log
    login_wall = _login(session, log=log)
    return session, login_wall


def _do_qmd_and_afk(
    session: GameSession,
    login_wall: float,
    *,
    log: LogFn,
) -> tuple[dict, dict, Any]:
    """Execute claim path when ready. Returns (care, afk, next_status)."""
    care = _run_qmd_checked(session, log=log)
    log(
        f"[*] qmdauto qmd ok={care.get('ok')} "
        f"cooldown={care.get('cooldown_sec')} err={care.get('error')}"
    )
    time.sleep(1.0)
    afk = _run_afk(session, log=log)
    log(
        f"[*] qmdauto afk done codes="
        f"{[afk.get(k, {}).get('code') for k in ('reward_list', 'reward', 'ad_view')]}"
    )
    st2, _ = _care_status(session, login_wall=login_wall, log=log)
    log(f"[*] qmdauto next: {st2.summary()}")
    return care, afk, st2


def _format_claim_detail(care: dict, afk: dict, st2: Any) -> str:
    after = (care.get("after") or {}) if isinstance(care, dict) else {}
    parts = [
        f"qmd_ok={bool(care.get('ok'))}",
        f"qmd_code={(care.get('relation_exp') or {}).get('code')}",
        f"exp={after.get('exp_before')}->{after.get('exp_after')}",
        f"gained={after.get('exp_gained')}",
        f"cooldown={care.get('cooldown_sec')}",
        f"afk_list={(afk.get('reward_list') or {}).get('code')}",
        f"afk_reward={(afk.get('reward') or {}).get('code')}",
        f"afk_ad={(afk.get('ad_view') or {}).get('code')}",
        f"next={getattr(st2, 'next_str', '-')}",
        f"left={getattr(st2, 'left_sec', None)}",
    ]
    if care.get("error"):
        parts.append(f"err={care.get('error')}")
    return " ".join(str(p) for p in parts)


def run_qmdauto_loop(
    make_session: Callable[[], GameSession],
    *,
    log: LogFn = print,
    http_log: bool = True,
    result_log_path: str | Path | None = None,
) -> int:
    """Infinite loop (no heartbeat):

    1) login -> query nextRelationExpTime
    2) if cooling: sleep until next (offline)
    3) re-login -> qmd + afk
    4) query next again -> sleep -> loop

    On -19006: wait 600s, full re-login, finish qmd+afk.

    Local result log (date/time + result) appends to logs/qmdauto.log.
    """
    result_path = Path(result_log_path) if result_log_path else DEFAULT_RESULT_LOG
    _append_result_log(
        result_path,
        cycle=0,
        result="start",
        detail=f"log={result_path}",
        log=log,
    )

    cycle = 0
    try:
        while True:
            cycle += 1
            log(f"===== qmdauto cycle={cycle} =====")
            session = make_session()
            session.client.log_enabled = http_log
            login_wall: float | None = None
            try:
                login_wall = _login(session, log=log)

                while True:
                    try:
                        # 肉田: harvest ripe + plant empty unlocked plots
                        try:
                            farm = run_farm_maintain(session, login_wall=login_wall, log=log)
                            _append_result_log(
                                result_path,
                                cycle=cycle,
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

                        st, _ = _care_status(session, login_wall=login_wall, log=log)

                        if not st.ready:
                            _append_result_log(
                                result_path,
                                cycle=cycle,
                                result="cooling",
                                detail=f"left={st.left_sec:.1f}s next={st.next_str}",
                                log=log,
                            )
                            _sleep_until(st.left_sec, label=f"until {st.next_str}", log=log)
                            break  # outer cycle: fresh login near ready time

                        care, afk, st2 = _do_qmd_and_afk(session, login_wall, log=log)
                        detail = _format_claim_detail(care, afk, st2)
                        _append_result_log(
                            result_path,
                            cycle=cycle,
                            result="ok" if care.get("ok") else "qmd_fail",
                            detail=detail,
                            log=log,
                        )

                        if st2.ready and not care.get("ok"):
                            log("[!] ready but qmd failed; sleep 60s then retry")
                            _sleep_until(60.0, label="retry backoff", log=log, extra_buffer=0.0)
                            session = make_session()
                            session.client.log_enabled = http_log
                            login_wall = _login(session, log=log)
                            continue

                        left = st2.left_sec
                        if left <= 0:
                            left = float(care.get("cooldown_sec") or DEFAULT_COOLDOWN_SEC)
                            log(f"[*] qmdauto fallback sleep {left:.0f}s")
                        _sleep_until(left, label=f"until {st2.next_str}", log=log)
                        break
                    except SessionKicked as kick:
                        _append_result_log(
                            result_path,
                            cycle=cycle,
                            result="kicked",
                            detail=f"where={kick.where} wait={KICK_RECOVER_WAIT_SEC:.0f}s",
                            log=log,
                        )
                        session, login_wall = _recover_session(
                            make_session,
                            log=log,
                            http_log=http_log,
                            where=kick.where,
                        )
                        continue
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"[-] qmdauto cycle error: {exc}")
                log(traceback.format_exc())
                msg = str(exc)
                wait = KICK_RECOVER_WAIT_SEC if "19006" in msg else 60.0
                _append_result_log(
                    result_path,
                    cycle=cycle,
                    result="error",
                    detail=f"err={exc} wait={wait:.0f}s",
                    log=log,
                )
                _sleep_until(wait, label=f"error recover ({wait:.0f}s)", log=log, extra_buffer=0.0)
    except KeyboardInterrupt:
        _append_result_log(
            result_path,
            cycle=cycle,
            result="stopped",
            detail="KeyboardInterrupt",
            log=log,
        )
        log("[*] qmdauto stopped by user")
        return 0
