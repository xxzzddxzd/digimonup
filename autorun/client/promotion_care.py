"""Player promotion (升阶) quest progress for TUI.

Server init/quest-list only has current `_value`, not dest. Dest + labels
are rank-specific (from UI). Kill-tracked quests (if any) advance locally
from farm kill-mob counts without re-fetching.

Quest key pattern observed:
  rank 11 -> 4101, 4102, 4103
  rank 12 -> 4111, 4112, 4113
  base = 4100 + (rank - 11) * 10 ; keys = base+1 .. base+3
"""
from __future__ import annotations

from typing import Any, Optional

# rank_key -> list of quest defs (only show these; never fall back to other ranks)
PROMO_QUESTS_BY_RANK: dict[int, list[dict[str, Any]]] = {
    11: [
        {"key": 4101, "dest": 10000, "label": "开装备", "track_kills": False},
        {"key": 4102, "dest": 5000, "label": "击退", "track_kills": True},
        {"key": 4103, "dest": 4000, "label": "用肉", "track_kills": False},
    ],
    # 12→13: two gacha 7000 each (no kill). Keys 4111/4112 from live init.
    12: [
        {"key": 4111, "dest": 7000, "label": "抽卡1", "track_kills": False},
        {"key": 4112, "dest": 7000, "label": "抽卡2", "track_kills": False},
    ],
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


def quest_keys_for_rank(rank: int) -> list[int]:
    """Observed pattern: rank 11→4101-3, 12→4111-3, …"""
    if rank < 1:
        return []
    base = 4100 + (int(rank) - 11) * 10
    return [base + 1, base + 2, base + 3]


def defs_for_rank(rank: int, quest_map: dict[int, dict] | None = None) -> list[dict[str, Any]]:
    """Quest definitions for this rank only (no cross-rank fallback)."""
    rank = int(rank or 0)
    if rank in PROMO_QUESTS_BY_RANK:
        return [dict(x) for x in PROMO_QUESTS_BY_RANK[rank]]

    # Unknown rank: only show pattern keys that exist in quest_map, no invented dest/kill.
    qmap = quest_map or {}
    defs: list[dict[str, Any]] = []
    for i, key in enumerate(quest_keys_for_rank(rank), start=1):
        if key not in qmap:
            continue
        defs.append(
            {
                "key": key,
                "dest": 0,  # unknown
                "label": f"任务{i}",
                "track_kills": False,
            }
        )
    return defs


def build_promotion_snapshot(
    init_data: dict | None,
    *,
    rank_key: int | None = None,
    quest_map: dict[int, dict] | None = None,
) -> dict[str, Any]:
    """Build promotion progress snapshot from init-data (server base values)."""
    rank = int(rank_key if rank_key is not None else extract_promotion_key(init_data) or 0)
    qmap = quest_map if quest_map is not None else extract_quest_map(init_data)
    items: list[dict[str, Any]] = []
    for dfn in defs_for_rank(rank, qmap):
        key = int(dfn["key"])
        dest = int(dfn.get("dest") or 0)
        q = qmap.get(key) or {}
        try:
            base = int(float(q.get("_value") or 0))
        except Exception:
            base = 0
        # If quest key missing from server list, still show when we have an explicit def
        # (progress 0) so user sees targets; skip pattern-discovered missing keys already handled.
        if key not in qmap and rank not in PROMO_QUESTS_BY_RANK:
            continue
        rewarded = bool(q.get("_isGetReward"))
        items.append(
            {
                "key": key,
                "label": str(dfn.get("label") or f"Q{key}"),
                "base": base,
                "dest": dest,
                "local": 0,
                "track_kills": bool(dfn.get("track_kills")),
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
    label = item.get("label") or "?"
    if dest <= 0:
        return f"{label} {cur}/?"
    remain = max(0, dest - cur)
    done = bool(item.get("rewarded")) or cur >= dest
    if done:
        return f"{label} {cur}/{dest}✓"
    return f"{label} {cur}/{dest} 剩{remain}"
