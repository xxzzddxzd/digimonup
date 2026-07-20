"""异次元 box maintain: attack weaker long-connected public-box targets.

Rules (user):
  Deploy line and attack line are independent.
  Attack (always evaluated, even if not deployed):
  - only while A.OVR (_attackOverloadValue) < 25
  - target level <= my_level - 5
  - connected duration >= 30 minutes (else not attackable)
  - attack longest-connected first

Collect (private red-bang / 领取):
  info._mySupporterCharacterDimBoxInfoList[*]._rewardIntervalCount > 0
  => POST /api/dimensional-box/device-disconnect {"_keys":[...]}  (claims + unplaces)
  then ensure supporters stay on public boxes (empty slot if unplaced/kicked)
  Never withdraw a public placement except to claim (rewardIntervalCount>0).

Protocol (live):
  info:        POST /api/dimensional-box/info {}
  public-info: POST /api/dimensional-box/public-info {"_ownerUID":"0".."4"}
  device-info: POST /api/dimensional-box/device-info {"_targetUid","_key"}
  battle:      POST /api/dimensional-box/battle
               _attackReqUID = device._uid (required non-empty)
               _damage / _attackerReceivedDamage = integer digit strings only
               _battleInfo non-empty (client combat dump; minimal JSON accepted)
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional

from .apis import dbox as dbox_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
PUBLIC_BOX_IDS = ("0", "1", "2", "3", "4")
MIN_CONNECT_SEC = 30 * 60
LEVEL_GAP = 5
OVR_MAX = 25  # attack only while A.OVR < 25 (independent of deploy)
BATTLE_GAP_SEC = 1.0


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


def _list_of(container: Any) -> list[dict]:
    if isinstance(container, list):
        return [x for x in container if isinstance(x, dict)]
    if isinstance(container, dict):
        lst = container.get("_list") or container.get("list") or []
        if isinstance(lst, list):
            return [x for x in lst if isinstance(x, dict)]
    return []


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


def _digits(v: Any) -> str:
    """Server requires digit-only for damage fields."""
    try:
        n = int(float(v))
    except Exception:
        n = 0
    if n < 0:
        n = 0
    return str(n)


def extract_me(info_body: dict) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Return (uid, level, nick) from dimensional-box/info."""
    for c in _list_of(info_body.get("_connectedDeviceList")):
        u = ((c.get("_user") or {}).get("_user") or {}) if isinstance(c.get("_user"), dict) else {}
        uid = u.get("_uid")
        if uid:
            return str(uid), _int(u.get("_level"), 0) or None, u.get("_nickName")
    # fallback: private device ownerId
    for d in _list_of(info_body.get("_deviceList")):
        oid = d.get("_ownerId")
        if oid and str(oid) not in PUBLIC_BOX_IDS:
            return str(oid), None, None
    return None, None, None


def _collect_public_targets(
    session: GameSession,
    *,
    my_uid: str,
    server_ms: int,
    log: LogFn,
) -> list[dict]:
    out: list[dict] = []
    for box in PUBLIC_BOX_IDS:
        pub = dbox_api.public_info(session.client, box)
        _raise_if_kick(pub, f"dimensional-box/public-info[{box}]")
        if _code(pub) not in (0, None):
            log(
                f"[*] dbox public-info box={box} code={_code(pub)} msg={pub.get('_message')}"
            )
            continue
        devices = _list_of(pub.get("_deviceList"))
        sups = {
            s.get("_dimBoxDeviceUID"): s
            for s in _list_of(pub.get("_supporterCharacterDimBoxInfoList"))
        }
        for d in devices:
            tid = str(d.get("_targetUserId") or "")
            if not tid or tid == my_uid:
                continue
            if d.get("_isAttacked"):
                continue
            sat = _int(d.get("_safetyTimeAt"), 0)
            if sat and sat > server_ms:
                continue
            device_uid = d.get("_uid")
            if not device_uid:
                continue
            s = sups.get(device_uid) or {}
            start = _int(s.get("_startTime"), 0)
            if not start:
                continue
            connect_sec = (server_ms - start) / 1000.0
            if connect_sec < MIN_CONNECT_SEC:
                continue
            out.append(
                {
                    "box": str(d.get("_ownerId") or box),
                    "device_uid": str(device_uid),
                    "key": _int(d.get("_key"), 0),
                    "equip_index": _int(d.get("_equipIndex"), 0),
                    "target_uid": tid,
                    "is_public": bool(d.get("_isPublic", True)),
                    "connect_sec": connect_sec,
                    "start_time": start,
                    "supporter_uid": s.get("_uid"),
                }
            )
    out.sort(key=lambda x: float(x.get("connect_sec") or 0), reverse=True)
    return out


