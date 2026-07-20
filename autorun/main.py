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
from client.apis import afk as afk_api
from client.partner_care import run_qmd
from client.qmd_auto import run_qmdauto_loop
from client.drops import parse_battle_end, reward_label, _list_of
from client.farm import FarmConfig, FarmRunner
from client.heartbeat import HeartbeatService
from client.runtime_state import STATE
from client.session import GameSession
from client.tui import FarmTUI

DUMP_PATH = "last_run.json"
STATS_PATH = "drop_stats.json"


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



def _summarize_rewards(payload: dict) -> list[str]:
    """Best-effort human labels from AFK / reward payloads."""
    labels: list[str] = []
    if not isinstance(payload, dict):
        return labels

    # Common containers: _rewardAllList, _pendingRewardList, nested _list
    candidates = []
    for key in ("_rewardAllList", "rewardAllList", "_pendingRewardList", "pendingRewardList"):
        if key in payload:
            candidates.append(payload.get(key))
    afk = payload.get("_afk") or payload.get("afk")
    if isinstance(afk, dict):
        for key in ("_pendingRewardList", "pendingRewardList", "_rewardList", "rewardList"):
            if key in afk:
                candidates.append(afk.get(key))
        # sometimes rewards are nested lists of RewardInfoParam
        for key in ("_baseRewardList", "_additionalRewardList"):
            if key in afk:
                candidates.append(afk.get(key))

    def walk(node):
        if isinstance(node, list):
            for it in node:
                walk(it)
            return
        if not isinstance(node, dict):
            return
        # RewardInfoParam-like
        if any(k in node for k in ("_rewardType", "rewardType", "_type")) and any(
            k in node for k in ("_count", "count", "_value", "value")
        ):
            rtype = node.get("_rewardType", node.get("rewardType", node.get("_type", 0)))
            value = node.get("_value", node.get("value", node.get("_goodsType", 0)))
            count = node.get("_count", node.get("count", 1))
            try:
                labels.append(f"{reward_label(int(rtype), value)}x{int(count)}")
            except Exception:
                labels.append(str(node))
            return
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v)

    for c in candidates:
        walk(c)
    # fallback walk whole payload shallowly for reward items
    if not labels:
        walk(payload)
    return labels


def _print_afk_state(tag: str, body: dict) -> None:
    code = body.get("_code", 0)
    print(f"[*] {tag} code={code} msg={body.get('_message')}")
    afk = body.get("_afk") if isinstance(body.get("_afk"), dict) else {}
    if afk:
        print(
            f"    afk count={afk.get('_rewardIntervalCount', afk.get('count'))} "
            f"adCount={afk.get('_adCount', afk.get('adCount'))} "
            f"lastRewardTime={afk.get('_lastRewardTime', afk.get('lastObtainTime'))}"
        )
    labels = _summarize_rewards(body)
    if labels:
        # unique preserve order
        seen = set()
        uniq = []
        for x in labels:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        print("    rewards:", ", ".join(uniq[:40]) + (" ..." if len(uniq) > 40 else ""))
    else:
        # short key dump for debugging empty claims
        keys = list(body.keys()) if isinstance(body, dict) else []
        print("    keys:", keys)


def cmd_afk() -> int:
    """Login then query + claim AFK rewards (/api/afk/*)."""
    session = _load_session()
    session.client.log_enabled = True
    hb = None
    result: dict = {"ok": False, "mode": "afk"}
    try:
        pipe = session.run_login_pipeline()
        result["login_pipeline"] = {
            "session_key": session.client.session_key,
            "public_uid": session.auth_info.get("_publicUid"),
            "battle_info": session.battle_info,
            "init_keys": pipe.get("init_keys"),
        }
        print("[+] login pipeline ok")
        hb = HeartbeatService(session, log=print)
        hb.start()

        listed = afk_api.reward_list(session.client)
        result["reward_list"] = listed
        _print_afk_state("afk/reward-list", listed)
        if listed.get("_code", 0) not in (0, None):
            raise RuntimeError(
                f"afk/reward-list failed code={listed.get('_code')} msg={listed.get('_message')}"
            )

        obtained = afk_api.reward_obtain(session.client)
        result["reward"] = obtained
        _print_afk_state("afk/reward", obtained)
        if obtained.get("_code", 0) not in (0, None):
            raise RuntimeError(
                f"afk/reward failed code={obtained.get('_code')} msg={obtained.get('_message')}"
            )

        # Optional ad bonus — ignore soft fail (no ad left / daily cap).
        ad = afk_api.ad_view(session.client)
        result["ad_view"] = ad
        _print_afk_state("afk/ad-view", ad)

        result["ok"] = True
        print("[+] afk done")
        return 0
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
        Path(DUMP_PATH).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {DUMP_PATH}")



def cmd_qmd() -> int:
    """Partner care: wait cooldown if needed, relation-exp, then relation-reward."""
    session = _load_session()
    session.client.log_enabled = True
    hb = None
    result: dict = {"ok": False, "mode": "qmd"}
    try:
        pipe = session.run_login_pipeline()
        result["login_pipeline"] = {
            "session_key": session.client.session_key,
            "public_uid": session.auth_info.get("_publicUid"),
            "server_time": (session.login_info or {}).get("_serverTime"),
            "battle_info": session.battle_info,
        }
        print("[+] login pipeline ok")
        hb = HeartbeatService(session, log=print)
        hb.start()

        care = run_qmd(session, wait_cooldown=True, log=print)
        result["care"] = care
        result["ok"] = bool(care.get("ok"))
        if result["ok"]:
            print("[+] qmd done")
            return 0
        print("[-] qmd incomplete")
        return 1
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
        Path(DUMP_PATH).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {DUMP_PATH}")



def cmd_qmdauto() -> int:
    """Loop: login -> query nextRelationExpTime -> sleep -> re-login -> qmd+afk."""
    def make_session() -> GameSession:
        return _load_session()

    print("[*] qmdauto: driven by server nextRelationExpTime; Ctrl+C to stop")
    return run_qmdauto_loop(make_session, log=print, http_log=True)


def cmd_runloop() -> int:
    """TUI + infinite stay farm on current login frontier."""
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
        print(
            f"[*] runloop: TUI + infinite stay on login frontier "
            f"{login_stage}-{login_sector} region={login_region}"
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
        "command",
        nargs="?",
        choices=("runloop", "afk", "qmd", "qmdauto"),
        help="runloop: auto farm; afk: AFK rewards; qmd: partner feed once; qmdauto: loop qmd+afk by cooldown",
    )
    args = parser.parse_args()

    if args.input:
        return cmd_import(args.input)

    if args.command == "runloop":
        return cmd_runloop()
    if args.command == "afk":
        return cmd_afk()
    if args.command == "qmd":
        return cmd_qmd()
    if args.command == "qmdauto":
        return cmd_qmdauto()

    parser.print_help()
    print("\nExamples:")
    print("  python3 main.py --input capture.chlsj")
    print("  python3 main.py runloop")
    print("  python3 main.py afk")
    print("  python3 main.py qmd")
    print("  python3 main.py qmdauto")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
