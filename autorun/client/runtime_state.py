"""Thread-safe runtime snapshot for TUI / logging."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional


# Client BattleInfoParam.sectorCount — 游戏内显示的「第 N 关」累计号:
#   N = maxStage * maxSector * repeat + maxSector * (stage - 1) + sector
# 主线表: 60 stages × 10 sectors per stage (= 600 关/周目)
# 例: stage=31, sector=4, repeat=1 → 60*10*1 + 10*30 + 4 = 904
DEFAULT_MAX_STAGE = 60
DEFAULT_MAX_SECTOR = 10


def ui_stage_no(
    stage: int,
    sector: int,
    repeat: int = 0,
    *,
    max_stage: int = DEFAULT_MAX_STAGE,
    max_sector: int = DEFAULT_MAX_SECTOR,
) -> int:
    """游戏内关卡号（UI「904关」），与 BattleInfoParam.sectorCount 一致。"""
    stage = int(stage or 0)
    sector = int(sector or 0)
    repeat = int(repeat or 0)
    if stage <= 0 or sector <= 0:
        return 0
    return max_stage * max_sector * repeat + max_sector * (stage - 1) + sector


@dataclass
class HttpEvent:
    ts: float
    method: str
    url: str
    status: Optional[int] = None
    ms: Optional[float] = None
    phase: str = "req"  # req | resp


@dataclass
class RuntimeState:
    # session / account
    public_uid: str = ""
    server_num: Any = ""
    session_key: str = ""
    mode: str = "-"  # stay / push / single
    status: str = "idle"
    recover: str = "idle"

    # battle target
    region: int = 0
    stage: int = 0
    sector: int = 0
    repeat: int = 0

    # counters
    loop_i: int = 0
    loop_total: str = "-"  # number or infinite
    wins: int = 0
    fails: int = 0
    runs: int = 0
    # small-mob kills (noboss / kill-mob cumulative)
    mobs_killed: int = 0
    mobs_killed_last: int = 0

    # promotion (升阶) quests: server base at login + local kill adds
    promotion_rank: int = 0
    promotion_items: list = field(default_factory=list)

    # last results
    last_drops: str = "-"
    last_error: str = ""
    last_http: str = "-"

    # drop totals label -> count
    drop_totals: dict[str, int] = field(default_factory=dict)
    stage_wins: dict[str, int] = field(default_factory=dict)

    events: Deque[str] = field(default_factory=lambda: deque(maxlen=40))
    http_events: Deque[HttpEvent] = field(default_factory=lambda: deque(maxlen=20))

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "public_uid": self.public_uid,
                "server_num": self.server_num,
                "session_key": self.session_key,
                "mode": self.mode,
                "status": self.status,
                "recover": self.recover,
                "region": self.region,
                "stage": self.stage,
                "sector": self.sector,
                "repeat": self.repeat,
                "ui_stage": ui_stage_no(self.stage, self.sector, self.repeat),
                "loop_i": self.loop_i,
                "loop_total": self.loop_total,
                "wins": self.wins,
                "fails": self.fails,
                "runs": self.runs,
                "mobs_killed": self.mobs_killed,
                "mobs_killed_last": self.mobs_killed_last,
                "promotion_rank": self.promotion_rank,
                "promotion_items": [dict(x) for x in self.promotion_items],
                "promotion_lines": self._promotion_lines_unlocked(),
                "last_drops": self.last_drops,
                "last_error": self.last_error,
                "last_http": self.last_http,
                "drop_totals": dict(self.drop_totals),
                "stage_wins": dict(self.stage_wins),
                "events": list(self.events),
                "http_events": list(self.http_events),
                "ts": time.time(),
            }

    def set_account(self, *, public_uid: str = "", server_num: Any = "", session_key: str = "") -> None:
        with self._lock:
            if public_uid:
                self.public_uid = public_uid
            if server_num != "":
                self.server_num = server_num
            if session_key:
                self.session_key = session_key

    def set_target(self, *, region: int, stage: int, sector: int, repeat: int = 0) -> None:
        with self._lock:
            self.region = region
            self.stage = stage
            self.sector = sector
            self.repeat = repeat

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

    def set_recover(self, recover: str) -> None:
        with self._lock:
            self.recover = recover

    def set_mode(self, mode: str, loop_total: str = "-") -> None:
        with self._lock:
            self.mode = mode
            self.loop_total = loop_total

    def set_progress(self, i: int) -> None:
        with self._lock:
            self.loop_i = i

    def set_stats(self, *, runs: int, wins: int, fails: int, drop_totals: dict | None = None, stage_wins: dict | None = None) -> None:
        with self._lock:
            self.runs = runs
            self.wins = wins
            self.fails = fails
            if drop_totals is not None:
                self.drop_totals = dict(drop_totals)
            if stage_wins is not None:
                self.stage_wins = dict(stage_wins)

    def set_promotion(self, *, rank: int, items: list | None = None) -> None:
        """Snapshot promotion rank + quests from init-data (server base)."""
        with self._lock:
            self.promotion_rank = int(rank or 0)
            self.promotion_items = [dict(x) for x in (items or [])]

    def _promotion_lines_unlocked(self) -> list[str]:
        lines: list[str] = []
        for it in self.promotion_items:
            dest = int(it.get("dest") or 0)
            base = int(it.get("base") or 0)
            local = int(it.get("local") or 0)
            cur = base + local
            if dest > 0:
                cur = min(cur, dest)
            remain = max(0, dest - cur) if dest else 0
            label = it.get("label") or f"Q{it.get('key')}"
            done = bool(it.get("rewarded")) or (dest > 0 and cur >= dest)
            mark = "✓" if done else f"剩{remain}"
            lines.append(f"{label} {cur}/{dest} {mark}")
        return lines

    def add_mobs_killed(self, n: int) -> None:
        """Accumulate successfully killed small-mob count (e.g. noboss).

        Also advances promotion kill-quest local progress (no re-fetch).
        """
        n = int(n or 0)
        if n <= 0:
            return
        with self._lock:
            self.mobs_killed_last = n
            self.mobs_killed += n
            for it in self.promotion_items:
                if it.get("track_kills"):
                    it["local"] = int(it.get("local") or 0) + n

    def set_last_drops(self, text: str) -> None:
        with self._lock:
            self.last_drops = text

    def set_last_error(self, text: str) -> None:
        with self._lock:
            self.last_error = text

    def add_event(self, msg: str) -> None:
        line = msg.rstrip()
        if not line:
            return
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.events.appendleft(f"[{ts}] {line}")

    def add_http_req(self, url: str, method: str = "POST") -> None:
        with self._lock:
            self.http_events.appendleft(HttpEvent(ts=time.time(), method=method, url=url, phase="req"))
            self.last_http = f"{method} {url}"
            self.status = "http"

    def add_http_resp(self, url: str, status: int, ms: float, method: str = "POST") -> None:
        with self._lock:
            self.http_events.appendleft(
                HttpEvent(ts=time.time(), method=method, url=url, status=status, ms=ms, phase="resp")
            )
            self.last_http = f"HTTP {status} {url} ({ms:.0f}ms)"


# process-wide default
STATE = RuntimeState()