def _build_battle_info(
    *,
    ovr: int,
    other_overload: int,
    max_hp: int,
    damage: str,
    other_start_hp: float,
    server_ms: int,
) -> str:
    # Minimal payload accepted by live server (full client dump is much larger).
    obj = {
        "_uid": str(uuid.uuid4()),
        "_pvp_type": 0,
        "_battleStartTime": int(server_ms) - 8000,
        "_battleTime": 8.0,
        "_seed": 1,
        "_isWin": True,
        "_isAttack": True,
        "ownerAddScore": 0,
        "otherAddScore": 0,
        "ownerScore": 0,
        "otherScore": 0,
        "_ownerOverload": int(ovr),
        "_ownerBattleOverload": 0,
        "_ownerMaxHP": "2000000",
        "_ownerAccumulatedDamage": "0",
        "_otherOverload": int(other_overload),
        "_otherBattleOverload": 0,
        "_otherMaxHP": str(max_hp),
        "_otherAccumulatedDamage": str(damage),
        "_otherStartHp": float(other_start_hp),
    }
    return json.dumps(obj, separators=(",", ":"))


def _reward_summary(body: dict) -> str:
    ra = body.get("_rewardAllList")
    if not isinstance(ra, dict):
        return "-"
    parts: list[str] = []
    for k, v in ra.items():
        if not isinstance(v, dict):
            continue
        lst = v.get("_list") or []
        if isinstance(lst, list) and lst:
            parts.append(f"{k}:{len(lst)}")
    return ",".join(parts) if parts else "empty"



def _my_supporters(info_body: dict) -> list[dict]:
    return _list_of(info_body.get("_mySupporterCharacterDimBoxInfoList"))


def _placement_map(info_body: dict) -> dict[int, dict]:
    """Map supporter key -> placement {owner_uid, equip_index, is_public, device_uid}."""
    by_device: dict[str, dict] = {}
    for c in _list_of(info_body.get("_connectedDeviceList")):
        dev = c.get("_device") if isinstance(c.get("_device"), dict) else {}
        uid = dev.get("_uid")
        if uid:
            by_device[str(uid)] = dev
    # private device list also useful
    for d in _list_of(info_body.get("_deviceList")):
        uid = d.get("_uid")
        if uid and str(uid) not in by_device:
            by_device[str(uid)] = d

    out: dict[int, dict] = {}
    for s in _my_supporters(info_body):
        key = _int(s.get("_key"), 0)
        duid = s.get("_dimBoxDeviceUID")
        if not key or not duid:
            continue
        dev = by_device.get(str(duid)) or {}
        owner = str(dev.get("_ownerId") or "")
        is_public = bool(dev.get("_isPublic")) or (owner in PUBLIC_BOX_IDS)
        out[key] = {
            "key": key,
            "owner_uid": owner,
            "equip_index": _int(dev.get("_equipIndex"), 0),
            "is_public": is_public,
            "device_uid": str(duid),
            "reward_interval_count": _int(s.get("_rewardIntervalCount"), 0),
            "remain_time": _int(s.get("_remainTime"), 0),
        }
    return out


