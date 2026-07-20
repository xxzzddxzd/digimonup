"""Partner relation-exp (喂养/点触) flow.

Live-measured protocol (2026-07-17):

1. POST /api/partner/collect-list {}
   - returns PartnerEvolutionInfo-like entry:
     _key, _evolution, _level, _exp, _relationLevel, _relationExp,
     _claimedRelationLevel, _nextRelationExpTime

2. Ready when serverTime >= _nextRelationExpTime
   - client-side gate + server rejects early with -24016

3. POST /api/partner/relation-exp {}  (empty body)
   - success _code=0
   - body: {_partnerCollect: {... updated fields ...}}
   - observed: relationExp +5 (180 -> 185)
   - nextRelationExpTime := claimTime + 1200s (20 minutes)

4. Immediate re-call -> -24016 「파트너 인연 경험치 획득 쿨타임입니다」

5. POST /api/partner/relation-reward {"_key": partnerBaseKey}
   - _key is partner baseKey (PS_PartnerRelationReward.Request(int baseKey))
   - not relation level index
   - -24020: nothing claimable yet
   - -24008: wrong key / no collect info
   - -11006: bad params (e.g. empty body)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .apis import partner as partner_api
from .session import GameSession

TZ = timezone(timedelta(hours=8))

# Live measured
DEFAULT_COOLDOWN_SEC = 1200
EXP_GAIN_OBSERVED = 5

CODE_COOLDOWN = -24016
CODE_NO_REWARD = -24020
CODE_NO_COLLECT = -24008
CODE_BAD_PARAM = -11006


def _to_ms(ts: Any) -> Optional[int]:
    if ts is None:
        return None
    try:
        v = int(ts)
    except Exception:
        return None
    if v < 10_000_000_000:
        v *= 1000
    return v


def ms_to_str(ms: Any) -> str:
    v = _to_ms(ms)
    if v is None:
        return str(ms)
    return datetime.fromtimestamp(v / 1000.0, TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def partner_from_payload(payload: Any) -> Optional[dict]:
    """Prefer explicit _partnerCollect, else first relation-bearing entry."""
    if isinstance(payload, dict):
        pc = payload.get("_partnerCollect") or payload.get("partnerCollect")
        if isinstance(pc, dict):
            return normalize_partner(pc)
    found = extract_partner_collect(payload)
    return found[0] if found else None


def normalize_partner(o: dict) -> dict:
    key = o.get("_key", o.get("key"))
    base = o.get("_baseKey", o.get("baseKey", key))
    return {
        "key": key,
        "baseKey": base,
        "evolution": o.get("_evolution", o.get("evolution")),
        "level": o.get("_level", o.get("level")),
        "relationLevel": o.get("_relationLevel", o.get("relationLevel")),
        "relationExp": o.get("_relationExp", o.get("relationExp")),
        "claimedRelationLevel": o.get("_claimedRelationLevel", o.get("claimedRelationLevel")),
        "nextRelationExpTime": o.get("_nextRelationExpTime", o.get("nextRelationExpTime")),
        "raw": o,
    }


def extract_partner_collect(payload: Any) -> list[dict]:
    """Extract PartnerEvolutionInfo-like entries with relation fields."""
    out: list[dict] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if any(
                k in o
                for k in (
                    "_nextRelationExpTime",
                    "nextRelationExpTime",
                    "_relationExp",
                    "relationExp",
                    "_relationLevel",
                    "relationLevel",
                )
            ):
                out.append(normalize_partner(o))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(payload)
    return out


def current_server_ms(session: GameSession, *, login_wall: float | None = None) -> int:
    st = (session.login_info or {}).get("_serverTime")
    base = _to_ms(st)
    if base is None:
        return int(time.time() * 1000)
    if login_wall is None:
        return base
    return base + int((time.time() - login_wall) * 1000)


@dataclass
class CareStatus:
    partner: Optional[dict]
    server_ms: int
    ready: bool
    left_sec: float
    next_str: str

    def summary(self) -> str:
        p = self.partner or {}
        return (
            f"key={p.get('key')} baseKey={p.get('baseKey')} "
            f"relLv={p.get('relationLevel')} relExp={p.get('relationExp')} "
            f"claimed={p.get('claimedRelationLevel')} next={self.next_str} "
            f"left={self.left_sec:.1f}s ready={self.ready}"
        )


def status_from_partner(
    partner: Optional[dict], session: GameSession, *, login_wall: float | None = None
) -> CareStatus:
    server_ms = current_server_ms(session, login_wall=login_wall)
    nxt = _to_ms((partner or {}).get("nextRelationExpTime"))
    if partner is None or nxt is None:
        return CareStatus(
            partner=partner, server_ms=server_ms, ready=True, left_sec=0.0, next_str="-"
        )
    left = (nxt - server_ms) / 1000.0
    return CareStatus(
        partner=partner,
        server_ms=server_ms,
        ready=left <= 0,
        left_sec=max(0.0, left),
        next_str=ms_to_str(nxt),
    )


def get_care_status(session: GameSession, *, login_wall: float | None = None) -> CareStatus:
    cl = partner_api.collect_list(session.client)
    partner = partner_from_payload(cl)
    # collect-list may nest differently; fallback first extract
    if partner is None:
        partners = extract_partner_collect(cl)
        partner = partners[0] if partners else None
    return status_from_partner(partner, session, login_wall=login_wall)


def _public_partner(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    return {k: v for k, v in p.items() if k != "raw"}


def run_qmd(session: GameSession, *, wait_cooldown: bool = True, log=print) -> dict:
    """Full 亲密点触 flow:

    login assumed done
    collect-list -> wait nextRelationExpTime -> relation-exp
    -> measure cooldown -> optional relation-reward(baseKey)
    """
    result: dict = {"ok": False, "steps": [], "cooldown_default_sec": DEFAULT_COOLDOWN_SEC}
    login_wall = time.time()

    st0 = get_care_status(session, login_wall=login_wall)
    result["before"] = {
        "summary": st0.summary(),
        "partner": _public_partner(st0.partner),
        "server_ms": st0.server_ms,
    }
    log(f"[*] qmd before: {st0.summary()}")

    if not st0.ready and wait_cooldown:
        wait = st0.left_sec + 1.5
        log(f"[*] qmd cooling down, sleep {wait:.0f}s until {st0.next_str}")
        end = time.time() + wait
        while time.time() < end:
            left = end - time.time()
            if left <= 0:
                break
            if int(left) % 30 == 0 or left < 15:
                log(f"[*] qmd wait... {left:.0f}s")
            time.sleep(min(5.0, left))
        st0 = get_care_status(session, login_wall=login_wall)
        log(f"[*] qmd after wait: {st0.summary()}")

    if not st0.ready and not wait_cooldown:
        result["error"] = f"still cooling: left={st0.left_sec:.1f}s next={st0.next_str}"
        log(f"[-] {result['error']}")
        return result

    if not st0.ready:
        # waited but still not ready (clock skew); try once anyway
        log(f"[!] still left={st0.left_sec:.1f}s; attempting relation-exp")

    t_claim = time.time()
    server_at_claim = current_server_ms(session, login_wall=login_wall)
    before_exp = int((st0.partner or {}).get("relationExp") or 0)

    exp = partner_api.relation_exp(session.client)
    result["relation_exp"] = {
        "code": exp.get("_code"),
        "message": exp.get("_message"),
        "details": exp.get("_details"),
    }
    log(f"[*] relation-exp code={exp.get('_code')} msg={exp.get('_message')}")

    resp_partner = partner_from_payload(exp)
    st1 = get_care_status(session, login_wall=login_wall)
    # prefer response partner for immediate fields
    if resp_partner is not None and st1.partner is None:
        st1 = status_from_partner(resp_partner, session, login_wall=login_wall)
    elif resp_partner is not None:
        # merge next time from response if collect-list lags
        st1.partner = st1.partner or resp_partner

    after_exp = int((resp_partner or st1.partner or {}).get("relationExp") or before_exp)
    gained = after_exp - before_exp if exp.get("_code", 0) in (0, None) else None

    cooldown_sec = None
    nxt = _to_ms((resp_partner or st1.partner or {}).get("nextRelationExpTime"))
    if exp.get("_code", 0) in (0, None) and nxt is not None:
        cooldown_sec = (nxt - server_at_claim) / 1000.0
        log(
            f"[+] qmd claim ok; exp {before_exp}->{after_exp} (+{gained if gained is not None else '?'}); "
            f"cooldown ~= {cooldown_sec:.1f}s (until {ms_to_str(nxt)}; default {DEFAULT_COOLDOWN_SEC}s)"
        )
    elif exp.get("_code") == CODE_COOLDOWN:
        log(f"[!] still in cooldown per server (-24016); left~{st1.left_sec:.1f}s next={st1.next_str}")
    else:
        log(f"[-] relation-exp failed code={exp.get('_code')} msg={exp.get('_message')}")

    result["after"] = {
        "summary": st1.summary(),
        "partner": _public_partner(st1.partner or resp_partner),
        "response_partner": _public_partner(resp_partner),
        "server_ms": st1.server_ms,
        "exp_before": before_exp,
        "exp_after": after_exp,
        "exp_gained": gained,
    }
    result["cooldown_sec"] = cooldown_sec
    result["claim_server_ms"] = server_at_claim

    # relation-reward uses partner baseKey (not relation level)
    rewards = []
    p = resp_partner or st1.partner or st0.partner or {}
    base_key = p.get("baseKey") or p.get("key")
    rel = int(p.get("relationLevel") or 0)
    claimed = int(p.get("claimedRelationLevel") or 0)
    log(f"[*] relation reward check baseKey={base_key} relLv={rel} claimed={claimed}")
    if base_key is not None and claimed < rel:
        rr = partner_api.relation_reward(session.client, key=int(base_key))
        item = {
            "key": int(base_key),
            "code": rr.get("_code"),
            "message": rr.get("_message"),
            "partner": _public_partner(partner_from_payload(rr)),
        }
        rewards.append(item)
        code = rr.get("_code", 0)
        if code in (0, None):
            log(f"[+] relation-reward baseKey={base_key} ok")
        elif code == CODE_NO_REWARD:
            log(f"[*] relation-reward: no claimable reward yet (-24020)")
        else:
            log(f"[!] relation-reward baseKey={base_key} code={code} msg={rr.get('_message')}")
    else:
        log("[*] relation-reward skipped (no baseKey or nothing pending)")
    result["relation_rewards"] = rewards

    result["ok"] = exp.get("_code", 0) in (0, None)
    result["elapsed_sec"] = time.time() - t_claim
    return result
