"""异次元 box maintain: claim, redeploy self+search, attack.

Rules (user 2026-07-21):
  Deploy line and attack line are independent.
  Attack (always evaluated, even if not deployed):
  - only while A.OVR (_attackOverloadValue) < 25
  - target level <= my_level - 5
  - connected duration >= 30 minutes (else not attackable)
  - attack longest-connected first

Collect / redeploy triggers (disconnect then ensure re-place):
  A) rewardIntervalCount > 0
     => device-disconnect (claim interval rewards + unplace)
  B) private placement reaches client IsMaxRewardTime red-bang
     (elapsed since _startTime >= DimensionalBoxMaxRewardTimeOnce,
      or preResetCollectSeconds+elapsed >= DimensionalBoxMaxRewardTimeDaily)
     => device-disconnect + ensure re-place even if rewardIntervalCount==0

Deploy / 续上 (no public box; N supporters):
  - Exactly 1 supporter on 自己 box when free quota allows.
  - All remaining free supporters (any count, not limited to 2/3):
      search other private boxes, pick the top-N highest-加成 boxes,
      one supporter per box (one empty equip slot each).
  - Search rounds scale with free count (at least 5) so the pool is large enough.
  - Never withdraw an existing placement except to claim / max-reward reset.
  - Public box placement is abandoned for new deploys (existing left until claim).

Protocol (live):
  info:        POST /api/dimensional-box/info {}
  search:      POST /api/dimensional-box/search {}  -> _targetUserList
  target-info: POST /api/dimensional-box/target-info {"_ownerUID"}
  device-connect: POST /api/dimensional-box/device-connect
  public-info: POST /api/dimensional-box/public-info {"_ownerUID":"0".."4"}  (attack only)
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
from pathlib import Path
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
SEARCH_ROUNDS = 5

# GameData OtherDataOne (1.0.2): reward caps are minutes.
# Client UIRDChecker_DimensionalBox_Info uses IsMaxRewardTime on placed supporters.
MAX_REWARD_TIME_ONCE_MIN = 480   # DimensionalBoxMaxRewardTimeOnce
MAX_REWARD_TIME_DAILY_MIN = 960  # DimensionalBoxMaxRewardTimeDaily

# Optional local table: { "10": {"option": 1, "value": 5}, ... }
# option: 1=DataFragment 2=Collect (E_DBOX_DECO_OPTION_TYPE). Used for 加成 ranking.
_DECO_TABLE_PATH = Path(__file__).resolve().parent.parent / "dbox_deco_table.json"
_DECO_TABLE_CACHE: dict[int, tuple[int, int]] | None = None


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



def _supporter_elapsed_sec(supporter: dict, server_ms: int) -> float:
    start = _int(supporter.get("_startTime"), 0)
    if start <= 0 or server_ms <= 0:
        return 0.0
    return max(0.0, (float(server_ms) - float(start)) / 1000.0)


def is_max_reward_time(
    supporter: dict,
    *,
    server_ms: int,
    max_once_min: int = MAX_REWARD_TIME_ONCE_MIN,
    max_daily_min: int = MAX_REWARD_TIME_DAILY_MIN,
) -> bool:
    """Client-side IsMaxRewardTime / private red-bang approximation.

    Matches live UI: private placement lights red after long hang even when
    _rewardIntervalCount == 0. Config values are minutes.
    """
    if not supporter or not supporter.get("_dimBoxDeviceUID"):
        return False
    elapsed = _supporter_elapsed_sec(supporter, server_ms)
    if elapsed <= 0:
        return False
    once_sec = max(0, int(max_once_min)) * 60
    daily_sec = max(0, int(max_daily_min)) * 60
    pre = _int(supporter.get("_preResetCollectSeconds"), 0)
    if once_sec and elapsed >= once_sec:
        return True
    if daily_sec and (pre + elapsed) >= daily_sec:
        return True
    return False


def collect_dbox_rewards(
    session: GameSession,
    *,
    log: LogFn = print,
    reconnect: bool = True,
) -> dict:
    """Disconnect supporters that need claim or private max-reward reset.

    - rewardIntervalCount>0: claim interval rewards
    - private placement IsMaxRewardTime: red-bang even when count==0
      (disconnect then ensure re-place)
    """
    result: dict[str, Any] = {
        "ok": False,
        "claimable": [],
        "max_reward_private": [],
        "disconnect_targets": [],
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
    server_ms = current_server_ms(session)

    claimable = []
    max_reward_private = []
    for s in _my_supporters(info_body):
        key = _int(s.get("_key"), 0)
        if not key:
            continue
        cnt = _int(s.get("_rewardIntervalCount"), 0)
        p = placements.get(key)
        device_uid = s.get("_dimBoxDeviceUID")
        elapsed = _supporter_elapsed_sec(s, server_ms)
        base = {
            "key": key,
            "count": cnt,
            "remain_time": _int(s.get("_remainTime"), 0),
            "device_uid": device_uid,
            "placement": p,
            "elapsed_sec": round(elapsed, 1),
            "pre_reset_collect_sec": _int(s.get("_preResetCollectSeconds"), 0),
            "is_max_reward_time": False,
            "reason": None,
        }
        if cnt > 0:
            row = dict(base)
            row["reason"] = "reward_interval"
            claimable.append(row)
            continue
        # Private red-bang: hung to max reward time even if interval count is 0.
        if device_uid and p and not p.get("is_public") and is_max_reward_time(s, server_ms=server_ms):
            row = dict(base)
            row["is_max_reward_time"] = True
            row["reason"] = "max_reward_time_private"
            max_reward_private.append(row)

    result["claimable"] = claimable
    result["max_reward_private"] = max_reward_private
    # Disconnect union: interval claims + private max-reward reset.
    by_key: dict[int, dict] = {}
    for row in claimable + max_reward_private:
        by_key[int(row["key"])] = row
    targets = list(by_key.values())
    if not targets:
        result["ok"] = True
        result["skipped_reason"] = "no_claimable"
        log(
            "[*] dbox collect: no rewardIntervalCount>0 and no private max-reward "
            "(no disconnect)"
        )
        return result

    keys = [int(t["key"]) for t in targets]
    log(
        f"[*] dbox collect disconnect keys={keys} "
        f"interval={[c['key'] for c in claimable]} "
        f"maxRewardPrivate={[c['key'] for c in max_reward_private]} "
        f"me={nick or my_uid}"
    )
    # remember placements for reconnect (all disconnect targets)
    want_place = []
    for c in targets:
        p = c.get("placement") or placements.get(c["key"])
        if p:
            want_place.append(p)
        else:
            want_place.append(
                {
                    "key": c["key"],
                    "owner_uid": None,
                    "equip_index": None,
                    "is_public": None,
                }
            )
    result["disconnect_targets"] = targets

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

    # Reconnect is handled by ensure_box_connections (self + search).
    result["ok"] = True
    return result



def _is_public_owner(owner_uid: str | None, is_public: bool | None = None) -> bool:
    if is_public:
        return True
    return str(owner_uid or "") in PUBLIC_BOX_IDS


def _load_deco_table() -> dict[int, tuple[int, int]]:
    """key -> (optionType, value). option 1=DataFragment 2=Collect."""
    global _DECO_TABLE_CACHE
    if _DECO_TABLE_CACHE is not None:
        return _DECO_TABLE_CACHE
    table: dict[int, tuple[int, int]] = {}
    try:
        if _DECO_TABLE_PATH.is_file():
            raw = json.loads(_DECO_TABLE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        table[_int(k)] = (_int(v.get("option"), 0), _int(v.get("value"), 0))
                    elif isinstance(v, (list, tuple)) and len(v) >= 2:
                        table[_int(k)] = (_int(v[0]), _int(v[1]))
    except Exception:
        table = {}
    _DECO_TABLE_CACHE = table
    return table


def _deco_keys_from_equip(deco_list: Any) -> list[int]:
    keys: list[int] = []
    for d in _list_of(deco_list):
        k = _int(d.get("_key"), 0)
        if k > 0:
            keys.append(k)
    return keys


def score_box_bonus(deco_list: Any) -> tuple[int, int, int, int, int]:
    """Return sort key for 加成 (higher better).

    Client UI shows DataFragment + Collect via GetTotalDecoOptionRatio.
    When dbox_deco_table.json is present, use real option sums.
    Fallback: more equipped decos first, then higher key sum (later/better items).
    """
    keys = _deco_keys_from_equip(deco_list)
    table = _load_deco_table()
    data_fragment = 0
    collect = 0
    if table:
        for k in keys:
            opt, val = table.get(k, (0, 0))
            if opt == 1:
                data_fragment += val
            elif opt == 2:
                collect += val
        total = data_fragment + collect
        return (total, data_fragment, collect, len(keys), sum(keys))
    # no table: approximate 加成 by equip density
    return (len(keys), sum(keys), 0, len(keys), sum(keys))


def _user_uid(user_obj: Any) -> str | None:
    if not isinstance(user_obj, dict):
        return None
    uid = user_obj.get("_uid")
    if uid:
        return str(uid)
    nested = user_obj.get("_user")
    if isinstance(nested, dict) and nested.get("_uid"):
        return str(nested.get("_uid"))
    return None


def _parse_search_targets(body: dict) -> list[dict]:
    """Normalize search _targetUserList into candidate dicts."""
    out: list[dict] = []
    for t in _list_of(body.get("_targetUserList")):
        user = t.get("_user") if isinstance(t.get("_user"), dict) else {}
        uid = _user_uid(user)
        if not uid:
            continue
        deco = t.get("_dimensionalBoxDecoEquipList")
        score = score_box_bonus(deco)
        out.append(
            {
                "uid": uid,
                "nick": user.get("_nickName"),
                "level": _int(user.get("_level"), 0),
                "deco_keys": _deco_keys_from_equip(deco),
                "score": score,
                "bonus_total": score[0],
                "raw": t,
            }
        )
    return out


def search_box_candidates(
    session: GameSession,
    *,
    rounds: int = SEARCH_ROUNDS,
    exclude_uids: set[str] | None = None,
    log: LogFn = print,
) -> list[dict]:
    """Run search N times, merge unique owners, sort by 加成 desc."""
    exclude = set(exclude_uids or ())
    by_uid: dict[str, dict] = {}
    for i in range(max(1, int(rounds))):
        body = dbox_api.search(session.client)
        _raise_if_kick(body, f"dimensional-box/search[{i}]")
        if _code(body) not in (0, None):
            log(
                f"[*] dbox search#{i+1} code={_code(body)} msg={body.get('_message')}"
            )
            continue
        batch = _parse_search_targets(body)
        top = sorted(batch, key=lambda x: x["score"], reverse=True)[:3]
        log(
            f"[*] dbox search#{i+1} hits={len(batch)} "
            f"top={[(b.get('nick'), b.get('bonus_total'), b.get('deco_keys')) for b in top]}"
        )
        for b in batch:
            if b["uid"] in exclude:
                continue
            prev = by_uid.get(b["uid"])
            if prev is None or b["score"] > prev["score"]:
                by_uid[b["uid"]] = b
    ranked = sorted(by_uid.values(), key=lambda x: x["score"], reverse=True)
    log(
        f"[*] dbox search pool unique={len(ranked)} "
        f"best={[(r.get('nick'), r.get('bonus_total'), r.get('deco_keys')) for r in ranked[:5]]}"
    )
    return ranked


def _empty_equips_from_devices(devices: list[dict]) -> list[int]:
    empties: list[int] = []
    for d in devices:
        if d.get("_targetUserId") or _int(d.get("_key"), 0):
            continue
        empties.append(_int(d.get("_equipIndex"), 0))
    return empties


def _self_empty_equips(info_body: dict, my_uid: str | None) -> list[int]:
    empties: list[int] = []
    for d in _list_of(info_body.get("_deviceList")):
        owner = str(d.get("_ownerId") or "")
        if _is_public_owner(owner, bool(d.get("_isPublic"))):
            continue
        if my_uid and owner and owner != str(my_uid):
            continue
        if d.get("_targetUserId") or _int(d.get("_key"), 0):
            continue
        empties.append(_int(d.get("_equipIndex"), 0))
    return empties


def _connect_device(
    session: GameSession,
    *,
    owner_uid: str,
    key: int,
    equip_index: int,
    log: LogFn,
    label: str,
) -> dict:
    r = dbox_api.device_connect(
        session.client,
        owner_uid=str(owner_uid),
        key=int(key),
        equip_index=int(equip_index),
    )
    _raise_if_kick(r, f"dimensional-box/device-connect[{label}]")
    return {
        "ok": _code(r) in (0, None),
        "key": int(key),
        "owner_uid": str(owner_uid),
        "equip_index": int(equip_index),
        "code": _code(r),
        "message": r.get("_message"),
        "mode": label,
    }


def _place_on_self(
    session: GameSession,
    *,
    key: int,
    my_uid: str,
    info_body: dict,
    log: LogFn,
) -> dict:
    empties = _self_empty_equips(info_body, my_uid)
    if not empties:
        return {
            "ok": False,
            "key": key,
            "mode": "self",
            "message": "no_empty_self_slot",
            "code": None,
        }
    last: dict | None = None
    for equip in empties:
        entry = _connect_device(
            session,
            owner_uid=my_uid,
            key=key,
            equip_index=equip,
            log=log,
            label="self",
        )
        last = entry
        if entry.get("ok"):
            log(f"[+] dbox self connect key={key} equip={equip}")
            return entry
        if entry.get("code") == -53011:
            break
    return last or {
        "ok": False,
        "key": key,
        "mode": "self",
        "message": "self_connect_failed",
        "code": None,
    }


def _place_on_searched(
    session: GameSession,
    *,
    key: int,
    candidates: list[dict],
    occupied_owners: set[str],
    log: LogFn,
) -> dict:
    """Try highest-bonus candidates that still have empty slots."""
    last: dict | None = None
    tried_uids: list[str] = []
    for cand in candidates:
        uid = str(cand["uid"])
        if uid in occupied_owners:
            continue
        tried_uids.append(uid)
        ti = dbox_api.target_info(session.client, uid)
        _raise_if_kick(ti, f"dimensional-box/target-info[{uid[:8]}]")
        if _code(ti) not in (0, None):
            last = {
                "ok": False,
                "key": key,
                "mode": "search",
                "owner_uid": uid,
                "code": _code(ti),
                "message": ti.get("_message"),
                "bonus_total": cand.get("bonus_total"),
                "nick": cand.get("nick"),
            }
            continue
        empties = _empty_equips_from_devices(_list_of(ti.get("_deviceList")))
        if not empties:
            log(
                f"[*] dbox search target full nick={cand.get('nick')} "
                f"bonus={cand.get('bonus_total')} decos={cand.get('deco_keys')}"
            )
            last = {
                "ok": False,
                "key": key,
                "mode": "search",
                "owner_uid": uid,
                "message": "no_empty_slot",
                "bonus_total": cand.get("bonus_total"),
                "nick": cand.get("nick"),
            }
            continue
        for equip in empties:
            entry = _connect_device(
                session,
                owner_uid=uid,
                key=key,
                equip_index=equip,
                log=log,
                label="search",
            )
            entry["bonus_total"] = cand.get("bonus_total")
            entry["nick"] = cand.get("nick")
            entry["deco_keys"] = cand.get("deco_keys")
            entry["mode"] = "search"
            last = entry
            if entry.get("ok"):
                log(
                    f"[+] dbox search connect key={key} nick={cand.get('nick')} "
                    f"bonus={cand.get('bonus_total')} deco={cand.get('deco_keys')} "
                    f"equip={equip}"
                )
                return entry
            code = entry.get("code")
            if code == -53011:
                return entry
            if code in (-53018, -53003, -53004, -53005):
                log(
                    f"[*] dbox search skip nick={cand.get('nick')} code={code} "
                    f"msg={entry.get('message')}"
                )
                break
    return last or {
        "ok": False,
        "key": key,
        "mode": "search",
        "message": "no_candidate",
        "tried": tried_uids[:8],
        "code": None,
    }


def _search_rounds_for_free(free_count: int, *, base: int = SEARCH_ROUNDS) -> int:
    """Scale search rounds with free supporters so top-N boxes are covered.

    Each search returns ~5 owners. Default at least `base` (5). When many free
    slots need distinct boxes, grow rounds (cap 20) for spare full/cooltime skips.
    """
    n = max(0, int(free_count))
    if n <= 0:
        return int(base)
    # ~5 unique-ish per round; want ~2x free_count headroom in pool
    by_need = (n * 2 + 4) // 5  # ceil(2n/5)
    return min(20, max(int(base), by_need, n))


def ensure_box_connections(
    session: GameSession,
    *,
    log: LogFn = print,
    search_rounds: int | None = None,
) -> dict:
    """Keep 1 on self; place any number of free supporters on distinct search boxes.

    - Already placed (self / other private / leftover public): leave alone.
    - Free supporters with remainTime>0 (count unlimited):
        1) if no self placement yet → one device-connect on own empty slot
        2) remaining free (n) → search, rank by 加成, connect to top-n *different*
           private boxes (one empty equip each; skip owners we already occupy)
    - Public box is not used for new placements.
    """
    result: dict[str, Any] = {
        "ok": False,
        "already_self": [],
        "already_other": [],
        "already_public": [],
        "already_private": [],  # compat: self+other
        "connected_self": [],
        "connected_other": [],
        "connected_private": [],  # compat
        "connected_public": [],  # always empty under new policy
        "connected": [],
        "moved": [],
        "failed": [],
        "skipped": [],
        "search_pool": [],
        "search_rounds": 0,
        "free_for_search": 0,
    }

    info_body = dbox_api.info(session.client)
    _raise_if_kick(info_body, "dimensional-box/info")
    if _code(info_body) not in (0, None):
        result["skipped"].append({"reason": "info_fail", "code": _code(info_body)})
        log(
            f"[-] dbox ensure info code={_code(info_body)} msg={info_body.get('_message')}"
        )
        return result

    my_uid, _, nick = extract_me(info_body)
    placements = _placement_map(info_body)
    supporters = _my_supporters(info_body)
    log(
        f"[*] dbox ensure me={nick or my_uid} supporters={len(supporters)} "
        f"placements={len(placements)} policy=self+1 / search+rest(N)"
    )

    free: list[tuple[int, int]] = []  # (key, remain)
    occupied_owners: set[str] = set()
    if my_uid:
        occupied_owners.add(str(my_uid))

    for s in supporters:
        key = _int(s.get("_key"), 0)
        if not key:
            continue
        remain = _int(s.get("_remainTime"), 0)
        p = placements.get(key)
        if not p:
            if remain <= 0:
                result["skipped"].append({"key": key, "reason": "remainTime=0"})
                log(f"[*] dbox skip key={key} remainTime=0 (no quota)")
            else:
                free.append((key, remain))
            continue

        owner = str(p.get("owner_uid") or "")
        if owner:
            occupied_owners.add(owner)
        equip = p.get("equip_index")
        if _is_public_owner(owner, p.get("is_public")):
            entry = {
                "key": key,
                "box": owner,
                "equip_index": equip,
                "remain": remain,
                "mode": "public",
            }
            result["already_public"].append(entry)
            log(
                f"[*] dbox public leftover key={key} box={owner} equip={equip} "
                f"remain={remain} (no new public deploys)"
            )
            continue

        is_self = bool(my_uid and owner == str(my_uid))
        entry = {
            "key": key,
            "owner_uid": owner,
            "equip_index": equip,
            "remain": remain,
            "mode": "self" if is_self else "other",
        }
        if is_self:
            result["already_self"].append(entry)
            log(f"[*] dbox self ok key={key} equip={equip} remain={remain}")
        else:
            result["already_other"].append(entry)
            log(
                f"[*] dbox other ok key={key} owner={owner[:8]}… equip={equip} "
                f"remain={remain}"
            )
        result["already_private"].append(entry)

    free.sort(key=lambda x: x[1], reverse=True)
    has_self = bool(result["already_self"])

    # 1) fill self first if missing (only one self slot among free keys)
    if free and not has_self and my_uid:
        key, remain = free.pop(0)
        entry = _place_on_self(
            session, key=key, my_uid=str(my_uid), info_body=info_body, log=log
        )
        if entry.get("ok"):
            has_self = True
            result["connected_self"].append(entry)
            result["connected_private"].append(entry)
            result["connected"].append(entry)
            occupied_owners.add(str(my_uid))
            info_body = dbox_api.info(session.client)
            _raise_if_kick(info_body, "dimensional-box/info(after-self)")
        else:
            result["failed"].append(entry)
            log(f"[-] dbox self connect fail key={key} last={entry}")

    # 2) remaining free (any n) → search + top-n distinct highest-加成 boxes
    result["free_for_search"] = len(free)
    if free:
        rounds = (
            int(search_rounds)
            if search_rounds is not None
            else _search_rounds_for_free(len(free))
        )
        result["search_rounds"] = rounds
        log(
            f"[*] dbox search place free={len(free)} keys={[k for k,_ in free]} "
            f"rounds={rounds} (1 box each, highest bonus first)"
        )
        candidates = search_box_candidates(
            session,
            rounds=rounds,
            exclude_uids=occupied_owners,
            log=log,
        )
        # keep a bit more than free count for logging / retry headroom
        pool_n = max(20, len(free) * 3)
        result["search_pool"] = [
            {
                "uid": c["uid"],
                "nick": c.get("nick"),
                "level": c.get("level"),
                "bonus_total": c.get("bonus_total"),
                "deco_keys": c.get("deco_keys"),
            }
            for c in candidates[:pool_n]
        ]
        for idx, (key, remain) in enumerate(free, start=1):
            log(
                f"[*] dbox search assign {idx}/{len(free)} key={key} "
                f"remain={remain} occupied_boxes={len(occupied_owners)}"
            )
            entry = _place_on_searched(
                session,
                key=key,
                candidates=candidates,
                occupied_owners=occupied_owners,
                log=log,
            )
            if entry.get("ok"):
                owner = str(entry.get("owner_uid") or "")
                if owner:
                    occupied_owners.add(owner)
                result["connected_other"].append(entry)
                result["connected_private"].append(entry)
                result["connected"].append(entry)
            else:
                result["failed"].append(entry)
                log(
                    f"[-] dbox search connect fail key={key} remain={remain} last={entry}"
                )
    else:
        result["search_rounds"] = 0
        log("[*] dbox no free supporters for search place")

    result["ok"] = True
    log(
        f"[*] dbox ensure done "
        f"self={len(result['already_self'])}+{len(result['connected_self'])} "
        f"other={len(result['already_other'])}+{len(result['connected_other'])} "
        f"public_left={len(result['already_public'])} "
        f"free_search={result['free_for_search']} "
        f"failed={len(result['failed'])} skipped={len(result['skipped'])} "
        f"search_pool={len(result['search_pool'])} rounds={result['search_rounds']}"
    )
    return result


def ensure_public_box_connections(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict:
    """Backward-compatible alias for ensure_box_connections."""
    return ensure_box_connections(session, log=log)


def run_dbox_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    max_attacks: int | None = None,
) -> dict:
    """Maintain dbox: claim if needed, keep self+search placements, then attacks.

    Placement policy (N supporters):
      - withdraw only for rewardIntervalCount claim or private IsMaxRewardTime reset
      - after free/kick/claim: keep 1 on self; place remaining free on top-N
        distinct search boxes (highest 加成 each, one empty slot per box)
      - public box is not used for new deploys
    """
    collect = collect_dbox_rewards(session, log=log, reconnect=False)
    ensure = ensure_box_connections(session, log=log)

    attacks = run_dbox_attacks(
        session, login_wall=login_wall, log=log, max_attacks=max_attacks
    )
    return {
        "ok": bool(attacks.get("ok")),
        "collect": collect,
        "public": ensure,  # compat key name
        "ensure": ensure,
        "attacks": attacks,
        "wins": attacks.get("wins"),
        "fails": attacks.get("fails"),
        "eligible": attacks.get("eligible"),
        "candidates": attacks.get("candidates"),
        "ovr_before": attacks.get("ovr_before"),
        "ovr_after": attacks.get("ovr_after"),
        "skipped_reason": attacks.get("skipped_reason"),
        "claimed_keys": collect.get("claimed_keys") or [],
        "claimable": collect.get("claimable") or [],
        "max_reward_private": collect.get("max_reward_private") or [],
        "rewards": collect.get("rewards") or [],
        "reconnected": (ensure.get("connected") or []) + (ensure.get("moved") or []),
        "reconnect_failed": ensure.get("failed") or [],
        "already_public": ensure.get("already_public") or [],
        "already_private": ensure.get("already_private") or [],
        "already_self": ensure.get("already_self") or [],
        "already_other": ensure.get("already_other") or [],
        "connected_public": ensure.get("connected_public") or [],
        "connected_private": ensure.get("connected_private") or [],
        "connected_self": ensure.get("connected_self") or [],
        "connected_other": ensure.get("connected_other") or [],
        "search_pool": ensure.get("search_pool") or [],
        "search_rounds": ensure.get("search_rounds") or 0,
        "free_for_search": ensure.get("free_for_search") or 0,
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