def _reward_items_summary(body: dict) -> list[str]:
    """Human-ish summary from disconnect _rewardAllList._rewardList."""
    ra = body.get("_rewardAllList") if isinstance(body, dict) else None
    if not isinstance(ra, dict):
        return []
    labels: list[str] = []
    rl = ra.get("_rewardList") or {}
    for it in _list_of(rl):
        labels.append(
            f"type={it.get('_type')} value={it.get('_value')} x{it.get('_count')}"
        )
    # also note non-empty other bags
    extra = _reward_summary(body)
    if extra and extra not in ("-", "empty"):
        labels.append(f"bags={extra}")
    return labels


def collect_dbox_rewards(
    session: GameSession,
    *,
    log: LogFn = print,
    reconnect: bool = True,
) -> dict:
    """If any supporter has rewardIntervalCount>0, disconnect those keys to claim.

    This is the private-box red-bang / 领取 action observed live.
    """
    result: dict[str, Any] = {
        "ok": False,
        "claimable": [],
        "claimed_keys": [],
        "disconnect_code": None,
        "rewards": [],
        "reconnected": [],
        "reconnect_failed": [],
        "skipped_reason": None,
        "placements": {},
    }

    info_body = dbox_api.info(session.client)
    _raise_if_kick(info_body, "dimensional-box/info")
    if _code(info_body) not in (0, None):
        result["disconnect_code"] = _code(info_body)
        result["skipped_reason"] = "info_fail"
        log(f"[-] dbox collect info code={_code(info_body)} msg={info_body.get('_message')}")
        return result

    my_uid, my_level, nick = extract_me(info_body)
    placements = _placement_map(info_body)
    result["placements"] = {str(k): v for k, v in placements.items()}

    claimable = []
    for s in _my_supporters(info_body):
        cnt = _int(s.get("_rewardIntervalCount"), 0)
        key = _int(s.get("_key"), 0)
        if key and cnt > 0:
            claimable.append(
                {
                    "key": key,
                    "count": cnt,
                    "remain_time": _int(s.get("_remainTime"), 0),
                    "device_uid": s.get("_dimBoxDeviceUID"),
                    "placement": placements.get(key),
                }
            )
    result["claimable"] = claimable
    if not claimable:
        result["ok"] = True
        result["skipped_reason"] = "no_claimable"
        log("[*] dbox collect: no rewardIntervalCount>0 (no red-bang claim)")
        return result

    keys = [c["key"] for c in claimable]
    log(
        f"[*] dbox collect claimable keys={keys} "
        f"counts={[c['count'] for c in claimable]} me={nick or my_uid}"
    )
    # remember placements for reconnect (claimable only)
    want_place = []
    for c in claimable:
        p = c.get("placement") or placements.get(c["key"])
        if p:
            want_place.append(p)
        else:
            want_place.append({"key": c["key"], "owner_uid": None, "equip_index": None, "is_public": None})

    resp = dbox_api.device_disconnect(session.client, keys)
    _raise_if_kick(resp, "dimensional-box/device-disconnect")
    result["disconnect_code"] = _code(resp)
    result["rewards"] = _reward_items_summary(resp)
    if _code(resp) not in (0, None):
        log(
            f"[-] dbox disconnect/claim fail code={_code(resp)} "
            f"msg={resp.get('_message')} details={resp.get('_details')}"
        )
        return result

    result["claimed_keys"] = keys
    log(
        f"[+] dbox claim ok keys={keys} rewards={result['rewards'][:12]}"
        + (" ..." if len(result["rewards"]) > 12 else "")
    )

    # Reconnect is handled by ensure_public_box_connections (prefer public empty slots).
    result["ok"] = True
    return result


