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
from client.item_spawner_care import run_zb
from client.pvp_care import run_pvp
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
    """One-shot: farm + dbox + qmd + afk. Schedule via crontab hourly."""
    session_holder = {"s": None}

    def make_session():
        s = _load_session()
        session_holder["s"] = s
        return s

    print("[*] auto: one-shot farm/dbox/qmd/afk (crontab hourly; no cooldown sleep)")
    return run_auto_once(make_session, log=print, http_log=True)



def cmd_runloop(*, no_boss: bool = False) -> int:
    """TUI + infinite stay farm on current login frontier.

    no_boss: kill only small-mob waves, fail-end without boss; re-open same stage.
    """
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

        hb = HeartbeatService(session, log=STATE.add_event)
        hb.start()
        result["heartbeat"] = {"interval_sec": 60}

        acc = session.config.account
        info = session.battle_info or {}
        login_stage = int(info.get("_stage") or acc.capture_stage)
        login_sector = max(1, int(info.get("_sector") or acc.capture_sector or 1))
        login_region = int(info.get("_region") or acc.capture_region or 1)
        login_repeat = int(info.get("_repeat") or 0)
        print(
            f"[*] runloop: TUI + infinite stay on login frontier "
            f"{login_stage}-{login_sector} region={login_region} "
            f"repeat={login_repeat} ui_stage={ui_stage_no(login_stage, login_sector, login_repeat)}"
            f"{' noboss' if no_boss else ''}"
        )

        cfg = FarmConfig(
            start_stage=login_stage,
            start_sector=login_sector,
            region=login_region,
            count=0,  # infinite
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
    total: int = 1000,
    count: int | None = None,
    info_only: bool = False,
    filter_grade: int = 0,
    filter_match: int = 0,
) -> int:
    """One-shot 开装备 (default open 1000 items then stop). Furnace care is in auto."""
    session = _load_session()
    session.client.log_enabled = True
    result: dict = {"ok": False, "mode": "zb"}

    try:
        pipe = session.run_login_pipeline()
        result["login"] = {
            "session_key": session.client.session_key,
            "public_uid": session.auth_info.get("_publicUid"),
            "server_num": session.auth_info.get("_serverNum"),
        }
        print(
            f"[+] login ok uid={result['login']['public_uid']} "
            f"server={result['login']['server_num']}"
        )
        stats = run_zb(
            session,
            batches=0 if info_only else batches,
            total=None if info_only else int(total),
            count=count,
            info_only=bool(info_only),
            filter_grade=int(filter_grade),
            filter_match_count=int(filter_match),
            log=print,
        )
        result.update(stats)
    except Exception as exc:
        result["error"] = str(exc)
        print(f"[-] zb crashed: {exc}")
        traceback.print_exc()
    finally:
        dump_path = Path("last_zb.json")
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
        "command",
        nargs="?",
        choices=("runloop", "auto", "ts", "mine", "zb", "pvp"),
        help="runloop: stage farm; auto: hourly maintain; ts: 数码世界; zb: 开装备; pvp: 竞技场(常+赛季)",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=1000,
        help="zb: total items to open then stop (default 1000)",
    )
    parser.add_argument(
        "--batches",
        type=int,
        default=None,
        help="zb: batch times override (if set, ignores --total)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="zb: items per batch (default: GameData SpawnCount, e.g. 8 at lv17)",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="zb: only print furnace info + bit cost, no spawn",
    )
    parser.add_argument(
        "--filter-grade",
        type=int,
        default=0,
        help="zb: _filterGrade for spawn-and-sell (default 0)",
    )
    parser.add_argument(
        "--filter-match",
        type=int,
        default=0,
        help="zb: _filterMatchCount for spawn-and-sell (default 0)",
    )
    args = parser.parse_args()

    if args.input:
        return cmd_import(args.input)

    if args.command == "runloop":
        return cmd_runloop(no_boss=bool(args.noboss))
    if args.command == "auto":
        return cmd_auto()
    if args.command in ("ts", "mine"):
        return cmd_ts()
    if args.command == "pvp":
        return cmd_pvp()
    if args.command == "zb":
        return cmd_zb(
            batches=args.batches,
            total=int(args.total if args.total is not None else 1000),
            count=args.count,
            info_only=bool(args.info),
            filter_grade=int(args.filter_grade or 0),
            filter_match=int(args.filter_match or 0),
        )

    parser.print_help()
    print("\nExamples:")
    print("  python3 main.py --input capture.chlsj")
    print("  python3 main.py runloop")
    print("  python3 main.py runloop --noboss")
    print("  python3 main.py auto")
    print("  python3 main.py ts")
    print("  python3 main.py zb")
    print("  python3 main.py pvp")
    print("  python3 main.py zb --info")
    print("  python3 main.py zb --total 1000")
    print("  python3 main.py zb --batches 3")
    print("  python3 main.py zb --count 8 --batches 1")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
