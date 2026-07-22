"""Item spawner / 装备炉.

- CLI `zb`: open equip only (spawn-and-sell, default total 1000 items)
- `run_item_spawner_care`: furnace maintain for auto (info/add-gold/level-up/complete)

GameData (ItemSpawner[level]):
  SpawnCount  — max items opened per batch (lv17 = 8)
  Gold        — bit cost per add-gold deposit while at this level
  GoldCount   — deposits needed before /level-up can start
  Time        — seconds of level-up construction

Runtime info (_itemSpawner):
  _level, _count (deposits done), _status (0 ready / 1 upgrading / 2 complete),
  _completeTime, _adAccelCount, _helpCount, _isHelpRequested
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from .apis import item_spawner as sp_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
GOODS_GOLD = 0  # E_GOODS_TYPE.Gold — 比特 / bit

TABLE_PATH = Path(__file__).resolve().parent.parent / "item_spawner_table.json"

STATUS_NAME = {
    sp_api.STATUS_READY: "ready",
    sp_api.STATUS_IN_PROGRESS: "upgrading",
    sp_api.STATUS_COMPLETED: "complete",
}


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


def load_spawner_table(path: Path | None = None) -> dict[str, dict[str, int]]:
    p = path or TABLE_PATH
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, dict[str, int]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        out[str(k)] = {
            "level": _int(v.get("level"), _int(k)),
            "spawn_count": _int(v.get("spawn_count")),
            "gold": _int(v.get("gold")),
            "gold_count": _int(v.get("gold_count")),
            "time": _int(v.get("time")),
        }
    return out


def table_row(level: int, table: dict[str, dict[str, int]] | None = None) -> Optional[dict[str, int]]:
    t = table if table is not None else load_spawner_table()
    return t.get(str(int(level)))


def spawner_from(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    sp = payload.get("_itemSpawner") or payload.get("itemSpawner") or {}
    if isinstance(sp, dict) and ("_level" in sp or "level" in sp or "_count" in sp):
        return sp
    # nested under init-style wrappers
    data = payload.get("_data") or {}
    if isinstance(data, dict):
        sp = data.get("_itemSpawner") or data.get("itemSpawner") or {}
        if isinstance(sp, dict):
            return sp
    return sp if isinstance(sp, dict) else {}


def summarize_spawner(sp: dict, *, table: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    level = _int(sp.get("_level", sp.get("level")), 0)
    count = _int(sp.get("_count", sp.get("count")), 0)
    status = _int(sp.get("_status", sp.get("status")), 0)
    complete_time = _int(sp.get("_completeTime", sp.get("completeTime")), 0)
    row = table_row(level, table)
    gold = row["gold"] if row else None
    gold_count = row["gold_count"] if row else None
    spawn_count = row["spawn_count"] if row else None
    time_sec = row["time"] if row else None

    remain_deposits = None
    bit_per_deposit = gold
    bit_remain = None
    bit_total = None
    if gold is not None and gold_count is not None:
        remain_deposits = max(0, int(gold_count) - int(count))
        bit_total = int(gold) * int(gold_count)
        bit_remain = int(gold) * remain_deposits

    next_row = table_row(level + 1, table) if level else None

    return {
        "level": level,
        "count": count,
        "status": status,
        "status_name": STATUS_NAME.get(status, str(status)),
        "complete_time": complete_time,
        "ad_accel_count": _int(sp.get("_adAccelCount", sp.get("adAccelCount")), 0),
        "help_count": _int(sp.get("_helpCount", sp.get("helpCount")), 0),
        "is_help_requested": bool(sp.get("_isHelpRequested", sp.get("isHelpRequested"))),
        "table": row,
        "spawn_count": spawn_count,
        "bit_per_deposit": bit_per_deposit,
        "deposits_needed": gold_count,
        "deposits_done": count,
        "deposits_remain": remain_deposits,
        "bit_total_for_level": bit_total,
        "bit_remain_for_level": bit_remain,
        "levelup_time_sec": time_sec,
        "next_level": (level + 1) if next_row else None,
        "next_spawn_count": next_row["spawn_count"] if next_row else None,
    }


def format_upgrade_cost(summary: dict[str, Any]) -> str:
    lv = summary.get("level")
    st = summary.get("status_name")
    if summary.get("status") == sp_api.STATUS_IN_PROGRESS:
        return (
            f"lv={lv} status={st} completeTime={summary.get('complete_time')} "
            f"(建造中，无需再投 bit)"
        )
    if summary.get("status") == sp_api.STATUS_COMPLETED:
        return f"lv={lv} status={st} (可 complete 升到下一档)"
    bit = summary.get("bit_per_deposit")
    need = summary.get("deposits_needed")
    done = summary.get("deposits_done")
    remain = summary.get("deposits_remain")
    total = summary.get("bit_total_for_level")
    left = summary.get("bit_remain_for_level")
    t = summary.get("levelup_time_sec")
    if bit is None:
        return f"lv={lv} status={st} count={done} (无本地表，无法算 bit)"
    return (
        f"lv={lv} status={st} "
        f"deposit={done}/{need} "
        f"bit/次={bit} "
        f"剩余次数={remain} "
        f"剩余bit={left} "
        f"本级总bit={total} "
        f"建造秒={t} "
        f"spawn/批={summary.get('spawn_count')}"
    )


def fetch_info(session: GameSession) -> tuple[dict, dict]:
    body = sp_api.item_spawner_info(session.client)
    _raise_if_kick(body, "item-spawner/info")
    sp = spawner_from(body)
    return body, sp



def _list_from(payload: Any, *keys: str) -> list:
    if not isinstance(payload, dict):
        return []
    cur: Any = payload
    for k in keys:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(k)
    if isinstance(cur, dict):
        lst = cur.get("_list") or cur.get("list") or []
        return [x for x in lst if isinstance(x, dict)] if isinstance(lst, list) else []
    if isinstance(cur, list):
        return [x for x in cur if isinstance(x, dict)]
    return []


def _item_uid(item: dict) -> str:
    return str(item.get("_uid") or item.get("uid") or item.get("strUID") or "")


def _item_type(item: dict) -> int:
    return _int(item.get("_type", item.get("type")), -1)


def _item_level(item: dict) -> int:
    return _int(item.get("_level", item.get("level")), 0)


def _item_key(item: dict) -> int:
    return _int(item.get("_key", item.get("key")), 0)


def _item_options(item: dict) -> list[dict]:
    opts = item.get("_options") or item.get("options") or []
    return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []


def _option_score(opts: list[dict]) -> tuple[float, float, int]:
    """(sum_value, sum_grade, option_count) — higher is better."""
    total_v = 0.0
    total_g = 0.0
    for o in opts:
        try:
            total_v += float(o.get("_value", o.get("value") or 0))
        except Exception:
            pass
        total_g += float(_int(o.get("_grade", o.get("grade")), 0))
    return (total_v, total_g, len(opts))


def item_score(item: dict) -> tuple:
    """Comparable score tuple (higher = better).

    Hologram value is mostly in random options; prefer quality over raw level
    so a high-lv trash 1-opt piece does not beat a lower-lv multi high-grade piece.
      option_count > option grade sum > option value sum > level > key
    """
    ov, og, oc = _option_score(_item_options(item))
    return (
        oc,
        og,
        ov,
        _item_level(item),
        _item_key(item),
    )


def is_better_item(new_item: dict, cur_item: dict | None) -> bool:
    if cur_item is None:
        return True
    return item_score(new_item) > item_score(cur_item)


def extract_items(payload: Any) -> list[dict]:
    """Pull item dicts from list / spawn / reward payloads."""
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def add_all(items: list[dict]) -> None:
        for it in items:
            uid = _item_uid(it)
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            out.append(it)

    # direct
    add_all(_list_from(payload, "_itemList"))
    add_all(_list_from(payload, "itemList"))
    # matched keep list
    add_all(_list_from(payload, "_matchedItemList"))
    add_all(_list_from(payload, "matchedItemList"))
    # reward bag
    ra = payload.get("_rewardAllList") or payload.get("rewardAllList") or {}
    if isinstance(ra, dict):
        add_all(_list_from(ra, "_itemList"))
        add_all(_list_from(ra, "itemList"))
    return out


def extract_matched_items(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    matched = _list_from(payload, "_matchedItemList")
    if matched:
        return matched
    matched = _list_from(payload, "matchedItemList")
    if matched:
        return matched
    # fallback: if filter matched but list empty, use reward itemList
    if payload.get("_isFilterMatched") or payload.get("isFilterMatched"):
        return extract_items(payload)
    return []


def load_item_bag(session: GameSession) -> list[dict]:
    body = sp_api.item_list(session.client)
    _raise_if_kick(body, "item/list")
    return extract_items(body)


def _walk_find_partner_equip_list(payload: Any) -> list[dict]:
    """Find partner._equipList entries: [{_uid,_type}, ...] from init-data."""
    found: list[dict] = []

    def walk(o: Any, path: str = "") -> None:
        nonlocal found
        if found:
            return
        if isinstance(o, dict):
            # partner node with _equipList
            if path.endswith("_partner") or path.endswith(".partner") or path == "_partner":
                el = o.get("_equipList") or o.get("equipList")
                if isinstance(el, dict):
                    lst = el.get("_list") or el.get("list") or []
                    if isinstance(lst, list):
                        found = [x for x in lst if isinstance(x, dict)]
                        return
                elif isinstance(el, list):
                    found = [x for x in el if isinstance(x, dict)]
                    return
            for k, v in o.items():
                p = f"{path}.{k}" if path else str(k)
                walk(v, p)
                if found:
                    return
        elif isinstance(o, list):
            for i, v in enumerate(o[:80]):
                walk(v, f"{path}[{i}]")
                if found:
                    return

    walk(payload)
    return found


def load_equip_uid_set(session: GameSession) -> set[str]:
    """UIDs currently worn (from init-data partner._equipList)."""
    uids: set[str] = set()
    src = session.init_data if isinstance(getattr(session, "init_data", None), dict) else {}
    for e in _walk_find_partner_equip_list(src):
        uid = str(e.get("_uid") or e.get("uid") or "").strip()
        if uid:
            uids.add(uid)
    return uids


def load_equipped_by_type(session: GameSession) -> dict[int, dict]:
    """Map item type slot -> currently equipped bag item.

    Prefer partner._equipList UIDs from init-data (authoritative).
    item/list omits _isEquipped; fall back to best score per type.
    """
    items = load_item_bag(session)
    by_uid = {_item_uid(it): it for it in items if _item_uid(it)}
    by_type_groups: dict[int, list[dict]] = {}
    for it in items:
        t = _item_type(it)
        if t < 0:
            continue
        by_type_groups.setdefault(t, []).append(it)

    equipped: dict[int, dict] = {}
    eq_uids = load_equip_uid_set(session)
    for uid in eq_uids:
        it = by_uid.get(uid)
        if not it:
            continue
        t = _item_type(it)
        if t >= 0:
            equipped[t] = it

    # fill missing slots with best bag item of that type
    for t, group in by_type_groups.items():
        if t in equipped:
            continue
        flagged = [
            x
            for x in group
            if x.get("_isEquipped") is True
            or x.get("isEquipped") is True
            or x.get("_isEquipped") == 1
            or _item_uid(x) in eq_uids
        ]
        pool = flagged or group
        equipped[t] = max(pool, key=item_score)
    return equipped


def load_pending_bag_items(session: GameSession) -> list[dict]:
    """Bag items not currently equipped (spawn leftovers / unequipped)."""
    eq_uids = load_equip_uid_set(session)
    items = load_item_bag(session)
    if not eq_uids:
        # no equip map: treat multi-per-type extras as pending
        by_type: dict[int, list[dict]] = {}
        for it in items:
            t = _item_type(it)
            if t < 0:
                continue
            by_type.setdefault(t, []).append(it)
        pending: list[dict] = []
        for group in by_type.values():
            if len(group) <= 1:
                continue
            best = max(group, key=item_score)
            buid = _item_uid(best)
            for it in group:
                uid = _item_uid(it)
                if uid and uid != buid:
                    pending.append(it)
        return pending
    return [it for it in items if _item_uid(it) and _item_uid(it) not in eq_uids]


def cleanup_bag_keep_best(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict[str, Any]:
    """Clear pending spawn leftovers so next spawn-and-sell is not blocked (-35004).

    1) Prefer selling bag items not in partner equipList.
    2) Also sell worse duplicates per type (keep equipped, else best score).
    """
    items = load_item_bag(session)
    eq_uids = load_equip_uid_set(session)
    by_type: dict[int, list[dict]] = {}
    for it in items:
        t = _item_type(it)
        if t < 0 or not _item_uid(it):
            continue
        by_type.setdefault(t, []).append(it)

    sell_uids: list[str] = []
    # unequipped first
    for it in items:
        uid = _item_uid(it)
        if uid and eq_uids and uid not in eq_uids:
            sell_uids.append(uid)

    # duplicates: keep equipped uid if present, else best score
    for t, group in by_type.items():
        keep_uid = None
        for it in group:
            uid = _item_uid(it)
            if uid and uid in eq_uids:
                keep_uid = uid
                break
        if keep_uid is None:
            keep_uid = _item_uid(max(group, key=item_score))
        for it in group:
            uid = _item_uid(it)
            if uid and uid != keep_uid:
                sell_uids.append(uid)

    sell_uids = list(dict.fromkeys(u for u in sell_uids if u))
    out: dict[str, Any] = {
        "bag": len(items),
        "slots": len(by_type),
        "equip_uids": len(eq_uids),
        "sell_n": len(sell_uids),
        "sold": sell_uids,
        "code": 0,
    }
    if not sell_uids:
        log(
            f"[*] zb bag clean: {len(items)} items / {len(by_type)} slots "
            f"equip={len(eq_uids)}, nothing to sell"
        )
        return out

    log(
        f"[*] zb bag clean: sell pending/duplicates n={len(sell_uids)} "
        f"keep_slots={len(by_type)} equip={len(eq_uids)}"
    )
    body = sp_api.item_sell(session.client, item_uids=sell_uids)
    _raise_if_kick(body, "item/sell")
    out["code"] = _code(body)
    if out["code"] == 0:
        log(f"[+] zb bag clean sell ok n={len(sell_uids)}")
    else:
        msg = body.get("_message") if isinstance(body, dict) else None
        log(f"[-] zb bag clean sell fail code={out['code']} msg={msg}")
    return out


def _format_item_short(item: dict | None) -> str:
    if not item:
        return "none"
    opts = _item_options(item)
    ov, og, oc = _option_score(opts)
    return (
        f"uid={_item_uid(item)[:8]}… type={_item_type(item)} "
        f"lv={_item_level(item)} key={_item_key(item)} "
        f"opts={oc} val={ov:.4f} gradeSum={og:.0f}"
    )


def _sell_uids(
    session: GameSession,
    uids: list[str],
    *,
    log: LogFn = print,
    label: str = "sell",
) -> dict[str, Any]:
    uids = list(dict.fromkeys(u for u in uids if u))
    out: dict[str, Any] = {"uids": uids, "code": 0, "n": len(uids)}
    if not uids:
        return out
    body = sp_api.item_sell(session.client, item_uids=uids)
    _raise_if_kick(body, "item/sell")
    code = _code(body)
    out["code"] = code
    out["message"] = body.get("_message") if isinstance(body, dict) else None
    if code == 0:
        log(f"[+] zb {label} ok n={len(uids)}")
    else:
        log(f"[-] zb {label} fail code={code} n={len(uids)} msg={out['message']}")
    return out


def process_item_candidates(
    session: GameSession,
    candidates: list[dict],
    *,
    equipped: dict[int, dict] | None = None,
    auto_equip: bool = True,
    auto_sell: bool = True,
    log: LogFn = print,
    source: str = "match",
) -> dict[str, Any]:
    """Compare candidates vs equipped; if better equip then sell old; else sell new.

    Mirrors client UIItemSelect / CompareStatEquipedItem flow used after spawn:
      better -> PS_ItemEquip then sell _unEquipUID
      worse/equal -> PS_ItemSell new
    """
    out: dict[str, Any] = {
        "source": source,
        "candidates": [],
        "equipped": [],
        "sold": [],
        "kept_worse": [],
        "skipped": [],
        "errors": [],
    }
    if equipped is None:
        equipped = load_equipped_by_type(session)
    else:
        equipped = dict(equipped)

    out["candidate_count"] = len(candidates)
    log(
        f"[*] zb {source}: {len(candidates)} candidate(s), "
        f"equipped_slots={len(equipped)}"
    )

    sell_uids: list[str] = []

    for new in candidates:
        uid = _item_uid(new)
        t = _item_type(new)
        if not uid or t < 0:
            out["skipped"].append({"reason": "bad_item", "raw_keys": list(new.keys())[:8]})
            continue
        cur = equipped.get(t)
        # never sell the currently equipped piece as "new"
        if cur and _item_uid(cur) == uid:
            out["skipped"].append({"reason": "already_equipped", "uid": uid, "type": t})
            log(f"[*] zb {source} type={t} uid={uid[:8]}… already equipped, skip")
            continue

        better = is_better_item(new, cur)
        entry = {
            "uid": uid,
            "type": t,
            "new": _format_item_short(new),
            "cur": _format_item_short(cur),
            "new_score": list(item_score(new)),
            "cur_score": list(item_score(cur)) if cur else None,
            "better": better,
        }
        out["candidates"].append(entry)
        log(
            f"[*] zb compare type={t} better={better} "
            f"new=({entry['new']}) cur=({entry['cur']})"
        )

        if better and auto_equip:
            body = sp_api.item_equip(session.client, item_uid=uid, is_equip=True)
            _raise_if_kick(body, "item/equip")
            code = _code(body)
            entry["equip_code"] = code
            # -35006: already equipped — treat as ok for equip step
            if code not in (0, -35006):
                msg = body.get("_message") if isinstance(body, dict) else None
                log(f"[-] zb equip fail uid={uid[:8]}… code={code} msg={msg}")
                out["errors"].append({"op": "equip", "uid": uid, "code": code, "message": msg})
                if auto_sell:
                    sell_uids.append(uid)
                continue

            if code == 0:
                log(f"[+] zb equip ok type={t} uid={uid[:8]}…")
            else:
                log(f"[*] zb equip already-on type={t} uid={uid[:8]}… code={code}")
            out["equipped"].append(entry)

            # Prefer server-reported unequipped UID (authoritative after swap)
            old_uid = ""
            if isinstance(body, dict):
                old_uid = str(body.get("_unEquipUID") or body.get("unEquipUID") or "")
                entry["equip_uid"] = body.get("_equipUID") or body.get("equipUID")
                entry["un_equip_uid"] = old_uid or None
            if not old_uid and cur:
                old_uid = _item_uid(cur)
            if auto_sell and old_uid and old_uid != uid:
                sell_uids.append(old_uid)
                entry["sell_old"] = old_uid
            equipped[t] = new
            # refresh equip uid set on session init cache if present
            try:
                if isinstance(session.init_data, dict) and old_uid:
                    for e in _walk_find_partner_equip_list(session.init_data):
                        if str(e.get("_uid") or "") == old_uid:
                            e["_uid"] = uid
            except Exception:
                pass
        else:
            if auto_sell:
                sell_uids.append(uid)
                out["kept_worse"].append(entry)
                log(f"[*] zb worse/equal -> sell new uid={uid[:8]}…")
            else:
                out["skipped"].append(entry)

    if auto_sell and sell_uids:
        sold = _sell_uids(session, sell_uids, log=log, label=f"{source}-sell")
        out["sell_code"] = sold.get("code")
        out["sold"] = sold.get("uids") or []
        if sold.get("code") not in (0, None):
            out["errors"].append(
                {"op": "sell", "code": sold.get("code"), "uids": sold.get("uids")}
            )

    out["equipped_map_size"] = len(equipped)
    return out


def process_matched_equips(
    session: GameSession,
    spawn_body: dict,
    *,
    equipped: dict[int, dict] | None = None,
    auto_equip: bool = True,
    auto_sell: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """If _isFilterMatched: compare vs equipped; equip if better then sell old; else sell new."""
    matched = bool(spawn_body.get("_isFilterMatched") or spawn_body.get("isFilterMatched"))
    out: dict[str, Any] = {
        "matched": matched,
        "candidates": [],
        "equipped": [],
        "sold": [],
        "kept_worse": [],
        "skipped": [],
        "errors": [],
    }
    if not matched:
        return out

    candidates = extract_matched_items(spawn_body)
    if not candidates:
        candidates = extract_items(spawn_body)
    result = process_item_candidates(
        session,
        candidates,
        equipped=equipped,
        auto_equip=auto_equip,
        auto_sell=auto_sell,
        log=log,
        source="match",
    )
    result["matched"] = True
    return result


def process_pending_bag_items(
    session: GameSession,
    *,
    equipped: dict[int, dict] | None = None,
    auto_equip: bool = True,
    auto_sell: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """Resolve unequipped bag items (e.g. after -35004) via compare/equip/sell."""
    pending = load_pending_bag_items(session)
    if not pending:
        log("[*] zb pending: no unequipped bag items")
        return {"pending": 0, "candidates": [], "sold": [], "equipped": []}
    log(f"[*] zb pending: {len(pending)} unequipped item(s) to resolve")
    return process_item_candidates(
        session,
        pending,
        equipped=equipped,
        auto_equip=auto_equip,
        auto_sell=auto_sell,
        log=log,
        source="pending",
    )


def fetch_stuck_spawn_body(
    session: GameSession,
    *,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    filter_stat_type_list: Optional[list[int]] = None,
) -> dict:
    """POST spawn-and-sell with _count=0 to read server-side pending spawn queue.

    When a previous filter-match batch was left unresolved, normal spawn returns
    -35004. count=0 does not open new items; it returns the stuck
    _matchedItemList (these UIDs often do NOT appear in item/list).
    """
    body = sp_api.item_spawn_and_sell(
        session.client,
        count=0,
        filter_grade=filter_grade,
        filter_match_count=filter_match_count,
        filter_stat_type_list=filter_stat_type_list,
    )
    _raise_if_kick(body, "item/spawn-and-sell count=0")
    return body if isinstance(body, dict) else {}


def resolve_stuck_spawn_queue(
    session: GameSession,
    *,
    equipped: dict[int, dict] | None = None,
    auto_equip: bool = True,
    auto_sell: bool = True,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    filter_stat_type_list: Optional[list[int]] = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Clear -35004 queue: count=0 → compare → equip if better → sell rest/old.

    This is the '直接卖了继续抽' path for stuck filter-match results that are
    not visible in the normal bag list.
    """
    out: dict[str, Any] = {
        "ok": False,
        "source": "stuck_queue",
        "code": 0,
        "matched": False,
        "candidates": [],
        "equipped": [],
        "sold": [],
    }
    body = fetch_stuck_spawn_body(
        session,
        filter_grade=filter_grade,
        filter_match_count=filter_match_count,
        filter_stat_type_list=filter_stat_type_list,
    )
    out["code"] = _code(body)
    out["message"] = body.get("_message")
    out["is_filter_matched"] = body.get("_isFilterMatched")
    matched = bool(body.get("_isFilterMatched") or body.get("isFilterMatched"))
    candidates = extract_matched_items(body)
    if not candidates and matched:
        candidates = extract_items(body)
    out["matched"] = matched
    out["candidate_count"] = len(candidates)

    if out["code"] not in (0, None):
        log(f"[-] zb stuck-queue fetch fail code={out['code']} msg={out['message']}")
        return out

    if not candidates:
        log("[*] zb stuck-queue: empty (no pending spawn results)")
        out["ok"] = True
        return out

    log(
        f"[*] zb stuck-queue: {len(candidates)} pending result item(s) "
        f"(not in bag list) — compare/equip/sell then continue"
    )
    actions = process_item_candidates(
        session,
        candidates,
        equipped=equipped,
        auto_equip=auto_equip,
        auto_sell=auto_sell,
        log=log,
        source="stuck",
    )
    out.update(actions)
    out["ok"] = not actions.get("errors")
    out["stuck_body_keys"] = sorted(body.keys())
    return out