def _reconnect_keys(
    session: GameSession,
    *,
    keys: list[int],
    preferred: list[dict],
    my_uid: str | None,
    log: LogFn,
) -> dict:
    pref_by_key = { _int(p.get("key"), 0): p for p in preferred if p }
    reconnected: list[dict] = []
    failed: list[dict] = []
    for key in keys:
        p = pref_by_key.get(int(key)) or {}
        ok_entry = _place_one(
            session,
            key=int(key),
            preferred=p,
            my_uid=my_uid,
            log=log,
        )
        if ok_entry.get("ok"):
            reconnected.append(ok_entry)
        else:
            failed.append(ok_entry)
    return {"reconnected": reconnected, "failed": failed}


def _try_reconnect_free(
    session: GameSession,
    *,
    info_body: dict,
    my_uid: str | None,
    log: LogFn,
) -> dict:
    free = []
    skipped = []
    for s in _my_supporters(info_body):
        if s.get("_dimBoxDeviceUID"):
            continue
        key = _int(s.get("_key"), 0)
        remain = _int(s.get("_remainTime"), 0)
        if not key:
            continue
        # remainTime==0 => no placement quota left (live: -53011)
        if remain <= 0:
            skipped.append({"key": key, "reason": "remainTime=0"})
            continue
        free.append(key)
    if skipped:
        log(f"[*] dbox reconnect skip no-quota keys={skipped}")
    if not free:
        return {"reconnected": [], "failed": skipped}
    log(f"[*] dbox reconnect free keys={free}")
    return _reconnect_keys(session, keys=free, preferred=[], my_uid=my_uid, log=log)


def _place_one(
    session: GameSession,
    *,
    key: int,
    preferred: dict,
    my_uid: str | None,
    log: LogFn,
) -> dict:
    """Try preferred slot first, then empty private, then empty public."""
    attempts: list[dict] = []

    def try_private(owner: str, equip: int) -> dict | None:
        r = dbox_api.device_connect(
            session.client,
            owner_uid=owner,
            key=key,
            equip_index=equip,
        )
        _raise_if_kick(r, "dimensional-box/device-connect")
        entry = {
            "key": key,
            "mode": "private",
            "owner_uid": owner,
            "equip_index": equip,
            "code": _code(r),
            "message": r.get("_message"),
        }
        attempts.append(entry)
        if _code(r) in (0, None):
            entry["ok"] = True
            log(f"[+] dbox reconnect private key={key} equip={equip}")
            return entry
        return None

    def try_public(owner: str, equip: int) -> dict | None:
        r = dbox_api.public_device_connect(
            session.client,
            owner_uid=str(owner),
            key=key,
            equip_index=equip,
        )
        _raise_if_kick(r, "dimensional-box/public-device-connect")
        entry = {
            "key": key,
            "mode": "public",
            "owner_uid": str(owner),
            "equip_index": equip,
            "code": _code(r),
            "message": r.get("_message"),
        }
        attempts.append(entry)
        if _code(r) in (0, None):
            entry["ok"] = True
            log(f"[+] dbox reconnect public key={key} box={owner} equip={equip}")
            return entry
        return None

    # 1) preferred
    if preferred.get("owner_uid") is not None and preferred.get("equip_index") is not None:
        owner = str(preferred.get("owner_uid"))
        equip = _int(preferred.get("equip_index"), 0)
        if preferred.get("is_public") or owner in PUBLIC_BOX_IDS:
            hit = try_public(owner, equip)
        else:
            hit = try_private(owner, equip)
        if hit:
            return hit

    # Hard fail codes that are not slot-specific (don't scan every slot)
    # -53011: no placement time left for this supporter
    # -53018: redeploy cooltime for this box/target
    STOP_CODES = {-53011}

    # 2) empty private slots (need fresh info)
    info_body = dbox_api.info(session.client)
    _raise_if_kick(info_body, "dimensional-box/info(place)")
    if not my_uid:
        my_uid, _, _ = extract_me(info_body)
    empties = [
        _int(d.get("_equipIndex"), 0)
        for d in _list_of(info_body.get("_deviceList"))
        if not d.get("_key") and not d.get("_targetUserId")
    ]
    if my_uid:
        for equip in empties:
            hit = try_private(str(my_uid), equip)
            if hit:
                return hit
            if attempts and attempts[-1].get("code") in STOP_CODES:
                log(
                    f"[*] dbox reconnect stop key={key} code={attempts[-1].get('code')} "
                    f"msg={attempts[-1].get('message')}"
                )
                return {
                    "ok": False,
                    "key": key,
                    "attempts": attempts[-3:],
                    "message": attempts[-1].get("message"),
                    "code": attempts[-1].get("code"),
                }

    # 3) empty public slots
    for box in PUBLIC_BOX_IDS:
        pub = dbox_api.public_info(session.client, box)
        _raise_if_kick(pub, f"dimensional-box/public-info[{box}]")
        if _code(pub) not in (0, None):
            continue
        for d in _list_of(pub.get("_deviceList")):
            if d.get("_targetUserId"):
                continue
            hit = try_public(box, _int(d.get("_equipIndex"), 0))
            if hit:
                return hit
            if attempts and attempts[-1].get("code") in STOP_CODES:
                log(
                    f"[*] dbox reconnect stop key={key} code={attempts[-1].get('code')} "
                    f"msg={attempts[-1].get('message')}"
                )
                return {
                    "ok": False,
                    "key": key,
                    "attempts": attempts[-3:],
                    "message": attempts[-1].get("message"),
                    "code": attempts[-1].get("code"),
                }
            # cooltime on one public box: try next box, not every equip
            if attempts and attempts[-1].get("code") == -53018:
                break

    log(f"[*] dbox reconnect failed key={key} attempts={len(attempts)} last={attempts[-1] if attempts else None}")
    return {
        "ok": False,
        "key": key,
        "attempts": attempts[-3:],
        "message": (attempts[-1] or {}).get("message") if attempts else "no_slot",
        "code": (attempts[-1] or {}).get("code") if attempts else None,
    }



