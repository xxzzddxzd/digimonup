"""Partner relation-exp (喂养/点触) flow — per partner.

Live-measured protocol:

1. POST /api/partner/collect-list {}
   - returns list of PartnerEvolutionInfo-like entries (each with own cooldown):
     _key, _evolution, _level, _exp, _relationLevel, _relationExp,
     _claimedRelationLevel, _nextRelationExpTime

2. Each partner is independent: ready when serverTime >= that partner's
   _nextRelationExpTime. Early claim -> -24016.

3. To claim partner N when not the active slot (capture 2026-07-20):
   POST /api/partner/change-character {"_key": partnerKey}
   then POST /api/partner/relation-exp {}  (empty; applies to current)

4. Immediate re-call on same partner -> -24016
   nextRelationExpTime := claimTime + ~1200s (20 minutes)

5. POST /api/partner/relation-reward {"_key": partnerBaseKey}
   - _key is partner baseKey / collect key (PS_PartnerRelationReward)
   - -24020: nothing claimable yet
   - -24008: wrong key / no collect info
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
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
    """Extract PartnerEvolutionInfo-like entries with relation fields (deduped by key)."""
    out: list[dict] = []
    seen: set[str] = set()

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
                p = normalize_partner(o)
                kid = p.get("key")
                # Prefer stable identity; fall back to object id for key-less rows.
                sid = str(kid) if kid is not None else f"id:{id(o)}"
                if sid not in seen:
                    seen.add(sid)
                    out.append(p)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(payload)
    # Stable order by key for logs / cron diffs.
    def _sort_key(p: dict) -> tuple:
        k = p.get("key")
        try:
            return (0, int(k))
        except Exception:
            return (1, str(k))

    out.sort(key=_sort_key)
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
    """Status for a single partner (or aggregate when multi is folded into one)."""

    partner: Optional[dict]
    server_ms: int
    ready: bool
    left_sec: float
    next_str: str
    # Multi-partner overview fields (filled by get_care_status / list_care_status).
    partners: list[dict] = field(default_factory=list)
    statuses: list["CareStatus"] = field(default_factory=list)
    ready_count: int = 0
    total_count: int = 0

    def summary(self) -> str:
        if self.total_count > 1 or self.statuses:
            bits = [
                f"n={self.total_count or len(self.statuses)}",
                f"ready={self.ready_count}",
                f"next={self.next_str}",
                f"left={self.left_sec:.1f}s",
                f"any_ready={self.ready}",
            ]
            for st in self.statuses:
                p = st.partner or {}
                bits.append(
                    f"[key={p.get('key')} relLv={p.get('relationLevel')} "
                    f"relExp={p.get('relationExp')} left={st.left_sec:.1f}s ready={st.ready}]"
                )
            return " ".join(bits)
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
            partner=partner,
            server_ms=server_ms,
            ready=True,
            left_sec=0.0,
            next_str="-",
            partners=[partner] if partner else [],
            ready_count=1 if partner else 0,
            total_count=1 if partner else 0,
        )
    left = (nxt - server_ms) / 1000.0
    return CareStatus(
        partner=partner,
        server_ms=server_ms,
        ready=left <= 0,
        left_sec=max(0.0, left),
        next_str=ms_to_str(nxt),
        partners=[partner],
        ready_count=1 if left <= 0 else 0,
        total_count=1,
    )


def list_care_status(
    partners: list[dict], session: GameSession, *, login_wall: float | None = None
) -> CareStatus:
    """Aggregate status: ready if *any* partner can claim."""
    server_ms = current_server_ms(session, login_wall=login_wall)
    statuses = [
        status_from_partner(p, session, login_wall=login_wall) for p in partners
    ]
    ready_list = [s for s in statuses if s.ready]
    # Earliest next claim among cooling partners (or among all if none ready).
    cooling = [s for s in statuses if not s.ready]
    if ready_list:
        next_str = "ready"
        left_sec = 0.0
        primary = ready_list[0]
    elif cooling:
        soonest = min(cooling, key=lambda s: s.left_sec)
        next_str = soonest.next_str
        left_sec = soonest.left_sec
        primary = soonest
    else:
        next_str = "-"
        left_sec = 0.0
        primary = CareStatus(
            partner=None, server_ms=server_ms, ready=False, left_sec=0.0, next_str="-"
        )

    return CareStatus(
        partner=primary.partner,
        server_ms=server_ms,
        ready=bool(ready_list),
        left_sec=left_sec,
        next_str=next_str,
        partners=list(partners),
        statuses=statuses,
        ready_count=len(ready_list),
        total_count=len(partners),
    )


def get_care_status(session: GameSession, *, login_wall: float | None = None) -> CareStatus:
    cl = partner_api.collect_list(session.client)
    partners = extract_partner_collect(cl)
    if not partners:
        # fallback single _partnerCollect shape
        one = partner_from_payload(cl)
        partners = [one] if one else []
    return list_care_status(partners, session, login_wall=login_wall)


def _public_partner(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    return {k: v for k, v in p.items() if k != "raw"}


def _partner_switch_key(partner: dict) -> Optional[int]:
    """Key for change-character: PartnerEvolutionInfoParam._key (ObscuredKey)."""
    for field_name in ("key", "baseKey"):
        v = partner.get(field_name)
        if v is None:
            continue
        try:
            return int(v)
        except Exception:
            continue
    return None


def _claim_one_partner(
    session: GameSession,
    partner: dict,
    *,
    login_wall: float | None,
    log,
    switch: bool,
) -> dict:
    """change-character (optional) -> relation-exp -> relation-reward for one partner."""
    item: dict = {
        "ok": False,
        "partner": _public_partner(partner),
        "key": partner.get("key"),
        "baseKey": partner.get("baseKey"),
    }
    switch_key = _partner_switch_key(partner)
    if switch_key is None:
        item["error"] = "no partner key for change-character"
        log(f"[-] qmd partner skip: {item['error']}")
        return item

    before_exp = int(partner.get("relationExp") or 0)
    item["exp_before"] = before_exp

    if switch:
        ch = partner_api.change_character(session.client, key=switch_key)
        item["change_character"] = {
            "code": ch.get("_code"),
            "message": ch.get("_message"),
            "key": switch_key,
        }
        log(
            f"[*] change-character key={switch_key} "
            f"code={ch.get('_code')} msg={ch.get('_message')}"
        )
        code = ch.get("_code", 0)
        if code not in (0, None):
            item["error"] = f"change-character code={code}"
            return item

    t_claim = time.time()
    server_at_claim = current_server_ms(session, login_wall=login_wall)
    exp = partner_api.relation_exp(session.client)
    item["relation_exp"] = {
        "code": exp.get("_code"),
        "message": exp.get("_message"),
        "details": exp.get("_details"),
    }
    log(
        f"[*] relation-exp key={switch_key} "
        f"code={exp.get('_code')} msg={exp.get('_message')}"
    )

    resp_partner = partner_from_payload(exp)
    after_exp = int((resp_partner or partner).get("relationExp") or before_exp)
    gained = after_exp - before_exp if exp.get("_code", 0) in (0, None) else None
    item["exp_after"] = after_exp
    item["exp_gained"] = gained
    item["response_partner"] = _public_partner(resp_partner)

    cooldown_sec = None
    nxt = _to_ms((resp_partner or partner).get("nextRelationExpTime"))
    if exp.get("_code", 0) in (0, None) and nxt is not None:
        cooldown_sec = (nxt - server_at_claim) / 1000.0
        log(
            f"[+] qmd claim ok key={switch_key}; exp {before_exp}->{after_exp} "
            f"(+{gained if gained is not None else '?'}); "
            f"cooldown ~= {cooldown_sec:.1f}s (until {ms_to_str(nxt)})"
        )
    elif exp.get("_code") == CODE_COOLDOWN:
        log(f"[!] key={switch_key} still in cooldown (-24016)")
    else:
        log(
            f"[-] relation-exp failed key={switch_key} "
            f"code={exp.get('_code')} msg={exp.get('_message')}"
        )
    item["cooldown_sec"] = cooldown_sec
    item["claim_server_ms"] = server_at_claim
    item["elapsed_sec"] = time.time() - t_claim

    # relation-reward uses partner baseKey (not relation level)
    rewards = []
    p = resp_partner or partner
    base_key = p.get("baseKey") or p.get("key") or switch_key
    rel = int(p.get("relationLevel") or 0)
    claimed = int(p.get("claimedRelationLevel") or 0)
    log(f"[*] relation reward check key={switch_key} baseKey={base_key} relLv={rel} claimed={claimed}")
    if base_key is not None and claimed < rel:
        rr = partner_api.relation_reward(session.client, key=int(base_key))
        ritem = {
            "key": int(base_key),
            "code": rr.get("_code"),
            "message": rr.get("_message"),
            "partner": _public_partner(partner_from_payload(rr)),
        }
        rewards.append(ritem)
        code = rr.get("_code", 0)
        if code in (0, None):
            log(f"[+] relation-reward baseKey={base_key} ok")
        elif code == CODE_NO_REWARD:
            log(f"[*] relation-reward: no claimable reward yet (-24020)")
        else:
            log(f"[!] relation-reward baseKey={base_key} code={code} msg={rr.get('_message')}")
    else:
        log(f"[*] relation-reward skipped key={switch_key} (no baseKey or nothing pending)")
    item["relation_rewards"] = rewards

    item["ok"] = exp.get("_code", 0) in (0, None)
    # keep first-partner flat fields for older log parsers
    item["after_partner"] = _public_partner(resp_partner or p)
    return item


def run_qmd(session: GameSession, *, wait_cooldown: bool = True, log=print) -> dict:
    """亲密点触 for *all* partners.

    Flow:
      collect-list
      -> for each partner with independent nextRelationExpTime:
           if ready (or wait_cooldown waits for earliest):
             change-character(_key) -> relation-exp -> optional relation-reward
      auto path uses wait_cooldown=False and only claims ready partners.
    """
    result: dict = {
        "ok": False,
        "steps": [],
        "cooldown_default_sec": DEFAULT_COOLDOWN_SEC,
        "partners": [],
        "claimed": 0,
        "skipped": 0,
        "failed": 0,
    }
    login_wall = time.time()

    st0 = get_care_status(session, login_wall=login_wall)
    result["before"] = {
        "summary": st0.summary(),
        "partner": _public_partner(st0.partner),
        "partners": [_public_partner(p) for p in st0.partners],
        "ready_count": st0.ready_count,
        "total_count": st0.total_count,
        "server_ms": st0.server_ms,
    }
    log(f"[*] qmd before: {st0.summary()}")

    if not st0.partners:
        result["error"] = "no partners in collect-list"
        log(f"[-] {result['error']}")
        return result

    if not st0.ready and wait_cooldown:
        wait = st0.left_sec + 1.5
        log(f"[*] qmd cooling down (all partners), sleep {wait:.0f}s until {st0.next_str}")
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
        result["error"] = (
            f"all partners cooling: ready=0/{st0.total_count} "
            f"left={st0.left_sec:.1f}s next={st0.next_str}"
        )
        log(f"[-] {result['error']}")
        result["skipped"] = st0.total_count
        # Preserve multi fields so auto logging still shows n partners.
        result["after"] = {
            "summary": st0.summary(),
            "partners": [_public_partner(p) for p in st0.partners],
            "ready_count": st0.ready_count,
            "total_count": st0.total_count,
        }
        # Backward-compat single-partner fields
        result["relation_exp"] = {"code": None, "message": "all cooling"}
        result["relation_rewards"] = []
        return result

    # Always switch when multiple partners so relation-exp hits the intended one.
    # With a single partner, skip change-character (already active).
    multi = len(st0.partners) > 1
    per: list[dict] = []
    claimed = 0
    skipped = 0
    failed = 0

    # Refresh partner rows after each successful claim so cooldowns stay accurate
    # when the same collect-list snapshot is reused for remaining keys.
    live_by_key: dict[str, dict] = {
        str(p.get("key")): p for p in st0.partners if p.get("key") is not None
    }

    for idx, partner in enumerate(st0.partners):
        key = partner.get("key")
        live = live_by_key.get(str(key), partner) if key is not None else partner
        st = status_from_partner(live, session, login_wall=login_wall)
        if not st.ready:
            if wait_cooldown and not st0.ready:
                # Already waited once for earliest; still not ready -> try once.
                log(f"[!] key={key} still left={st.left_sec:.1f}s; attempting claim")
            else:
                skipped += 1
                row = {
                    "ok": False,
                    "skipped": True,
                    "reason": "cooling",
                    "key": key,
                    "baseKey": live.get("baseKey"),
                    "left_sec": st.left_sec,
                    "next_str": st.next_str,
                    "partner": _public_partner(live),
                }
                per.append(row)
                log(
                    f"[*] qmd skip key={key} cooling left={st.left_sec:.1f}s next={st.next_str}"
                )
                continue

        # Switch when multi, or when not the first claim in this run after a previous switch.
        do_switch = multi or idx > 0
        row = _claim_one_partner(
            session,
            live,
            login_wall=login_wall,
            log=log,
            switch=do_switch,
        )
        per.append(row)
        if row.get("ok"):
            claimed += 1
            # Update local snapshot for this key from response if present.
            if row.get("response_partner") and key is not None:
                # response_partner is public (no raw); merge into live template.
                merged = dict(live)
                for k, v in (row.get("response_partner") or {}).items():
                    if v is not None:
                        merged[k] = v
                live_by_key[str(key)] = merged
        else:
            failed += 1
            # Stop hard on session kick codes so auto can recover.
            for blob in (
                row.get("change_character"),
                row.get("relation_exp"),
                *list(row.get("relation_rewards") or []),
            ):
                if isinstance(blob, dict) and blob.get("code") == -19006:
                    result["partners"] = per
                    result["claimed"] = claimed
                    result["skipped"] = skipped
                    result["failed"] = failed
                    result["relation_exp"] = row.get("relation_exp") or {}
                    result["relation_rewards"] = row.get("relation_rewards") or []
                    result["error"] = f"session kick at partner key={key}"
                    return result

    result["partners"] = per
    result["claimed"] = claimed
    result["skipped"] = skipped
    result["failed"] = failed

    # Final overview
    st1 = get_care_status(session, login_wall=login_wall)
    # Aggregate exp from per-partner rows for log compatibility
    exp_before_sum = sum(int(r.get("exp_before") or 0) for r in per if not r.get("skipped"))
    exp_after_sum = sum(
        int(r.get("exp_after") or r.get("exp_before") or 0) for r in per if not r.get("skipped")
    )
    first_ok = next((r for r in per if r.get("ok")), None)
    first_attempt = next((r for r in per if not r.get("skipped")), None) or (per[0] if per else {})

    result["after"] = {
        "summary": st1.summary(),
        "partner": _public_partner(st1.partner),
        "partners": [_public_partner(p) for p in st1.partners],
        "ready_count": st1.ready_count,
        "total_count": st1.total_count,
        "server_ms": st1.server_ms,
        "exp_before": exp_before_sum if claimed or failed else None,
        "exp_after": exp_after_sum if claimed or failed else None,
        "exp_gained": (exp_after_sum - exp_before_sum) if claimed else None,
    }
    # Flat fields for older qmd_auto parsers (first attempted / first success).
    src = first_ok or first_attempt or {}
    result["relation_exp"] = src.get("relation_exp") or {"code": None, "message": "no claim"}
    result["relation_rewards"] = []
    for r in per:
        result["relation_rewards"].extend(r.get("relation_rewards") or [])
    result["cooldown_sec"] = src.get("cooldown_sec")
    result["claim_server_ms"] = src.get("claim_server_ms")
    # ok only if at least one partner claimed; pure-skip / all-fail is False
    # (auto already gates on any-ready before calling run_qmd).
    result["ok"] = claimed > 0

    log(
        f"[*] qmd done claimed={claimed} skipped={skipped} failed={failed} "
        f"of {st0.total_count}; after: {st1.summary()}"
    )
    return result