def run_spawn_batches(
    session: GameSession,
    *,
    batches: Optional[int] = None,
    total: Optional[int] = 1000,
    count: Optional[int] = None,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    filter_stat_type_list: Optional[list[int]] = None,
    auto_equip: bool = True,
    auto_sell: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """Open equip via spawn-and-sell.

    Default: open until ``total`` items (1000), per-batch size = table SpawnCount.
    If ``batches`` is set, run that many batches instead (ignores total).

    When _isFilterMatched: compare vs equipped; if better equip+sell old, else sell new.
    Stop early on spawn failure.
    """
    table = load_spawner_table()
    result: dict[str, Any] = {
        "ok": False,
        "batches_ok": 0,
        "items_ok": 0,
        "runs": [],
        "equip_actions": [],
    }

    info_body, sp = fetch_info(session)
    summary = summarize_spawner(sp, table=table)
    result["before"] = summary
    log(f"[*] zb info: {format_upgrade_cost(summary)}")

    batch_count = int(count) if count is not None else int(summary.get("spawn_count") or 8)
    if batch_count < 1:
        batch_count = 1
    result["batch_count"] = batch_count

    # batches mode vs total-items mode
    if batches is not None and int(batches) > 0:
        target_items = int(batches) * batch_count
        max_batches = int(batches)
        mode = "batches"
    else:
        target_items = int(total) if total is not None and int(total) > 0 else 1000
        max_batches = max(1, (target_items + batch_count - 1) // batch_count)
        mode = "total"
    result["mode"] = mode
    result["target_items"] = target_items
    result["batches_requested"] = max_batches
    log(
        f"[*] zb plan: mode={mode} target_items={target_items} "
        f"per_batch={batch_count} max_batches={max_batches}"
    )

    equipped: dict[int, dict] | None = None
    if auto_equip or auto_sell:
        try:
            # clear multi-per-slot leftovers that block next spawn (-35004)
            cleanup_bag_keep_best(session, log=log)
            # resolve any remaining unequipped via compare/equip/sell
            process_pending_bag_items(
                session,
                auto_equip=auto_equip,
                auto_sell=auto_sell,
                log=log,
            )
            equipped = load_equipped_by_type(session)
            log(
                f"[*] zb equipped slots loaded: {sorted(equipped.keys())} "
                f"({len(equipped)})"
            )
            for t, it in sorted(equipped.items()):
                log(f"    slot {t}: {_format_item_short(it)}")
            # Proactively drain server-side stuck spawn queue (count=0).
            # These pending UIDs are often invisible in item/list and cause -35004.
            stuck = resolve_stuck_spawn_queue(
                session,
                equipped=equipped,
                auto_equip=auto_equip,
                auto_sell=auto_sell,
                filter_grade=filter_grade,
                filter_match_count=filter_match_count,
                filter_stat_type_list=filter_stat_type_list,
                log=log,
            )
            if stuck.get("candidate_count"):
                result["equip_actions"].append(stuck)
                result["stuck_resolved"] = {
                    "n": stuck.get("candidate_count"),
                    "equipped_n": len(stuck.get("equipped") or []),
                    "sold_n": len(stuck.get("sold") or []),
                }
                try:
                    from .apis import account as acc_api

                    session.init_data = acc_api.init_data(session.client)
                except Exception:
                    pass
                equipped = load_equipped_by_type(session)
        except Exception as exc:
            log(f"[!] zb load equipped failed: {exc}")
            equipped = {}

    def _do_spawn(n: int | None = None) -> dict:
        return sp_api.item_spawn_and_sell(
            session.client,
            count=batch_count if n is None else int(n),
            filter_grade=filter_grade,
            filter_match_count=filter_match_count,
            filter_stat_type_list=filter_stat_type_list,
        )

    items_ok = 0
    for i in range(max_batches):
        remain = target_items - items_ok
        if remain <= 0:
            log(f"[*] zb target reached items={items_ok}/{target_items}, stop")
            break
        this_count = min(batch_count, remain)
        log(
            f"[*] zb spawn-and-sell batch={i + 1}/{max_batches} "
            f"count={this_count} progress={items_ok}/{target_items} "
            f"filterGrade={filter_grade} match={filter_match_count}"
        )
        body = _do_spawn(this_count)
        _raise_if_kick(body, "item/spawn-and-sell")
        code = _code(body)
        # pending result items block further spawns (-35004)
        if code == -35004 and (auto_sell or auto_equip):
            log(
                "[!] zb -35004: fetch stuck queue via count=0, "
                "then compare/equip/sell and retry"
            )
            cleanup_bag_keep_best(session, log=log)
            try:
                stuck = resolve_stuck_spawn_queue(
                    session,
                    equipped=equipped,
                    auto_equip=auto_equip,
                    auto_sell=auto_sell,
                    filter_grade=filter_grade,
                    filter_match_count=filter_match_count,
                    filter_stat_type_list=filter_stat_type_list,
                    log=log,
                )
                result["equip_actions"].append(stuck)
            except SessionKicked:
                raise
            except Exception as exc:
                log(f"[!] zb stuck-queue process failed: {exc}")
            try:
                process_pending_bag_items(
                    session,
                    equipped=equipped,
                    auto_equip=auto_equip,
                    auto_sell=auto_sell,
                    log=log,
                )
            except Exception:
                pass
            try:
                if hasattr(session, "init_data"):
                    from .apis import account as acc_api

                    session.init_data = acc_api.init_data(session.client)
                equipped = load_equipped_by_type(session)
            except Exception:
                try:
                    equipped = load_equipped_by_type(session)
                except Exception:
                    pass
            body = _do_spawn(this_count)
            _raise_if_kick(body, "item/spawn-and-sell")
            code = _code(body)
        run: dict[str, Any] = {
            "batch": i + 1,
            "count": this_count,
            "code": code,
            "message": body.get("_message") if isinstance(body, dict) else None,
            "is_filter_matched": body.get("_isFilterMatched") if isinstance(body, dict) else None,
            "player_level": body.get("_playerLevel") if isinstance(body, dict) else None,
        }
        if isinstance(body, dict) and "_rewardAllList" in body:
            ra = body["_rewardAllList"]
            if isinstance(ra, dict):
                run["reward_keys"] = sorted(ra.keys())
        if code == 0:
            result["batches_ok"] += 1
            items_ok += this_count
            result["items_ok"] = items_ok
            log(
                f"[+] zb spawn ok batch={i + 1} matched={run.get('is_filter_matched')} "
                f"items={items_ok}/{target_items}"
            )
            # Always process when filter matched: compare -> equip if better -> sell
            if run.get("is_filter_matched") and isinstance(body, dict):
                try:
                    actions = process_matched_equips(
                        session,
                        body,
                        equipped=equipped,
                        auto_equip=auto_equip,
                        auto_sell=auto_sell,
                        log=log,
                    )
                    run["match_actions"] = {
                        "equipped_n": len(actions.get("equipped") or []),
                        "sold_n": len(actions.get("sold") or []),
                        "worse_n": len(actions.get("kept_worse") or []),
                        "candidates": actions.get("candidates"),
                        "sell_code": actions.get("sell_code"),
                        "errors": actions.get("errors"),
                    }
                    result["equip_actions"].append(actions)
                    if auto_equip:
                        try:
                            equipped = load_equipped_by_type(session)
                        except Exception:
                            pass
                except SessionKicked:
                    raise
                except Exception as exc:
                    run["match_error"] = str(exc)
                    log(f"[!] zb match process failed: {exc}")
            elif auto_sell:
                # even without filter match, clear any unexpected unequipped leftovers
                try:
                    pending = load_pending_bag_items(session)
                    if pending:
                        log(f"[*] zb post-spawn unequipped leftovers n={len(pending)}")
                        actions = process_pending_bag_items(
                            session,
                            equipped=equipped,
                            auto_equip=auto_equip,
                            auto_sell=auto_sell,
                            log=log,
                        )
                        run["pending_actions"] = {
                            "equipped_n": len(actions.get("equipped") or []),
                            "sold_n": len(actions.get("sold") or []),
                        }
                        result["equip_actions"].append(actions)
                        try:
                            equipped = load_equipped_by_type(session)
                        except Exception:
                            pass
                except SessionKicked:
                    raise
                except Exception as exc:
                    log(f"[!] zb post-spawn pending failed: {exc}")
            if items_ok >= target_items:
                result["runs"].append(run)
                log(f"[*] zb done: opened {items_ok} items (target {target_items})")
                break
        else:
            result["runs"].append(run)
            log(f"[-] zb spawn fail batch={i + 1} code={code} msg={run.get('message')}")
            if code == -35004:
                log(
                    "[!] zb -35004 persists after stuck-queue drain "
                    "(count=0 → compare/equip/sell). Check last_zb.json."
                )
            break
        result["runs"].append(run)

    result["items_ok"] = items_ok
    try:
        _, sp2 = fetch_info(session)
        result["after"] = summarize_spawner(sp2, table=table)
    except Exception as exc:
        result["after_error"] = str(exc)

    result["ok"] = items_ok >= target_items and items_ok > 0
    if items_ok > 0 and items_ok < target_items:
        result["ok"] = False
        result["partial"] = True
    return result


def run_item_spawner_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    max_deposits: int = 20,
    do_complete: bool = True,
    do_level_up: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """Furnace maintain for auto (no open-equip).

    Flow:
      1) /api/item-spawner/info
      2) if completed (or upgrade timer elapsed): /complete
      3) while ready and deposits remain: /add-gold
      4) if deposits full: /level-up (start build)

    Open equip is CLI-only via zb / run_spawn_batches.
    """
    table = load_spawner_table()
    result: dict[str, Any] = {
        "ok": False,
        "actions": [],
        "completed": False,
        "deposits": 0,
        "leveled_up": False,
        "skipped_reason": None,
    }
    # login_wall reserved for parity with other care helpers (server clock uses session)
    _ = login_wall

    info_body, sp = fetch_info(session)
    summary = summarize_spawner(sp, table=table)
    result["before"] = summary
    log(f"[*] furnace: {format_upgrade_cost(summary)}")
    if summary.get("bit_per_deposit") is not None:
        log(
            f"[*] furnace bit: per={summary['bit_per_deposit']} "
            f"remain_n={summary.get('deposits_remain')} "
            f"remain_bit={summary.get('bit_remain_for_level')} "
            f"total={summary.get('bit_total_for_level')}"
        )

    server_ms = current_server_ms(session)

    # 1) complete if ready
    if do_complete and summary["status"] == sp_api.STATUS_COMPLETED:
        log("[*] furnace complete level-up")
        body = sp_api.item_spawner_complete(session.client)
        _raise_if_kick(body, "item-spawner/complete")
        code = _code(body)
        result["actions"].append({"op": "complete", "code": code})
        if code != 0:
            log(f"[-] furnace complete fail code={code}")
            result["ok"] = False
            result["after"] = summary
            result["cost_summary"] = format_upgrade_cost(summary)
            return result
        result["completed"] = True
        sp = spawner_from(body) or spawner_from(fetch_info(session)[0])
        summary = summarize_spawner(sp, table=table)
        log(f"[+] furnace complete ok -> {format_upgrade_cost(summary)}")

    # if upgrading and time passed, treat as complete-able
    if do_complete and summary["status"] == sp_api.STATUS_IN_PROGRESS:
        ct = summary.get("complete_time") or 0
        # completeTime may be ms or sec; client uses long ms often
        if ct and ct < 10_000_000_000:
            ct_ms = ct * 1000
        else:
            ct_ms = ct
        if ct_ms and server_ms >= ct_ms:
            log("[*] furnace complete (timer elapsed)")
            body = sp_api.item_spawner_complete(session.client)
            _raise_if_kick(body, "item-spawner/complete")
            code = _code(body)
            result["actions"].append({"op": "complete_timer", "code": code})
            if code == 0:
                result["completed"] = True
                sp = spawner_from(body) or spawner_from(fetch_info(session)[0])
                summary = summarize_spawner(sp, table=table)
                log(f"[+] furnace complete ok -> {format_upgrade_cost(summary)}")
            else:
                log(f"[-] furnace complete fail code={code}")
        else:
            left = None
            if ct_ms:
                left = max(0, (ct_ms - server_ms) / 1000.0)
            result["skipped_reason"] = "upgrading"
            if left is not None:
                log(f"[*] furnace building, skip deposit left~={left:.0f}s")
            else:
                log("[*] furnace building, skip deposit")

    # 2) add-gold deposits
    deposits = 0
    while (
        summary["status"] == sp_api.STATUS_READY
        and summary.get("deposits_remain") is not None
        and summary["deposits_remain"] > 0
        and deposits < max_deposits
    ):
        bit = summary.get("bit_per_deposit")
        log(
            f"[*] furnace add-gold deposit={summary['deposits_done'] + 1}/"
            f"{summary.get('deposits_needed')} cost_bit={bit}"
        )
        body = sp_api.item_spawner_add_gold(session.client)
        _raise_if_kick(body, "item-spawner/add-gold")
        code = _code(body)
        result["actions"].append(
            {"op": "add_gold", "code": code, "bit": bit, "n": deposits + 1}
        )
        if code != 0:
            log(f"[-] furnace add-gold fail code={code} (可能 bit 不足)")
            if not result.get("skipped_reason"):
                result["skipped_reason"] = f"add_gold_fail:{code}"
            break
        deposits += 1
        result["deposits"] = deposits
        sp = spawner_from(body)
        if not sp:
            _, sp = fetch_info(session)
        summary = summarize_spawner(sp, table=table)
        log(f"[+] furnace add-gold ok -> {format_upgrade_cost(summary)}")

    # 3) start level-up when deposits full
    if (
        do_level_up
        and summary["status"] == sp_api.STATUS_READY
        and summary.get("deposits_remain") == 0
        and summary.get("deposits_needed")
        and summary.get("deposits_needed", 0) > 0
    ):
        log("[*] furnace level-up start build")
        body = sp_api.item_spawner_level_up(session.client)
        _raise_if_kick(body, "item-spawner/level-up")
        code = _code(body)
        result["actions"].append({"op": "level_up", "code": code})
        if code == 0:
            result["leveled_up"] = True
            sp = spawner_from(body) or spawner_from(fetch_info(session)[0])
            summary = summarize_spawner(sp, table=table)
            log(f"[+] furnace level-up ok -> {format_upgrade_cost(summary)}")
        else:
            log(f"[-] furnace level-up fail code={code}")
            if not result.get("skipped_reason"):
                result["skipped_reason"] = f"level_up_fail:{code}"

    if (
        summary["status"] == sp_api.STATUS_READY
        and summary.get("deposits_remain")
        and summary["deposits_remain"] > 0
        and deposits == 0
        and not result.get("skipped_reason")
    ):
        result["skipped_reason"] = "no_deposit"

    result["after"] = summary
    result["ok"] = True
    result["cost_summary"] = format_upgrade_cost(summary)
    return result


def run_upgrade(
    session: GameSession,
    *,
    max_deposits: int = 20,
    do_complete: bool = True,
    do_level_up: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """Alias of run_item_spawner_care (legacy name)."""
    return run_item_spawner_care(
        session,
        max_deposits=max_deposits,
        do_complete=do_complete,
        do_level_up=do_level_up,
        log=log,
    )


def run_zb(
    session: GameSession,
    *,
    batches: Optional[int] = None,
    total: Optional[int] = 1000,
    count: Optional[int] = None,
    info_only: bool = False,
    filter_grade: int = 0,
    filter_match_count: int = 0,
    auto_equip: bool = True,
    auto_sell: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    """CLI zb: open equip only (spawn-and-sell). Furnace care is auto-only.

    Default opens ``total`` items (1000) then stops. Override with ``batches``.
    """
    table = load_spawner_table()
    out: dict[str, Any] = {"ok": False, "mode": "zb"}

    info_body, sp = fetch_info(session)
    summary = summarize_spawner(sp, table=table)
    out["info"] = summary
    out["upgrade_cost_line"] = format_upgrade_cost(summary)
    log(f"[*] zb furnace snapshot: {out['upgrade_cost_line']}")

    if info_only:
        out["ok"] = True
        return out

    # batches=0 means no-op (e.g. info path); batches=None uses total default
    if batches is not None and int(batches) <= 0:
        out["ok"] = True
        return out

    sp_res = run_spawn_batches(
        session,
        batches=batches,
        total=total,
        count=count,
        filter_grade=filter_grade,
        filter_match_count=filter_match_count,
        auto_equip=auto_equip,
        auto_sell=auto_sell,
        log=log,
    )
    out["spawn"] = sp_res
    out["ok"] = bool(sp_res.get("ok"))
    return out
