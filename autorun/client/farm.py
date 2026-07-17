"""Stage farming loop with drop stats, stay-mode, and re-login recovery."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .apis import battle as battle_api
from .drops import BattleDropResult, DropStats, parse_battle_end
from .http_client import ApiError
from .session import GameSession
from .runtime_state import STATE, RuntimeState


def _extract_spawn_waves(spawn) -> list[tuple[int, list[str]]]:
    waves: list[tuple[int, list[str]]] = []
    if not isinstance(spawn, dict):
        return waves
    entries = spawn.get("_list") or spawn.get("list") or []
    if not isinstance(entries, list):
        return waves
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        wave_no = int(entry.get("_wave", entry.get("wave", 0)) or 0)
        mobs: list[str] = []
        mob_list = entry.get("_mobList") or entry.get("mobList") or entry
        if isinstance(mob_list, dict):
            items = mob_list.get("_list") or mob_list.get("list") or []
        elif isinstance(mob_list, list):
            items = mob_list
        else:
            items = []
        for m in items:
            if isinstance(m, dict):
                uid = m.get("_uid") or m.get("uid") or m.get("_mobUID")
                if uid:
                    mobs.append(str(uid))
            elif isinstance(m, str):
                mobs.append(m)
        if mobs:
            waves.append((wave_no, mobs))
    return waves


def _is_negative_code(code) -> bool:
    try:
        return code is not None and int(code) < 0
    except Exception:
        return False


# Server rejects non-frontier battle/start with these codes.
# Retarget to current battle_info instead of long recover.
INVALID_PROGRESS_CODES = {-28002, -28003, -28004, -11006}

# Session invalidated (another device/client logged in). Wait 10 min then re-login.
SESSION_KICK_CODES = {-19006}


def _is_invalid_progress_code(code) -> bool:
    try:
        return code is not None and int(code) in INVALID_PROGRESS_CODES
    except Exception:
        return False


def _is_session_kick_code(code) -> bool:
    try:
        return code is not None and int(code) in SESSION_KICK_CODES
    except Exception:
        return False


@dataclass
class FarmTarget:
    region: int = 1
    stage: int = 23
    sector: int = 2
    repeat: int = 0


@dataclass
class FarmConfig:
    # Capture baseline: main story stage 23 / sector 2.
    start_stage: int = 23
    start_sector: int = 2
    region: int = 1
    count: int = 10
    min_stage: int = 1
    sleep_sec: float = 0.2
    damage: str = "0"
    # Prefer server current progress if still on same stage.
    prefer_server_progress: bool = True
    # Stay on current startable frontier from login; follow server after clear.
    # Note: server only accepts current frontier with repeat=0; previous stage/sector fails.
    stay: bool = False
    # On fail code < 0: wait then re-login and resume.
    recover_wait_sec: float = 60.0  # default for non-kick fails
    stats_path: str = "drop_stats.json"


@dataclass
class FarmRunner:
    session: GameSession
    config: FarmConfig = field(default_factory=FarmConfig)
    stats: DropStats = field(default_factory=DropStats)
    log: Callable[[str], None] = print
    state: RuntimeState = field(default_factory=lambda: STATE)
    # Locked stage for --stay mode.
    _stay_target: Optional[FarmTarget] = None

    def __post_init__(self) -> None:
        # Tee log into runtime state events.
        user_log = self.log

        def _tee(msg: str) -> None:
            try:
                self.state.add_event(msg)
            except Exception:
                pass
            user_log(msg)

        self.log = _tee

    def _frontier_from_info(self, info: dict | None = None) -> FarmTarget:
        info = info if info is not None else (self.session.battle_info or {})
        region = int(info.get("_region", self.config.region) or self.config.region)
        stage = int(info.get("_stage", self.config.start_stage) or self.config.start_stage or 1)
        sector = max(1, int(info.get("_sector", self.config.start_sector) or self.config.start_sector or 1))
        # Server only accepts the current frontier with repeat=0.
        return FarmTarget(region=region, stage=stage, sector=sector, repeat=0)

    def refresh_frontier(self) -> FarmTarget:
        """Re-fetch init-data and return current startable frontier."""
        try:
            self.session.init_game_data()
        except Exception as exc:
            self.log(f"[-] refresh frontier failed: {exc}")
        target = self._frontier_from_info()
        if self.config.stay:
            self._stay_target = FarmTarget(
                region=target.region,
                stage=target.stage,
                sector=target.sector,
                repeat=0,
            )
        self.state.set_target(
            region=target.region, stage=target.stage, sector=target.sector, repeat=target.repeat
        )
        self.log(
            f"[*] retarget frontier stage={target.stage} sector={target.sector} "
            f"region={target.region} repeat={target.repeat}"
        )
        return target

    def resolve_start_target(self) -> FarmTarget:
        info = self.session.battle_info or {}
        server_stage = int(info.get("_stage", 0) or 0)
        server_sector = int(info.get("_sector", 0) or 0)
        server_repeat = int(info.get("_repeat", 0) or 0)
        region = int(info.get("_region", self.config.region) or self.config.region)

        if self.config.stay:
            # Only the current server frontier is startable (repeat must be 0).
            # Explicit --stage/--sector can override, but invalid targets are
            # auto-retargeted on -28002/-28003/-28004.
            if self.config.prefer_server_progress and server_stage:
                target = self._frontier_from_info(info)
            else:
                target = FarmTarget(
                    region=region,
                    stage=int(self.config.start_stage or server_stage or 1),
                    sector=max(1, int(self.config.start_sector or server_sector or 1)),
                    repeat=0,
                )
            self._stay_target = FarmTarget(
                region=target.region,
                stage=target.stage,
                sector=target.sector,
                repeat=0,
            )
            self.log(
                f"[*] stay mode lock stage={target.stage} sector={target.sector} "
                f"region={target.region} repeat=0"
            )
            self.state.set_target(
                region=target.region, stage=target.stage, sector=target.sector, repeat=target.repeat
            )
            self.state.set_status("stay-lock")
            return target

        stage = self.config.start_stage
        sector = max(1, self.config.start_sector)
        repeat = 0

        if self.config.prefer_server_progress and server_stage:
            # Prefer the only startable frontier when available.
            stage = server_stage
            sector = max(1, server_sector or 1)
            repeat = 0
        return FarmTarget(region=region, stage=stage, sector=sector, repeat=repeat)

    def run_one_clear(self, target: FarmTarget) -> BattleDropResult:
        self.log(
            f"[*] start region={target.region} stage={target.stage} "
            f"sector={target.sector} repeat={target.repeat}"
        )
        self.state.set_target(
            region=target.region, stage=target.stage, sector=target.sector, repeat=target.repeat
        )
        self.state.set_status("battle")
        start = self.session.battle_start(
            region=target.region,
            stage=target.stage,
            sector=target.sector,
            repeat=target.repeat,
            wave=0,
            state=battle_api.STATE_FORWARD,
            attr=battle_api.ATTR_PLAY,
        )
        code = start.get("_code", 0)
        if code not in (0, None):
            self.log(
                f"[-] battle/start fail code={code} "
                f"msg={start.get('_message')} details={start.get('_details')}"
            )
            return BattleDropResult(
                ok=False,
                region=target.region,
                stage=target.stage,
                sector=target.sector,
                code=code,
                message=start.get("_details") or start.get("_message"),
                raw_end=start,
            )

        waves = _extract_spawn_waves(start.get("_spawnMobList") or {})
        self.log(f"[+] start ok waves={[(w, len(m)) for w, m in waves]}")
        for wave_no, mobs in waves:
            km = self.session.battle_kill_mob(
                wave=wave_no,
                mob_uid_list=mobs,
                reason=battle_api.REASON_NONE,
            )
            kcode = km.get("_code", 0)
            if kcode not in (0, None):
                self.log(f"[-] kill-mob wave={wave_no} code={kcode} msg={km.get('_message')}")
                return BattleDropResult(
                    ok=False,
                    region=target.region,
                    stage=target.stage,
                    sector=target.sector,
                    code=kcode,
                    message=km.get("_details") or km.get("_message"),
                    raw_end=km,
                )

        end = self.session.battle_end(
            region=target.region,
            reason=battle_api.REASON_CLEAR,
            state=battle_api.STATE_FORWARD,
            damage=self.config.damage,
        )
        result = parse_battle_end(
            end,
            region=target.region,
            stage=target.stage,
            sector=target.sector,
        )
        if result.ok:
            labels = ", ".join(f"{d.label}x{d.count}" for d in result.drops) or "(no drops)"
            self.log(f"[+] clear ok drops={labels}")
            self.state.set_last_drops(labels)
            self.state.set_last_error("")
            self.state.set_status("clear-ok")
            # Always cache server progress so next start uses the valid frontier.
            if result.battle_after:
                self.session.battle_info = result.battle_after
        else:
            self.log(f"[-] battle/end fail code={result.code} msg={result.message}")
            self.state.set_last_error(f"code={result.code} {result.message}")
            self.state.set_status("battle-fail")
        return result

    def _fallback_target(self, target: FarmTarget) -> Optional[FarmTarget]:
        if self.config.stay:
            # Stay never walks backward; refresh to server frontier instead.
            return self.refresh_frontier()
        if target.stage <= self.config.min_stage and target.sector <= 1:
            return None
        # Push mode: try previous sector/stage, but always with repeat=0 first.
        # If previous content is not startable, invalid-progress retarget will fix it.
        if target.sector > 1:
            return FarmTarget(
                region=target.region,
                stage=target.stage,
                sector=target.sector - 1,
                repeat=0,
            )
        return FarmTarget(
            region=target.region,
            stage=target.stage - 1,
            sector=1,
            repeat=0,
        )

    def _advance_target(self, target: FarmTarget, result: BattleDropResult) -> FarmTarget:
        after = result.battle_after or self.session.battle_info or {}
        if after:
            nxt = FarmTarget(
                region=int(after.get("_region", target.region) or target.region),
                stage=int(after.get("_stage", target.stage) or target.stage),
                sector=max(1, int(after.get("_sector", target.sector) or target.sector)),
                repeat=0,
            )
        else:
            nxt = FarmTarget(
                region=target.region,
                stage=target.stage,
                sector=target.sector,
                repeat=0,
            )
        if self.config.stay:
            # Server advances after Clear; re-lock to the new startable frontier.
            self._stay_target = FarmTarget(
                region=nxt.region, stage=nxt.stage, sector=nxt.sector, repeat=0
            )
        return nxt

    def _recover_session(self, reason: str, *, wait_sec: float | None = None) -> bool:
        """Wait then full re-auth pipeline (auth -> login -> init-data)."""
        wait_sec = self.config.recover_wait_sec if wait_sec is None else wait_sec
        wait_sec = max(0.0, float(wait_sec))
        self.log(
            f"[!] recover triggered: {reason}; "
            f"sleep {wait_sec:.0f}s then re-auth"
        )
        self.state.set_recover(f"wait {wait_sec:.0f}s")
        self.state.set_status("recover-wait")
        # Sleep in chunks so Ctrl+C stays responsive.
        end_at = time.time() + wait_sec
        try:
            while time.time() < end_at:
                left = end_at - time.time()
                if left <= 0:
                    break
                if int(left) % 60 == 0 or left < 60:
                    self.log(f"[*] recover wait... {left:.0f}s left")
                time.sleep(min(10.0, left))
        except KeyboardInterrupt:
            self.log("[*] recover wait interrupted")
            raise

        self.log("[*] re-auth pipeline (public-key -> auth -> login -> init-data)...")
        self.state.set_recover("re-auth")
        self.state.set_status("re-auth")
        try:
            # Must re-auth: -19006 invalidates sessionKey from previous auth.
            pipe = self.session.reauth_pipeline()
            info = self.session.battle_info or {}
            self.log(
                f"[+] re-auth ok session={self.session.client.session_key} "
                f"battle={info}"
            )
            self.state.set_account(
                public_uid=str(self.session.auth_info.get("_publicUid") or ""),
                server_num=self.session.auth_info.get("_serverNum"),
                session_key=str(self.session.client.session_key or ""),
            )
            self.state.set_recover("idle")
            self.state.set_status("ready")
            if self.config.stay:
                # After re-auth, retarget the only startable frontier.
                self.refresh_frontier()
            return True
        except Exception as exc:
            self.log(f"[-] re-auth failed: {exc}")
            return False

    def farm(self) -> DropStats:
        target = self.resolve_start_target()
        infinite = self.config.count <= 0
        count_desc = "infinite" if infinite else str(self.config.count)
        mode = "stay" if self.config.stay else "push"
        self.log(f"[*] farm begin mode={mode} target={target} count={count_desc}")
        self.state.set_mode(mode, count_desc)
        self.state.set_target(region=target.region, stage=target.stage, sector=target.sector, repeat=target.repeat)
        self.state.set_status("farming")
        i = 0
        stay_fail_streak = 0

        try:
            while infinite or i < self.config.count:
                try:
                    result = self.run_one_clear(target)
                except ApiError as exc:
                    self.log(f"[-] api error: {exc}")
                    body = getattr(exc, "body", None)
                    code = None
                    if isinstance(body, dict):
                        code = body.get("_code")
                    result = BattleDropResult(
                        ok=False,
                        region=target.region,
                        stage=target.stage,
                        sector=target.sector,
                        code=code if code is not None else getattr(exc, "status", None),
                        message=str(exc),
                        raw_end=body if isinstance(body, dict) else {},
                    )
                except Exception as exc:
                    self.log(f"[-] unexpected: {exc}")
                    result = BattleDropResult(
                        ok=False,
                        region=target.region,
                        stage=target.stage,
                        sector=target.sector,
                        message=str(exc),
                    )

                self.stats.add(result)
                summary = self.stats.summary()
                # summary drops is nested; flatten totals for TUI
                drop_totals = {k: v.get("total", 0) for k, v in (summary.get("drops") or {}).items()}
                self.state.set_stats(
                    runs=summary.get("runs", 0),
                    wins=summary.get("wins", 0),
                    fails=summary.get("fails", 0),
                    drop_totals=drop_totals,
                    stage_wins=summary.get("stage_wins") or {},
                )

                if not result.ok and _is_invalid_progress_code(result.code):
                    # Wrong stage/sector/repeat: refresh and jump to current frontier.
                    # Do NOT treat as long recover (no 10-minute wait).
                    self.log(
                        f"[!] invalid progress code={result.code} msg={result.message}; "
                        f"retarget frontier"
                    )
                    self.state.set_last_error(f"code={result.code} {result.message}")
                    self.state.set_status("retarget")
                    target = self.refresh_frontier()
                    stay_fail_streak = 0
                    self.stats.save(self.config.stats_path)
                    if self.config.sleep_sec > 0:
                        time.sleep(self.config.sleep_sec)
                    continue

                # Duplicate login / session kicked by another client: wait 10 min.
                if not result.ok and _is_session_kick_code(result.code):
                    reason = f"session kick code={result.code} msg={result.message}"
                    self.state.set_last_error(reason)
                    if self._recover_session(reason, wait_sec=600.0):
                        target = self.refresh_frontier()
                        stay_fail_streak = 0
                        self.log(f"[*] resume after kick at {target}")
                        self.stats.save(self.config.stats_path)
                        continue
                    self.log("[-] re-auth after kick failed; stop")
                    break

                # Other negative business codes -> wait + full re-auth.
                need_recover = (not result.ok) and _is_negative_code(result.code)
                if need_recover:
                    reason = f"fail code={result.code} msg={result.message}"
                    if self._recover_session(reason):
                        if self.config.stay:
                            target = self.refresh_frontier()
                        else:
                            # Prefer server frontier after re-login.
                            info = self.session.battle_info or {}
                            if info.get("_stage"):
                                target = self._frontier_from_info(info)
                        self.log(f"[*] resume command at {target}")
                        self.stats.save(self.config.stats_path)
                        continue
                    self.log("[-] recover failed; stop")
                    break

                if result.ok:
                    stay_fail_streak = 0
                    i += 1
                    target = self._advance_target(target, result)
                    self.state.set_progress(i)
                    self.state.set_target(
                        region=target.region, stage=target.stage, sector=target.sector, repeat=target.repeat
                    )
                    self.log(
                        f"[*] progress {i}"
                        + ("" if infinite else f"/{self.config.count}")
                        + f" next={target.stage}-{target.sector} repeat={target.repeat}"
                    )
                else:
                    if self.config.stay:
                        stay_fail_streak += 1
                        nxt = self._fallback_target(target)
                        if nxt is None:
                            self.log("[-] stay cannot retry; stop")
                            break
                        target = nxt
                        if stay_fail_streak >= 3:
                            if self._recover_session(
                                f"stay fail streak={stay_fail_streak} code={result.code}"
                            ):
                                stay_fail_streak = 0
                                target = self.refresh_frontier()
                                continue
                            break
                    else:
                        nxt = self._fallback_target(target)
                        if nxt is None:
                            self.log("[-] cannot fallback further; stop")
                            break
                        self.log(
                            f"[*] fallback {target.stage}-{target.sector} -> "
                            f"{nxt.stage}-{nxt.sector} (repeat={nxt.repeat})"
                        )
                        target = nxt
                        if not infinite:
                            i += 1

                if self.config.sleep_sec > 0:
                    time.sleep(self.config.sleep_sec)
                self.stats.save(self.config.stats_path)
        except KeyboardInterrupt:
            self.log("[*] interrupted by user (Ctrl+C)")

        self.log("[*] farm done")
        self.log(self.stats.pretty())
        self.state.set_status("done")
        self.stats.save(self.config.stats_path)
        return self.stats