def _is_public_owner(owner_uid: str | None, is_public: bool | None = None) -> bool:
    if is_public:
        return True
    return str(owner_uid or "") in PUBLIC_BOX_IDS


def ensure_public_box_connections(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict:
    """Keep supporters connected on public boxes.

    - If already on a public slot: leave it.
    - If unplaced or only on private: find an empty public slot and connect.
    - Private placement is not preferred; moving private->public requires disconnect first.
    - remainTime<=0 and unplaced: skip (no placement quota).
    """
    result: dict[str, Any] = {
        "ok": False,
        "already_public": [],
        "connected": [],
        "moved": [],
        "failed": [],
        "skipped": [],
    }

    info_body = dbox_api.info(session.client)
    _raise_if_kick(info_body, "dimensional-box/info")
    if _code(info_body) not in (0, None):
        result["skipped"].append({"reason": "info_fail", "code": _code(info_body)})
        log(f"[-] dbox ensure-public info code={_code(info_body)} msg={info_body.get('_message')}")
        return result

    my_uid, _, nick = extract_me(info_body)
    placements = _placement_map(info_body)
    supporters = _my_supporters(info_body)
    log(f"[*] dbox ensure-public me={nick or my_uid} supporters={len(supporters)}")

    # cache empty public slots lazily
    empty_public: list[tuple[str, int]] | None = None

    def load_empty_public() -> list[tuple[str, int]]:
        slots: list[tuple[str, int]] = []
        for box in PUBLIC_BOX_IDS:
            pub = dbox_api.public_info(session.client, box)
            _raise_if_kick(pub, f"dimensional-box/public-info[{box}]")
            if _code(pub) not in (0, None):
                continue
            for d in _list_of(pub.get("_deviceList")):
                if d.get("_targetUserId"):
                    continue
                slots.append((str(box), _int(d.get("_equipIndex"), 0)))
        return slots

    def take_public_slot() -> tuple[str, int] | None:
        nonlocal empty_public
        if empty_public is None:
            empty_public = load_empty_public()
        if not empty_public:
            return None
        return empty_public.pop(0)

    def connect_public(key: int, box: str, equip: int) -> dict:
        r = dbox_api.public_device_connect(
            session.client, owner_uid=str(box), key=int(key), equip_index=int(equip)
        )
        _raise_if_kick(r, "dimensional-box/public-device-connect")
        return {
            "key": key,
            "box": str(box),
            "equip_index": int(equip),
            "code": _code(r),
            "message": r.get("_message"),
            "ok": _code(r) in (0, None),
        }

    for s in supporters:
        key = _int(s.get("_key"), 0)
        if not key:
            continue
        remain = _int(s.get("_remainTime"), 0)
        placed = bool(s.get("_dimBoxDeviceUID"))
        p = placements.get(key)

        if p and _is_public_owner(p.get("owner_uid"), p.get("is_public")):
            result["already_public"].append(
                {"key": key, "box": p.get("owner_uid"), "equip_index": p.get("equip_index")}
            )
            log(
                f"[*] dbox public ok key={key} box={p.get('owner_uid')} "
                f"equip={p.get('equip_index')} remain={remain}"
            )
            continue

        # not on public
        if not placed and remain <= 0:
            result["skipped"].append({"key": key, "reason": "remainTime=0"})
            log(f"[*] dbox public skip key={key} remainTime=0")
            continue

        # if on private, disconnect first (only this key)
        if p and not _is_public_owner(p.get("owner_uid"), p.get("is_public")):
            log(
                f"[*] dbox move private->public key={key} "
                f"from equip={p.get('equip_index')}"
            )
            disc = dbox_api.device_disconnect(session.client, [key])
            _raise_if_kick(disc, "dimensional-box/device-disconnect(move)")
            if _code(disc) not in (0, None):
                entry = {
                    "key": key,
                    "stage": "disconnect_private",
                    "code": _code(disc),
                    "message": disc.get("_message"),
                }
                result["failed"].append(entry)
                log(f"[-] dbox move disconnect fail key={key} code={_code(disc)} msg={disc.get('_message')}")
                continue
            # after disconnect, refresh empty public (slot freed elsewhere unrelated)
            empty_public = None

        # try preferred previous public if any (from claim preferred not available here)
        connected = False
        last_err = None
        # attempt several empty public slots
        for _try in range(12):
            slot = take_public_slot()
            if not slot:
                last_err = {"code": None, "message": "no_empty_public_slot"}
                break
            box, equip = slot
            entry = connect_public(key, box, equip)
            if entry.get("ok"):
                result["connected" if not p else "moved"].append(entry)
                log(f"[+] dbox public connect key={key} box={box} equip={equip}")
                connected = True
                break
            last_err = entry
            code = entry.get("code")
            # character-level stop
            if code == -53011:
                log(f"[*] dbox public stop key={key} code=-53011 msg={entry.get('message')}")
                break
            # cooltime on this box: try other boxes (slot list already mixed)
            if code == -53018:
                # drop remaining slots of same box
                if empty_public is not None:
                    empty_public = [(b, e) for (b, e) in empty_public if b != str(box)]
                continue
            # occupied / invalid slot: try next
            continue

        if not connected:
            fail = {"key": key, "stage": "public_connect", **(last_err or {})}
            result["failed"].append(fail)
            log(f"[-] dbox public connect fail key={key} last={last_err}")

    result["ok"] = True
    log(
        f"[*] dbox ensure-public done already={len(result['already_public'])} "
        f"connected={len(result['connected'])} moved={len(result['moved'])} "
        f"failed={len(result['failed'])} skipped={len(result['skipped'])}"
    )
    return result


def run_dbox_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    max_attacks: int | None = None,
) -> dict:
    """Maintain dbox: claim if needed, keep public connections, then attacks.

    Public placement policy:
      - do not withdraw public supporters unless claiming rewards
      - if unplaced / kicked / only private: connect an empty public slot
    """
    # Line A: deploy/claim (does not gate attacks)
    collect = collect_dbox_rewards(session, log=log, reconnect=False)
    public = ensure_public_box_connections(session, log=log)

    # Line B: attacks — independent of whether we have public placements
    attacks = run_dbox_attacks(
        session, login_wall=login_wall, log=log, max_attacks=max_attacks
    )
    return {
        "ok": bool(attacks.get("ok")),
        "collect": collect,
        "public": public,
        "attacks": attacks,
        # flatten common fields for qmdauto log line
        "wins": attacks.get("wins"),
        "fails": attacks.get("fails"),
        "eligible": attacks.get("eligible"),
        "candidates": attacks.get("candidates"),
        "ovr_before": attacks.get("ovr_before"),
        "ovr_after": attacks.get("ovr_after"),
        "skipped_reason": attacks.get("skipped_reason"),
        "claimed_keys": collect.get("claimed_keys") or [],
        "claimable": collect.get("claimable") or [],
        "rewards": collect.get("rewards") or [],
        "reconnected": (public.get("connected") or []) + (public.get("moved") or []),
        "reconnect_failed": public.get("failed") or [],
        "already_public": public.get("already_public") or [],
    }


