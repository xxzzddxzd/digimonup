#!/usr/bin/env python3
"""DIGIMON UP protocol client: login, single battle, or farm with drop stats."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client.session import GameSession
from client.apis import battle as battle_api
from client.http_client import ApiError
from client.drops import DropStats, parse_battle_end
from client.farm import FarmConfig, FarmRunner, FarmTarget, _extract_spawn_waves
from client.runtime_state import STATE
from client.tui import FarmTUI
from client.account_store import import_input_file, apply_account_to_config, load_account_file


def _pick_stage(session: GameSession, args) -> tuple[int, int, int, int]:
    info = session.battle_info or {}
    acc = session.config.account
    region = args.region if args.region is not None else int(info.get("_region", acc.capture_region))
    stage = args.stage if args.stage is not None else int(info.get("_stage", acc.capture_stage))
    sector = args.sector if args.sector is not None else int(info.get("_sector", acc.capture_sector))
    repeat = args.repeat if args.repeat is not None else int(info.get("_repeat", 0) or 0)
    if sector < 1:
        sector = 1
    if stage < 1:
        stage = 1
    return region, stage, sector, repeat


def _run_single_battle(session: GameSession, args, result: dict) -> None:
    region, stage, sector, repeat = _pick_stage(session, args)
    print(f"[*] battle start region={region} stage={stage} sector={sector} repeat={repeat}")
    start = session.battle_start(
        region=region,
        stage=stage,
        sector=sector,
        repeat=repeat,
        wave=0,
        state=battle_api.STATE_FORWARD,
        attr=battle_api.ATTR_PLAY,
    )
    waves = _extract_spawn_waves(start.get("_spawnMobList") or {})
    result["battle_start"] = {
        "_code": start.get("_code"),
        "_battle": start.get("_battle"),
        "_spawn_waves": [{"wave": w, "mobs": len(m)} for w, m in waves],
    }
    print("[+] battle/start code=", start.get("_code"), "waves=", result["battle_start"]["_spawn_waves"])
    code = start.get("_code", 0)
    if code not in (0, None):
        result["battle_start_full"] = start
        raise ApiError(f"battle/start rejected code={code}", body=start)

    result["battle_kill_mob"] = []
    for wave_no, mobs in waves:
        print(f"[*] kill-mob wave={wave_no} count={len(mobs)}")
        km = session.battle_kill_mob(wave=wave_no, mob_uid_list=mobs, reason=battle_api.REASON_NONE)
        result["battle_kill_mob"].append({"wave": wave_no, "code": km.get("_code"), "message": km.get("_message")})
        print("[+] kill-mob code=", km.get("_code"), "msg=", km.get("_message"))
        if km.get("_code", 0) not in (0, None):
            raise ApiError(f"battle/kill-mob rejected code={km.get('_code')}", body=km)

    end = session.battle_end(
        region=region,
        reason=battle_api.REASON_CLEAR,
        state=battle_api.STATE_FORWARD,
        damage=args.damage,
    )
    drop = parse_battle_end(end, region=region, stage=stage, sector=sector)
    result["battle_end"] = end
    result["drops"] = drop.to_dict()
    print("[+] battle/end code=", end.get("_code"))
    print("[+] drops:", ", ".join(f"{d.label}x{d.count}" for d in drop.drops) or "(none)")
    if end.get("_code", 0) not in (0, None):
        raise ApiError(f"battle/end rejected code={end.get('_code')}", body=end)


def main() -> int:
    parser = argparse.ArgumentParser(description="DIGIMON UP offline protocol client")
    parser.add_argument("--region", type=int, default=None)
    parser.add_argument("--stage", type=int, default=None, help="default: capture stage 23")
    parser.add_argument("--sector", type=int, default=None, help="default: capture sector 2")
    parser.add_argument("--repeat", type=int, default=None)
    parser.add_argument("--damage", default="0")
    parser.add_argument("--skip-battle", action="store_true")
    parser.add_argument("--farm", action="store_true", help="loop clear stages and collect drop stats")
    parser.add_argument("--stay", action="store_true", help="farm current login frontier (repeat=0); follow server after clear")
    parser.add_argument("--recover-wait", type=float, default=60, help="seconds to wait on fail code<0 before re-auth (default 60; -19006 always waits 600)")
    parser.add_argument("--count", type=int, default=0, help="farm loops; 0 = infinite (default)")
    parser.add_argument("--min-stage", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--stats", default="drop_stats.json")
    parser.add_argument("--quiet", action="store_true", help="hide per-request REQ/RESP logs")
    parser.add_argument("--tui", action="store_true", help="rich TUI dashboard (hides plain HTTP lines)")
    parser.add_argument("--dump", default="last_run.json")
    parser.add_argument(
        "--input",
        default=None,
        help="import only: parse Charles .chlsj / capture JSON, overwrite account.json, then exit",
    )
    args = parser.parse_args()

    if args.input:
        imported = import_input_file(args.input)
        print(
            f"[+] imported account from {args.input} -> {imported.get('saved_path')} "
            f"client_id={imported.get('client_id')} "
            f"device_id={imported.get('device_id')} "
            f"server={imported.get('preferred_server_num')}"
        )
        return 0

    session = GameSession()
    # Auto-load local account.json if present.
    saved = load_account_file()
    if saved:
        apply_account_to_config(session.config, saved)
        session.client.data_no = session.config.account.data_no
    # TUI owns the screen: suppress raw HTTP lines by default when --tui.
    session.client.log_enabled = (not args.quiet) and (not args.tui)
    session.client.state = STATE
    result: dict = {"ok": False}

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
        if not args.tui:
            print(json.dumps(result["login_pipeline"], ensure_ascii=False, indent=2)[:2500])
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

        # Defaults for bare `python3 main.py`:
        # TUI + infinite stay farm on current login frontier (only startable target).
        bare_default = (
            not args.skip_battle
            and not args.farm
            and not args.stay
            and not args.tui
            and args.stage is None
            and args.sector is None
        )
        if bare_default:
            args.farm = True
            args.stay = True
            args.tui = True
            args.count = 0 if args.count == 0 else args.count
            session.client.log_enabled = False  # TUI owns screen
            print("[*] default: --tui --stay --count 0 on login frontier")

        if args.stay and not args.farm:
            args.farm = True
            print("[*] --stay implies --farm")
        if args.tui and not args.farm and not args.skip_battle:
            args.farm = True
            args.stay = True
            print("[*] --tui implies --farm --stay")
            session.client.log_enabled = False

        if args.farm:
            acc = session.config.account
            info = session.battle_info or {}
            login_stage = int(info.get("_stage") or acc.capture_stage)
            login_sector = int(info.get("_sector") or acc.capture_sector or 1)
            login_region = int(info.get("_region") or acc.capture_region or 1)

            if args.stay:
                # Server only accepts current frontier with repeat=0.
                # Default stay target = login battle_info (not stage-1).
                if args.stage is not None:
                    start_stage = args.stage
                    prefer_server = False
                else:
                    start_stage = login_stage
                    prefer_server = True
                if args.sector is not None:
                    start_sector = max(1, args.sector)
                    prefer_server = False
                else:
                    start_sector = max(1, login_sector)
                region = args.region if args.region is not None else login_region
                print(
                    f"[*] stay target stage={start_stage} sector={start_sector} "
                    f"(login frontier {login_stage}-{login_sector}; prefer_server={prefer_server})"
                )
            else:
                # Push farm: start at explicit stage or current login frontier.
                start_stage = args.stage if args.stage is not None else login_stage
                start_sector = args.sector if args.sector is not None else login_sector
                region = args.region if args.region is not None else login_region
                prefer_server = True

            cfg = FarmConfig(
                start_stage=start_stage,
                start_sector=start_sector,
                region=region,
                count=args.count,
                min_stage=args.min_stage,
                sleep_sec=args.sleep,
                damage=args.damage,
                prefer_server_progress=prefer_server,
                stay=args.stay,
                recover_wait_sec=args.recover_wait,
                stats_path=args.stats,
            )
            runner = FarmRunner(session=session, config=cfg, state=STATE)
            if args.tui:
                import threading

                done = threading.Event()
                err_box: dict = {}

                def _worker():
                    try:
                        # avoid double-print under TUI screen
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
            else:
                stats = runner.farm()
            result["farm_summary"] = stats.summary()
            result["ok"] = stats.wins > 0
            print("[+] farm summary saved ->", args.stats)
            return 0 if result["ok"] else 1

        if not args.skip_battle:
            _run_single_battle(session, args, result)

        result["ok"] = True
        print("[+] SUCCESS")
        return 0
    except Exception as exc:
        result["error"] = str(exc)
        result["trace"] = traceback.format_exc()
        print("[-] FAILED:", exc)
        traceback.print_exc()
        return 1
    finally:
        dump_path = Path(args.dump)
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {dump_path}")


if __name__ == "__main__":
    raise SystemExit(main())
