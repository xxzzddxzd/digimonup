"""Player promotion (升阶) quest progress for TUI.

Server init/quest-list only has current `_value`, not dest. Dest for the
current promotion tier is known from UI (e.g. rank 11→12):
  4101 open equip 10000
  4102 kill mobs   5000
  4103 use meat    4000

Kill progress is advanced locally from farm kill-mob counts so TUI remaining
does not require re-fetching quest/list each battle.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# Promotion rank key -> quest key -> dest (total needed).
# Extend when advancing ranks if dest changes.
PROMO_DEST_BY_RANK: dict[int, dict[int, int]] = {
    11: {
        4101: 10000,  # 开装备
        4102: 5000,   # 击退敌人
        4103: 4000,   # 用肉
    },
}

QUEST_META: dict[int, dict[str, Any]] = {
    4101: {"label": "开装备", "track_kills": False},
    4102: {"label": "击退", "track_kills": True},
    4103: {"label": "用肉", "track_kills": False},
}


def _list_of(container: Any) -> list:
    if container is None:
        return []
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for k in ("_list", "list"):
            v = container.get(k)
            if isinstance(v, list):
                return v
    return []


def extract_promotion_key(init_data: dict | None) -> int:
    if not isinstance(init_data, dict):
        return 0
    root = init_data.get("_initData") if isinstance(init_data.get("_initData"), dict) else init_data
    for it in _list_of(root.get("_list") if isinstance(root, dict) else None):
        if not isinstance(it, dict):
            continue
        data = it.get("_data") or {}
        if not isinstance(data, dict):
            continue
        promo = data.get("_promotion")
        if isinstance(promo, dict) and promo.get("_key") is not None:
            try:
                return int(promo.get("_key") or 0)
            except Exception:
                return 0
    return 0


def extract_quest_map(init_data: dict | None) -> dict[int, dict]:
    """quest_key -> {_key,_value,_level,_isGetReward} from init-data quest lists."""
    out: dict[int, dict] = {}
    if not isinstance(init_data, dict):
        return out
    root = init_data.get("_initData") if isinstance(init_data.get("_initData"), dict) else init_data
    for it in _list_of(root.get("_list") if isinstance(root, dict) else None):
        if not isinstance(it, dict):
            continue
        data = it.get("_data") or {}
        if not isinstance(data, dict) or "_questList" not in data:
            continue
        for q in _list_of(data.get("_questList")):
            if not isinstance(q, dict) or q.get("_key") is None:
                continue
            try:
                key = int(q["_key"])
            except Exception:
                continue
            out[key] = q
    return out


def dest_table_for_rank(rank_key: int) -> dict[int, int]:
    if rank_key in PROMO_DEST_BY_RANK:
        return dict(PROMO_DEST_BY_RANK[rank_key])
    # Fallback: use the latest known table (same keys) so UI still works.
    if PROMO_DEST_BY_RANK:
        return dict(PROMO_DEST_BY_RANK[max(PROMO_DEST_BY_RANK)])
    return {}


def build_promotion_snapshot(
    init_data: dict | None,
    *,
    rank_key: int | None = None,
    quest_map: dict[int, dict] | None = None,
) -> dict[str, Any]:
    """Build promotion progress snapshot from init-data (server base values)."""
    rank = int(rank_key if rank_key is not None else extract_promotion_key(init_data) or 0)
    qmap = quest_map if quest_map is not None else extract_quest_map(init_data)
    dests = dest_table_for_rank(rank)
    items: list[dict[str, Any]] = []
    # Prefer known order 4101/4102/4103; include any dest keys present.
    keys = list(QUEST_META.keys())
    for k in dests:
        if k not in keys:
            keys.append(k)
    for key in keys:
        dest = int(dests.get(key) or 0)
        if dest <= 0 and key not in qmap:
            continue
        meta = QUEST_META.get(key) or {"label": f"Q{key}", "track_kills": key == 4102}
        q = qmap.get(key) or {}
        try:
            base = int(float(q.get("_value") or 0))
        except Exception:
            base = 0
        rewarded = bool(q.get("_isGetReward"))
        if dest <= 0:
            dest = max(base, 1)
        items.append(
            {
                "key": int(key),
                "label": str(meta.get("label") or f"Q{key}"),
                "base": base,
                "dest": dest,
                "local": 0,
                "track_kills": bool(meta.get("track_kills")),
                "rewarded": rewarded,
            }
        )
    return {"rank": rank, "items": items}


def format_promo_line(item: dict[str, Any]) -> str:
    dest = int(item.get("dest") or 0)
    base = int(item.get("base") or 0)
    local = int(item.get("local") or 0)
    cur = base + local
    if dest > 0:
        cur = min(cur, dest)
    remain = max(0, dest - cur) if dest else 0
    done = "✓" if item.get("rewarded") or (dest and cur >= dest) else ""
    label = item.get("label") or "?"
    if done:
        return f"{label} {cur}/{dest}{done}"
    return f"{label} {cur}/{dest} 剩{remain}"
