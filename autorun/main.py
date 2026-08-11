#!/usr/bin/env python3
"""DIGIMON UP autorun: import account or auto farm."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client.account_store import apply_account_to_config, import_input_file, load_account_file
from client.qmd_auto import run_auto_once
from client.farm import FarmConfig, FarmRunner
from client.promotion_care import build_promotion_snapshot, format_promo_line
from client.item_spawner_care import (
    DEFAULT_FILTER_GRADE,
    DEFAULT_FILTER_MATCH_COUNT,
    DEFAULT_FILTER_STAT_TYPE_LIST,
    run_zb,
)
from client.pvp_care import run_pvp
from client.dungeon_care import (
    ADVANCING_DUNGEON_CONFIG,
    advancing_dungeon_progress,
    resolve_fb_key,
    rotating_trial_progress,
    run_advancing_dungeon_clear,
    run_advancing_dungeon_sweep,
    run_fb,
)
from client.heartbeat import HeartbeatService
from client.runtime_state import STATE, ui_stage_no
from client.session import GameSession
from client.tui import FarmTUI

DUMP_PATH = "last_run.json"
STATS_PATH = "drop_stats.json"


def cmd_ts() -> int:
    """Interactive Textual TUI for 数码世界 / 探索 (alias: mine)."""
    try:
        from client.mine_tui import run_mine_tui
    except ImportError as exc:
        print("[-] Textual is required for ts TUI: pip install textual")
        print(f"    detail: {exc}")
        return 2
    session = _load_session()
    session.client.log_enabled = False
    print("[*] ts (数码世界): login then click cells (drill / dash / claim)")
    return run_mine_tui(session, http_log=False)



def _load_session() -> GameSession:
    session = GameSession()
    saved = load_account_file()
    if saved:
        apply_account_to_config(session.config, saved)
        session.client.data_no = session.config.account.data_no
    return session


def cmd_import(input_path: str) -> int:
    imported = import_input_file(input_path)
    print(
        f"[+] imported account from {input_path} -> {imported.get('saved_path')} "
        f"client_id={imported.get('client_id')} "
        f"device_id={imported.get('device_id')} "
        f"server={imported.get('preferred_server_num')}"
    )
    return 0


def cmd_auto() -> int:
    """One-shot account maintenance. Schedule via crontab hourly."""
    session_holder = {"s": None}

    def make_session():
        s = _load_session()
        session_holder["s"] = s
        return s

    print("[*] auto: one-shot maintenance including guild (crontab hourly; no cooldown sleep)")
    return run_auto_once(make_session, log=print, http_log=True)



def cmd_runloop(*, no_boss: bool = False, delay: float = 0.0, count: int | None = None) -> int:
    """TUI + stay farm on current login frontier.

    no_boss: kill only small-mob waves, fail-end without boss; re-open same stage.
    delay: seconds before each battle/* request (sets REQUEST_DELAY_SEC; default 0).
    count: stop after N killed mobs; None/0 = infinite.
    """
    from client.apis import battle as battle_api

    applied = battle_api.set_request_delay(delay)
    print(f"[*] battle request delay={applied:g}s")
    session = _load_session()
    session.client.log_enabled = False
    session.client.state = STATE
    hb: HeartbeatService | None = None
    result: dict = {"ok": False, "mode": "runloop"}

    try:
        pipe = session.run_login_pipeline()
        result["login_pipeline"] = {
            "session_key": session.client.session_key,
            "auth_code": session.auth_info.get("_code"),
            "public_uid": session.auth_info.get("_publicUid"),
            "server_num": session.auth_info.get("_serverNum"),
            "login": session.login_info,
            "battle_info": session.battle_info,
            "init_keys": pipe.get("init_keys"),
        }
        print("[+] login pipeline ok")
        STATE.set_account(
            public_uid=str(session.auth_info.get("_publicUid") or ""),
            server_num=session.auth_info.get("_serverNum"),
            session_key=str(session.client.session_key or ""),
        )
        bi = session.battle_info or {}
        if bi:
            STATE.set_target(
                region=int(bi.get("_region") or 0),
                stage=int(bi.get("_stage") or 0),
                sector=int(bi.get("_sector") or 0),
                repeat=int(bi.get("_repeat") or 0),
            )
        STATE.set_status("ready")
        STATE.add_event("login pipeline ok")

        promo = build_promotion_snapshot(session.init_data)
        STATE.set_promotion(rank=int(promo.get("rank") or 0), items=promo.get("items") or [])
        if promo.get("rank"):
            lines = [format_promo_line(it) for it in (promo.get("items") or [])]
            STATE.add_event(
                f"[*] 升阶 {promo.get('rank')}→{int(promo.get('rank') or 0)+1} "
                + " | ".join(lines)
            )
            print(
                f"[*] 升阶 {promo.get('rank')}→{int(promo.get('rank') or 0)+1} "
                + " | ".join(lines)
            )

        hb = HeartbeatService(session, log=STATE.add_event)
        hb.start()
        result["heartbeat"] = {"interval_sec": 60}

        acc = session.config.account
        info = session.battle_info or {}
        login_stage = int(info.get("_stage") or acc.capture_stage)
        login_sector = max(1, int(info.get("_sector") or acc.capture_sector or 1))
        login_region = int(info.get("_region") or acc.capture_region or 1)
        login_repeat = int(info.get("_repeat") or 0)
        max_mobs = int(count or 0)
        count_desc = "infinite" if max_mobs <= 0 else f"mobs/{max_mobs}"
        print(
            f"[*] runloop: TUI + stay on login frontier "
            f"{login_stage}-{login_sector} region={login_region} "
            f"repeat={login_repeat} ui_stage={ui_stage_no(login_stage, login_sector, login_repeat)}"
            f"{' noboss' if no_boss else ''}"
            f" count={count_desc}"
            f" delay={applied:g}s"
        )

        cfg = FarmConfig(
            start_stage=login_stage,
            start_sector=login_sector,
            region=login_region,
            count=0,  # infinite runs; stop by max_mobs when set
            max_mobs=max_mobs,  # 0 = no mob limit
            min_stage=1,
            sleep_sec=0.2,
            damage="0",
            prefer_server_progress=True,
            stay=True,
            no_boss=bool(no_boss),
            recover_wait_sec=60.0,
            stats_path=STATS_PATH,
        )
        runner = FarmRunner(session=session, config=cfg, state=STATE)

        done = threading.Event()
        err_box: dict = {}

        def _worker() -> None:
            try:
                runner.log = STATE.add_event
                stats = runner.farm()
                err_box["stats"] = stats
            except Exception as exc:
                err_box["exc"] = exc
                STATE.add_event(f"[-] farm crashed: {exc}")
                STATE.set_status("error")
            finally:
                done.set()

        th = threading.Thread(target=_worker, name="farm", daemon=True)
        th.start()
        with FarmTUI(STATE) as ui:
            ui.run_until(done, interval=0.2)
        th.join(timeout=1)
        if "exc" in err_box:
            raise err_box["exc"]
        stats = err_box.get("stats") or runner.stats
        result["farm_summary"] = stats.summary()
        result["ok"] = stats.wins > 0
        print("[+] farm summary saved ->", STATS_PATH)
        return 0 if result["ok"] else 1
    except Exception as exc:
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        print("[-] FAILED:", exc)
        traceback.print_exc()
        return 1
    finally:
        try:
            if hb is not None:
                hb.stop()
        except Exception:
            pass
        dump_path = Path(DUMP_PATH)
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {dump_path}")



def cmd_zb(
    *,
    batches: int | None = None,
    total: int | None = None,
    count: int | None = None,
    info_only: bool = False,
    filter_grade: int = DEFAULT_FILTER_GRADE,
    filter_match: int = DEFAULT_FILTER_MATCH_COUNT,
    filter_stat: list[int] | None = None,
    workers: int = 1,
) -> int:
    """One-shot 开装备 (default: open all startup ItemTicket stock)."""
    progress_width = 20
    progress_open = False
    progress_total: int | None = None

    def show_progress(opened: int, total_at_start: int) -> None:
        nonlocal progress_open, progress_total
        if progress_total is None:
            progress_total = max(0, int(total_at_start))
        current = min(max(0, int(opened)), progress_total)
        filled = (
            min(progress_width, (current * progress_width) // progress_total)
            if progress_total > 0
            else progress_width
        )
        bar = ("█" * filled) + ("░" * (progress_width - filled))
        finished = current >= progress_total
        print(
            f"\r开装备｜[{bar}] 已开 {current}/{progress_total}",
            end="\n" if finished else "",
            flush=True,
        )
        progress_open = not finished

    session = _load_session()
    session.client.log_enabled = bool(info_only)
    result: dict = {"ok": False, "mode": "zb"}

    try:
        pipe = session.run_login_pipeline()
        result["login"] = {
            "session_key": session.client.session_key,
            "public_uid": session.auth_info.get("_publicUid"),
            "server_num": session.auth_info.get("_serverNum"),
        }
        if info_only:
            print(
                f"[+] login ok uid={result['login']['public_uid']} "
                f"server={result['login']['server_num']}"
            )
        stats = run_zb(
            session,
            batches=0 if info_only else batches,
            total=None if info_only or total is None else int(total),
            count=count,
            info_only=bool(info_only),
            filter_grade=int(filter_grade),
            filter_match_count=int(filter_match),
            filter_stat_type_list=(
                list(DEFAULT_FILTER_STAT_TYPE_LIST)
                if filter_stat is None
                else list(filter_stat)
            ),
            workers=max(1, int(workers or 1)),
            log=print if info_only else (lambda _message: None),
            progress=None if info_only else show_progress,
        )
        result.update(stats)
        if not info_only and not result.get("ok"):
            if progress_open:
                print()
                progress_open = False
            spawn = stats.get("spawn") if isinstance(stats, dict) else None
            runs = spawn.get("runs") if isinstance(spawn, dict) else None
            last_run = runs[-1] if isinstance(runs, list) and runs else {}
            code = last_run.get("code") if isinstance(last_run, dict) else None
            message = last_run.get("message") if isinstance(last_run, dict) else None
            detail = f"：code={code}" if code is not None else ""
            if message:
                detail += f" message={message}"
            print(f"开装备失败{detail}", file=sys.stderr)
    except Exception as exc:
        result["error"] = str(exc)
        if progress_open:
            print()
            progress_open = False
        print(f"开装备失败：{exc}", file=sys.stderr)
        if info_only:
            traceback.print_exc()
    finally:
        if progress_open:
            print()
            progress_open = False
        dump_path = Path("last_zb.json")
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if info_only:
            print(f"[*] wrote {dump_path}")

    return 0 if result.get("ok") else 1



def cmd_pvp() -> int:
    """One-shot arena PVP (regular+season): lowest combat until both tickets gone."""
    session = _load_session()
    session.client.log_enabled = True
    result: dict = {"ok": False, "mode": "pvp"}
    try:
        session.run_login_pipeline()
        print("[+] login pipeline ok")
        care = run_pvp(session, log=print)
        result["pvp"] = care
        result["ok"] = bool(care.get("ok"))
        print(
            f"[*] pvp summary battles={care.get('battles')} wins={care.get('wins')} "
            f"fails={care.get('fails')} tickets={care.get('ticket_start')}->{care.get('ticket_end')}"
        )
        return 0 if result["ok"] else 1
    except Exception as exc:
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        print("[-] FAILED:", exc)
        traceback.print_exc()
        return 1
    finally:
        dump_path = Path("last_pvp.json")
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {dump_path}")



def cmd_fb(alias: str, *, level: int | None = None) -> int:
    """Clear one dungeon: fb 1 / fb 2 / fb 3 (or raw key). Prefer sweep on cleared floors."""
    session = _load_session()
    session.client.log_enabled = True
    result: dict = {"ok": False, "mode": "fb", "alias": alias, "level": level}
    try:
        session.run_login_pipeline()
        print("[+] login pipeline ok")
        key = resolve_fb_key(alias)
        result["key"] = key
        care = run_fb(session, alias=alias, level=level, log=print)
        result["fb"] = care
        result["ok"] = bool(care.get("ok"))
        return 0 if result["ok"] else 1
    except Exception as exc:
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        print("[-] FAILED:", exc)
        traceback.print_exc()
        return 1
    finally:
        dump_path = Path("last_fb.json")
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {dump_path}")


def cmd_dungeon_advance(
    *,
    key: int | str,
    count: int | None = None,
    level: int | None = None,
) -> int:
    """Advance from live progress, or clear/sweep one explicitly selected floor."""
    requested_key = str(key).strip().lower()
    tower_mode = requested_key == "tower"
    resolved_key = None if tower_mode else int(key)
    fixed_level = int(level) if level is not None else None
    # A fixed floor defaults to one run.  Repeating it requires an explicit
    # count because subsequent sweeps may consume Trial reset tickets.
    run_limit = int(count) if count is not None else (1 if fixed_level is not None else None)
    if run_limit is not None and run_limit < 1:
        raise ValueError(f"dungeon count must be >= 1, got {run_limit}")
    if fixed_level is not None and fixed_level < 1:
        raise ValueError(f"dungeon level must be >= 1, got {fixed_level}")
    session = _load_session()
    session.client.log_enabled = False
    result: dict = {
        "ok": False,
        "mode": "dungeon_advance",
        "requested_key": requested_key,
        "key": resolved_key,
        "count": run_limit,
        "level": fixed_level,
        "completed": 0,
        "runs": [],
    }
    try:
        session.run_login_pipeline()
        if tower_mode:
            progress = rotating_trial_progress(session)
            resolved_key = int(progress["key"])
            result["key"] = resolved_key
            result["play_list"] = progress.get("play_list")
        else:
            progress = advancing_dungeon_progress(session, key=int(resolved_key))
        key = int(resolved_key)
        dungeon_label = f"dungeon tower（今日 key {key}）" if tower_mode else f"dungeon {key}"
        result["progress_before"] = progress
        cleared_before = int(progress["cleared_level"])
        current_level = int(progress["next_level"])
        max_level = progress.get("max_level")
        max_level = int(max_level) if max_level is not None else None
        progress_payload = progress.get("progress") or {}
        challenge_level = int(
            progress.get(
                "challenge_level",
                progress_payload.get(
                    "_challengeLevel",
                    progress_payload.get("challengeLevel", 0),
                ),
            )
            or 0
        )
        if fixed_level is not None:
            if max_level is not None and fixed_level > max_level:
                result["stop_reason"] = "level_above_max"
                print(
                    f"{dungeon_label} 指定第 {fixed_level} 关无效｜"
                    f"最高可指定第 {max_level} 关",
                    file=sys.stderr,
                )
                return 2
            if fixed_level > cleared_before + 1:
                result["stop_reason"] = "level_not_unlocked"
                print(
                    f"{dungeon_label} 指定第 {fixed_level} 关尚未解锁｜"
                    f"当前只能打第 {cleared_before + 1} 关",
                    file=sys.stderr,
                )
                return 2
            current_level = fixed_level
            sweep_mode = fixed_level <= cleared_before
        else:
            sweep_mode = max_level is not None and cleared_before >= max_level
        repeat_needs_reset = bool(
            sweep_mode and challenge_level >= current_level
        )
        if fixed_level is not None:
            action = "扫荡" if sweep_mode else "挑战"
            print(
                f"{dungeon_label} 当前已通关第 {cleared_before} 关｜"
                f"指定{action}第 {fixed_level} 关"
            )
        elif sweep_mode:
            print(f"{dungeon_label} 已到第 {max_level} 关｜开始重复刷第 {max_level} 关")
        else:
            print(
                f"{dungeon_label} 当前已通关第 {progress['cleared_level']} 关｜"
                f"下一关：第 {current_level} 关"
            )

        while True:
            if result["runs"]:
                session.ensure_heartbeat()
            if sweep_mode:
                care = run_advancing_dungeon_sweep(
                    session,
                    key=key,
                    level=current_level,
                    reset_before=repeat_needs_reset,
                    log=print,
                )
            else:
                care = run_advancing_dungeon_clear(
                    session,
                    key=key,
                    level=current_level,
                    log=print,
                )
            result["runs"].append(care)
            result["last_run"] = care
            if not care.get("ok"):
                result["stopped_level"] = current_level
                result["stop_reason"] = care.get("error") or "dungeon_failed"
                code = care.get("start_code")
                if code in (0, None):
                    code = care.get("kill_code")
                if code in (0, None):
                    code = care.get("end_code")
                if code in (0, None):
                    code = care.get("code")
                message = (
                    care.get("start_message")
                    or care.get("kill_message")
                    or care.get("end_message")
                    or care.get("message")
                )
                terminal_end_limit = (
                    care.get("error") == "dungeon_end_failed"
                    and care.get("end_code") == -10001
                    and "dungeonTrialInfoLevelMap" in str(message)
                )
                if care.get("error") == "dungeon_start_failed" or terminal_end_limit:
                    # A rejected next floor is the normal terminal condition.
                    result["ok"] = True
                    print(
                        f"{dungeon_label} 第 {current_level} 关｜无法继续"
                        f"（code={code}，message={message}）"
                    )
                    return 0
                print(
                    f"{dungeon_label} 第 {current_level} 关｜失败："
                    f"{result['stop_reason']}（code={code}，message={message}）",
                    file=sys.stderr,
                )
                return 1

            result["completed"] += 1
            if run_limit is not None and result["completed"] >= run_limit:
                result["ok"] = True
                result["stopped_level"] = current_level
                result["stop_reason"] = "count_reached"
                return 0
            if fixed_level is not None:
                # A newly-cleared fixed floor becomes sweepable; every
                # subsequent successful sweep requires a new trial reset.
                sweep_mode = True
                repeat_needs_reset = True
                current_level = fixed_level
                continue
            cleared_level = int(care.get("cleared_level") or current_level)
            current_level = cleared_level + 1
            if max_level is not None:
                current_level = min(max_level, current_level)
                entering_repeat = not sweep_mode and cleared_level >= max_level
                sweep_mode = cleared_level >= max_level
                if sweep_mode:
                    progress_after = care.get("progress_after") or {}
                    challenge_after = int(
                        progress_after.get(
                            "_challengeLevel",
                            progress_after.get("challengeLevel", current_level),
                        )
                        or 0
                    )
                    repeat_needs_reset = bool(
                        challenge_after >= current_level or not entering_repeat
                    )
    except KeyboardInterrupt:
        result["cancelled"] = True
        result["ok"] = bool(result["completed"])
        print("\n已取消")
        return 130
    except Exception as exc:
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        label = "dungeon tower" if tower_mode else f"dungeon {resolved_key}"
        print(f"{label} 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        Path("last_dungeon.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def cmd_slzt(*, level: int | None = None, times: int | None = None) -> int:
    """Clear Lost Tower quietly; no level/count means advance until Ctrl+C."""
    progress_width = 20

    def progress_line(floor: int, completed: int, total: int) -> str:
        filled = min(progress_width, (max(0, completed) * progress_width) // total)
        bar = ("█" * filled) + ("░" * (progress_width - filled))
        return f"失落之塔 第 {floor} 层｜[{bar}] {completed}/{total}"

    fixed_level = int(level) if level is not None else None
    if fixed_level is not None and fixed_level < 1:
        raise ValueError(f"slzt level must be >= 1, got {fixed_level}")
    if times is not None and int(times) < 1:
        raise ValueError(f"slzt times must be >= 1, got {times}")
    show_total = times is not None
    run_limit = int(times) if times is not None else (1 if fixed_level is not None else None)
    auto_advance = fixed_level is None

    session = _load_session()
    session.client.log_enabled = False
    result: dict = {
        "ok": False,
        "mode": "slzt",
        "level": fixed_level,
        "auto_advance": auto_advance,
        "times": run_limit,
        "completed": 0,
        "runs": [],
    }
    progress_open = False
    try:
        session.run_login_pipeline()
        current_level = fixed_level if fixed_level is not None else session.slzt_next_level()
        run_index = 0
        while run_limit is None or run_index < run_limit:
            if run_index > 0:
                session.ensure_heartbeat()
            if show_total:
                print(
                    f"\r{progress_line(current_level, run_index, run_limit)}",
                    end="",
                    flush=True,
                )
                progress_open = True
            run_index += 1
            if not show_total:
                print(f"失落之塔 第 {current_level} 层｜第 {run_index} 次")
            care = session.slzt(level=current_level)
            result["runs"].append(care)
            result["slzt"] = care  # latest run, kept for backward compatibility
            result["completed"] = len(result["runs"])
            if not care.get("ok"):
                result["error"] = care.get("error") or "slzt_failed"
                if progress_open:
                    print()
                    progress_open = False
                print(f"失落之塔失败：{result['error']}", file=sys.stderr)
                return 1

            if show_total:
                is_last = run_index >= run_limit
                print(
                    f"\r{progress_line(current_level, run_index, run_limit)}",
                    end="\n" if is_last else "",
                    flush=True,
                )
                progress_open = not is_last
            if auto_advance:
                current_level += 1

        result["completed"] = len(result["runs"])
        result["ok"] = all(bool(run.get("ok")) for run in result["runs"])
        return 0
    except KeyboardInterrupt:
        if progress_open:
            print()
            progress_open = False
        result["cancelled"] = True
        result["completed"] = len(result["runs"])
        result["ok"] = all(bool(run.get("ok")) for run in result["runs"])
        return 130
    except Exception as exc:
        if progress_open:
            print()
            progress_open = False
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        print(f"失落之塔失败：{exc}", file=sys.stderr)
        return 1
    finally:
        dump_path = Path("last_slzt.json")
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")




def main() -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="DIGIMON UP autorun",
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        help="import account from Charles .chlsj / capture JSON, write account.json, then exit",
    )
    parser.add_argument(
        "--noboss",
        action="store_true",
        help="runloop: only kill small mobs, skip boss; re-open same stage (no progress)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SEC",
        help="runloop: sleep seconds before each battle request (REQUEST_DELAY_SEC; default 0)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("runloop", "auto", "ts", "mine", "zb", "pvp", "fb", "dungeon", "slzt"),
        help="runloop: stage farm; auto: hourly maintain; ts: 数码世界; zb: 开装备; pvp: 竞技场; fb: 副本; dungeon tower: 自动推进今日职业试炼; slzt: 失落之塔",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=None,
        help="zb: items to open; default reads all remaining ItemTicket stock",
    )
    parser.add_argument(
        "--batches",
        type=int,
        default=None,
        help="zb: batch times override (if set, ignores --total)",
    )
    parser.add_argument(
        "-c",
        "--count",
        type=int,
        default=None,
        help=(
            "runloop: stop after N killed mobs; zb: items per batch; "
            "dungeon tower: total successful runs (default: infinite)"
        ),
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="zb: only print furnace info + ItemTicket stock, no spawn",
    )
    parser.add_argument(
        "--filter-grade",
        type=int,
        default=DEFAULT_FILTER_GRADE,
        help=(
            "zb: _filterGrade for spawn-and-sell "
            f"(default {DEFAULT_FILTER_GRADE}, live client)"
        ),
    )
    parser.add_argument(
        "--filter-match",
        type=int,
        default=DEFAULT_FILTER_MATCH_COUNT,
        help=(
            "zb: _filterMatchCount for spawn-and-sell "
            f"(default {DEFAULT_FILTER_MATCH_COUNT}, live client)"
        ),
    )
    parser.add_argument(
        "--filter-stat",
        type=str,
        default=",".join(str(x) for x in DEFAULT_FILTER_STAT_TYPE_LIST),
        help=(
            "zb: comma-separated _filterStatTypeList E_STAT ids "
            f"(default {','.join(str(x) for x in DEFAULT_FILTER_STAT_TYPE_LIST)} = "
            "CriticalRate,StunRate,SkillCriticalRate)"
        ),
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=1,
        help="zb: concurrent spawn-and-sell workers (default 1=serial; progress uses real ticket drop)",
    )
    parser.add_argument(
        "fb_key",
        nargs="?",
        default=None,
        help="fb/dungeon: dungeon alias/key; tower dynamically selects today's trial",
    )
    parser.add_argument(
        "--key",
        type=int,
        default=None,
        help="fb: dungeon progress key override",
    )
    parser.add_argument(
        "-l",
        "--level",
        type=int,
        default=None,
        help="slzt or dungeon tower: fixed floor; omit to advance from current progress",
    )
    parser.add_argument(
        "-t",
        "--times",
        type=int,
        default=None,
        help="slzt: number of clears; omit with no -l to run until Ctrl+C",
    )
    args = parser.parse_args()

    if args.input:
        return cmd_import(args.input)

    if args.command == "runloop":
        return cmd_runloop(
            no_boss=bool(args.noboss),
            delay=float(args.delay or 0.0),
            count=args.count,
        )
    if args.command == "auto":
        return cmd_auto()
    if args.command in ("ts", "mine"):
        return cmd_ts()
    if args.command == "pvp":
        return cmd_pvp()
    if args.command == "slzt":
        if args.level is not None and int(args.level) < 1:
            print("[-] slzt level must be positive")
            return 2
        if args.times is not None and int(args.times) < 1:
            print("[-] slzt requires positive times, e.g. python3 main.py slzt -l 4 -t 2")
            return 2
        return cmd_slzt(level=args.level, times=args.times)
    if args.command in ("fb", "dungeon"):
        alias = args.fb_key if args.fb_key is not None else (str(args.key) if args.key is not None else None)
        if alias is None:
            print("[-] fb/dungeon requires key/alias, e.g. python3 main.py dungeon tower")
            return 2
        tower_mode = args.command == "dungeon" and str(alias).strip().lower() == "tower"
        key = "tower" if tower_mode else resolve_fb_key(alias)
        if args.command == "dungeon" and (
            tower_mode or key in ADVANCING_DUNGEON_CONFIG
        ):
            if args.count is not None and int(args.count) < 1:
                print(
                    f"[-] dungeon {key} requires positive count, "
                    f"e.g. python3 main.py dungeon {key} -c 10"
                )
                return 2
            if args.level is not None and int(args.level) < 1:
                print(
                    f"[-] dungeon {key} requires positive level, "
                    f"e.g. python3 main.py dungeon {key} -l 3"
                )
                return 2
            return cmd_dungeon_advance(
                key=key,
                count=args.count,
                level=args.level,
            )
        return cmd_fb(str(alias), level=args.level)
    if args.command == "zb":
        def _parse_filter_stat(raw: str | None) -> list[int]:
            if raw is None:
                return list(DEFAULT_FILTER_STAT_TYPE_LIST)
            s = str(raw).strip()
            if not s:
                return []
            return [int(part.strip()) for part in s.split(",") if part.strip()]

        return cmd_zb(
            batches=args.batches,
            total=int(args.total) if args.total is not None else None,
            count=args.count,
            info_only=bool(args.info),
            filter_grade=int(args.filter_grade),
            filter_match=int(args.filter_match),
            filter_stat=_parse_filter_stat(args.filter_stat),
            workers=max(1, int(args.workers or 1)),
        )

    parser.print_help()
    print("\nExamples:")
    print("  python3 main.py --input capture.chlsj")
    print("  python3 main.py runloop")
    print("  python3 main.py runloop --noboss")
    print("  python3 main.py runloop --noboss --count 20")
    print("  python3 main.py auto")
    print("  python3 main.py ts")
    print("  python3 main.py zb")
    print("  python3 main.py pvp")
    print("  python3 main.py zb --info")
    print("  python3 main.py zb --total 1000")
    print("  python3 main.py zb --batches 3")
    print("  python3 main.py zb --count 8 --batches 1")
    print("  python3 main.py zb --filter-grade 10 --filter-match 2 --filter-stat 10,20,13")
    print("  python3 main.py zb --filter-grade 0 --filter-match 0 --filter-stat \"\"")
    print("  python3 main.py zb -j 2")
    print("  python3 main.py fb 1")
    print("  python3 main.py fb 2 --level 56")
    print("  python3 main.py fb 3")
    print("  python3 main.py dungeon tower")
    print("  python3 main.py dungeon tower -l 3")
    print("  python3 main.py dungeon tower -l 100 -c 10")
    print("  python3 main.py slzt")
    print("  python3 main.py slzt -l 4 -t 2")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