def run_dbox_attacks(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    max_attacks: int | None = None,
) -> dict:
    """Scan public boxes and attack eligible targets while A.OVR < 25.

    Independent of our own deploy/claim state.
    """
    result: dict[str, Any] = {
        "ok": False,
        "my_uid": None,
        "my_level": None,
        "ovr_before": None,
        "ovr_after": None,
        "candidates": 0,
        "eligible": 0,
        "attacks": [],
        "wins": 0,
        "fails": 0,
        "skipped_reason": None,
        "errors": [],
    }

    info_body = dbox_api.info(session.client)
    _raise_if_kick(info_body, "dimensional-box/info")
    if _code(info_body) not in (0, None):
        result["errors"].append(
            {"where": "info", "code": _code(info_body), "message": info_body.get("_message")}
        )
        log(f"[-] dbox info code={_code(info_body)} msg={info_body.get('_message')}")
        return result

    my_uid, my_level, nick = extract_me(info_body)
    result["my_uid"] = my_uid
    result["my_level"] = my_level
    if not my_uid:
        result["skipped_reason"] = "no_my_uid"
        log("[!] dbox: cannot resolve self uid from info")
        return result
    if not my_level:
        # still allow if we can read levels on targets; treat as very high gap failsafe
        my_level = 0
        result["my_level"] = my_level
        log("[!] dbox: self level unknown; level filter may skip all")

    server_ms = current_server_ms(session, login_wall=login_wall)
    targets = _collect_public_targets(
        session, my_uid=str(my_uid), server_ms=server_ms, log=log
    )
    result["candidates"] = len(targets)
    log(
        f"[*] dbox me={nick or my_uid} lv={my_level} candidates(time>=30m)={len(targets)}"
    )
    if not targets:
        result["ok"] = True
        result["skipped_reason"] = "no_candidates"
        result["ovr_before"] = result["ovr_after"] = None
        return result

    # seed A.OVR from first device-info
    first = targets[0]
    di0 = dbox_api.device_info(
        session.client, target_uid=first["target_uid"], key=first["key"]
    )
    _raise_if_kick(di0, "dimensional-box/device-info")
    ovr = _int(di0.get("_attackOverloadValue"), 0)
    result["ovr_before"] = ovr
    result["ovr_after"] = ovr
    log(f"[*] dbox A.OVR={ovr} (attack only while <{OVR_MAX}; independent of deploy)")
    if ovr >= OVR_MAX:
        result["ok"] = True
        result["skipped_reason"] = f"ovr_ge_{OVR_MAX}"
        log(f"[*] dbox skip attacks: A.OVR={ovr} >= {OVR_MAX}")
        return result

    max_lv = int(my_level) - LEVEL_GAP if my_level else -1
    attacks_done = 0

    for t in targets:
        if ovr >= OVR_MAX:
            break
        if max_attacks is not None and attacks_done >= max_attacks:
            break

        di = dbox_api.device_info(
            session.client, target_uid=t["target_uid"], key=t["key"]
        )
        _raise_if_kick(di, "dimensional-box/device-info")
        if _code(di) not in (0, None):
            entry = {
                "target": t["target_uid"],
                "box": t["box"],
                "code": _code(di),
                "message": di.get("_message"),
                "stage": "device-info",
            }
            result["fails"] += 1
            result["attacks"].append(entry)
            log(
                f"[-] dbox device-info fail box={t['box']} code={_code(di)} msg={di.get('_message')}"
            )
            continue

        ovr = _int(di.get("_attackOverloadValue"), ovr)
        result["ovr_after"] = ovr
        if ovr >= OVR_MAX:
            log(f"[*] dbox stop attacks: A.OVR={ovr} >= {OVR_MAX}")
            break

        user = ((di.get("_user") or {}).get("_user") or {}) if isinstance(di.get("_user"), dict) else {}
        lvl = _int(user.get("_level"), 0)
        if my_level and lvl > max_lv:
            continue

        result["eligible"] += 1
        max_hp = _int((di.get("_user") or {}).get("_maxHp"), 1)
        acc = _int((di.get("_user") or {}).get("_accumulateDamage"), 0)
        other_start = max(0, max_hp - acc)
        # deal enough to cover remaining HP
        damage = _digits(max(1, other_start + 1))
        other_ov = _int((di.get("_user") or {}).get("_overloadValue"), 0)
        server_ms = current_server_ms(session, login_wall=login_wall)
        bi = _build_battle_info(
            ovr=ovr,
            other_overload=other_ov,
            max_hp=max_hp,
            damage=damage,
            other_start_hp=float(other_start),
            server_ms=server_ms,
        )
        body = dbox_api.battle(
            session.client,
            target_uid=t["target_uid"],
            attack_req_uid=t["device_uid"],
            is_win=True,
            battle_info=bi,
            owner_uid=t["box"],
            equip_index=t["equip_index"],
            is_public=True,
            attacker_received_damage="0",
            damage=damage,
        )
        _raise_if_kick(body, "dimensional-box/battle")
        attacks_done += 1
        code = _code(body)
        entry = {
            "nick": user.get("_nickName"),
            "level": lvl,
            "box": t["box"],
            "equip_index": t["equip_index"],
            "target_uid": t["target_uid"],
            "device_uid": t["device_uid"],
            "connect_sec": round(float(t["connect_sec"]), 1),
            "ovr_before": ovr,
            "damage": damage,
            "code": code,
            "message": body.get("_message"),
            "rewards": _reward_summary(body),
        }
        if code in (0, None):
            result["wins"] += 1
            # refresh OVR after win
            di2 = dbox_api.device_info(
                session.client, target_uid=t["target_uid"], key=t["key"]
            )
            _raise_if_kick(di2, "dimensional-box/device-info(after)")
            if _code(di2) in (0, None):
                ovr = _int(di2.get("_attackOverloadValue"), ovr)
            entry["ovr_after"] = ovr
            result["ovr_after"] = ovr
            log(
                f"[+] dbox win {user.get('_nickName')} lv={lvl} box={t['box']} "
                f"conn={t['connect_sec']:.0f}s ovr={entry['ovr_before']}->{ovr} "
                f"rewards={entry['rewards']}"
            )
        else:
            result["fails"] += 1
            entry["details"] = body.get("_details")
            log(
                f"[-] dbox battle fail {user.get('_nickName')} lv={lvl} box={t['box']} "
                f"code={code} msg={body.get('_message')} details={body.get('_details')}"
            )
        result["attacks"].append(entry)
        time.sleep(BATTLE_GAP_SEC)

    result["ok"] = True
    log(
        f"[*] dbox done wins={result['wins']} fails={result['fails']} "
        f"eligible={result['eligible']} ovr={result['ovr_before']}->{result['ovr_after']}"
    )
    return result
