"""Battle drop parsing and multi-run statistics."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable


REWARD_TYPE = {
    0: "None",
    1: "Goods",
    2: "Character",
    3: "Item",
    4: "Skill",
    5: "Digivice",
    6: "DigiviceForm",
    7: "Rune",
    8: "Soul",
    9: "SupporterCharacter",
    10: "HolyWeaponCharacter",
    11: "BackAccessoryParts",
    12: "Costume",
    13: "DimensionalBoxDeco",
    14: "TimeReward",
    15: "Cooking",
    16: "ChatBalloon",
    17: "Portrait",
    18: "Frame",
}

# E_GOODS_TYPE subset used in rewards.
GOODS_TYPE = {
    0: "Gold",
    1: "FreeCrystal",
    2: "CashCrystal",
    3: "Crystal",
    4: "FreeDiamond",
    5: "CashDiamond",
    6: "Diamond",
    7: "Exp",
    50: "ItemTicket",
    51: "SkillTicket",
    52: "MemberTicket",
    53: "SoulTicket",
    54: "AccelTicket",
}


def _list_of(container: Any) -> list:
    if container is None:
        return []
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in ("_list", "list"):
            val = container.get(key)
            if isinstance(val, list):
                return val
    return []


def reward_label(reward_type: int, value: Any) -> str:
    type_name = REWARD_TYPE.get(int(reward_type), f"Type{reward_type}")
    if int(reward_type) == 1:
        try:
            goods = GOODS_TYPE.get(int(value), f"Goods{value}")
        except Exception:
            goods = f"Goods{value}"
        return f"{type_name}/{goods}"
    return f"{type_name}/{value}"


@dataclass
class DropItem:
    reward_type: int
    value: Any
    count: int
    label: str
    source: str = "rewardList"
    meta: dict = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.reward_type}:{self.value}"


@dataclass
class BattleDropResult:
    ok: bool
    region: int
    stage: int
    sector: int
    code: int | None = None
    message: str | None = None
    drops: list[DropItem] = field(default_factory=list)
    battle_after: dict = field(default_factory=dict)
    raw_end: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "region": self.region,
            "stage": self.stage,
            "sector": self.sector,
            "code": self.code,
            "message": self.message,
            "drops": [asdict(d) for d in self.drops],
            "battle_after": self.battle_after,
        }


def parse_reward_all_list(reward_all: Any, *, stage: int | None = None) -> list[DropItem]:
    """Extract drop entries from battle/end `_rewardAllList`."""
    drops: list[DropItem] = []
    if not isinstance(reward_all, dict):
        return drops

    # Instant rewards.
    for item in _list_of(reward_all.get("_rewardList")):
        if not isinstance(item, dict):
            continue
        rtype = int(item.get("_type", 0) or 0)
        value = item.get("_value", 0)
        count = int(item.get("_count", 0) or 0)
        drops.append(
            DropItem(
                reward_type=rtype,
                value=value,
                count=count,
                label=reward_label(rtype, value),
                source="rewardList",
                meta={"metaList": item.get("_metaList")},
            )
        )

    # Entity lists (only count newly listed entities when present).
    entity_map = {
        "_itemList": (3, "itemList"),
        "_skillList": (4, "skillList"),
        "_runeList": (7, "runeList"),
        "_soulList": (8, "soulList"),
        "_memberList": (2, "memberList"),
        "_supporterCharacterList": (9, "supporterCharacterList"),
        "_holyWeaponCharacterList": (10, "holyWeaponCharacterList"),
        "_backAccessoryPartsList": (11, "backAccessoryPartsList"),
        "_costumeList": (12, "costumeList"),
        "_dimensionalBoxDecoList": (13, "dimensionalBoxDecoList"),
        "_timeRewardList": (14, "timeRewardList"),
        "_cookingRecipeList": (15, "cookingRecipeList"),
        "_chatBalloonList": (16, "chatBalloonList"),
        "_profilePortraitList": (17, "portraitList"),
        "_profileFrameList": (18, "frameList"),
        "_decorationKeyList": (0, "decorationKeyList"),
    }
    for field_name, (rtype, source) in entity_map.items():
        for item in _list_of(reward_all.get(field_name)):
            if not isinstance(item, dict):
                continue
            value = (
                item.get("_key")
                or item.get("_uid")
                or item.get("_type")
                or item.get("_value")
                or item.get("_id")
                or "?"
            )
            drops.append(
                DropItem(
                    reward_type=rtype,
                    value=value,
                    count=1,
                    label=reward_label(rtype, value) if rtype else f"Entity/{value}",
                    source=source,
                    meta=item,
                )
            )
    return drops


def parse_battle_end(end: dict, *, region: int, stage: int, sector: int) -> BattleDropResult:
    code = end.get("_code", 0)
    ok = code in (0, None)
    drops = parse_reward_all_list(end.get("_rewardAllList"), stage=stage) if ok else []
    battle_after = end.get("_battle") if isinstance(end.get("_battle"), dict) else {}
    return BattleDropResult(
        ok=ok,
        region=region,
        stage=stage,
        sector=sector,
        code=code,
        message=end.get("_message") or end.get("_details"),
        drops=drops,
        battle_after=battle_after,
        raw_end=end,
    )


@dataclass
class DropStats:
    """Aggregate drops across many battles."""

    runs: int = 0
    wins: int = 0
    fails: int = 0
    # label -> total count
    totals: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # label -> times appeared
    hit_runs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # stage-sector -> wins
    stage_wins: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    history: list[dict] = field(default_factory=list)

    def add(self, result: BattleDropResult) -> None:
        self.runs += 1
        entry = result.to_dict()
        entry["ts"] = int(time.time())
        self.history.append(entry)
        if not result.ok:
            self.fails += 1
            return
        self.wins += 1
        stage_key = f"{result.stage}-{result.sector}"
        self.stage_wins[stage_key] += 1
        seen = set()
        for drop in result.drops:
            self.totals[drop.label] += int(drop.count or 0)
            seen.add(drop.label)
        for label in seen:
            self.hit_runs[label] += 1

    def summary(self) -> dict:
        rates = {}
        for label, total in sorted(self.totals.items(), key=lambda x: (-x[1], x[0])):
            hits = self.hit_runs.get(label, 0)
            rates[label] = {
                "total": total,
                "hit_runs": hits,
                "avg_per_win": (total / self.wins) if self.wins else 0.0,
                "drop_rate": (hits / self.wins) if self.wins else 0.0,
            }
        return {
            "runs": self.runs,
            "wins": self.wins,
            "fails": self.fails,
            "win_rate": (self.wins / self.runs) if self.runs else 0.0,
            "stage_wins": dict(self.stage_wins),
            "drops": rates,
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": self.summary(),
            "history": self.history[-500:],  # cap file size
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def pretty(self) -> str:
        s = self.summary()
        lines = [
            f"runs={s['runs']} wins={s['wins']} fails={s['fails']} win_rate={s['win_rate']:.1%}",
            f"stage_wins={s['stage_wins']}",
            "drops:",
        ]
        if not s["drops"]:
            lines.append("  (none)")
        for label, info in s["drops"].items():
            lines.append(
                f"  {label}: total={info['total']} "
                f"hit={info['hit_runs']}/{s['wins']} "
                f"avg={info['avg_per_win']:.2f} rate={info['drop_rate']:.1%}"
            )
        return "\n".join(lines)
