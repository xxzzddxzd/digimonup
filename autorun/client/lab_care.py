"""Lab / 训练 maintain: claim completed training, restart/switch, ask camp help.

Flow from capture + UI:
  1) /api/lab/info -> {_lab: key/start/complete/isHelpRequested}
  2) if completeTime <= serverTime: /api/lab/complete {_key}
  3) pick next key via lab_config.json max_level + priority
  4) /api/lab/run {_key}
  5) /api/camp/help {_helpContentType: 2}

Manual config: autorun/lab_config.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from .apis import lab as lab_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
HELP_LAB = lab_api.HELP_LAB

# autorun/lab_config.json (next to main.py)
LAB_CONFIG_PATH = Path(__file__).resolve().parent.parent / "lab_config.json"


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


def _lab_from(payload: dict) -> dict:
    lab = payload.get("_lab") or payload.get("lab") or {}
    return lab if isinstance(lab, dict) else {}


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


def load_lab_config(path: Path | None = None) -> dict[str, Any]:
    """Load manual lab_config.json; missing file -> empty defaults."""
    p = path or LAB_CONFIG_PATH
    cfg: dict[str, Any] = {
        "default_max_level": 5,
        "max_level": {},
        "priority": [],
    }
    if not p.is_file():
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return cfg
    if not isinstance(raw, dict):
        return cfg

    try:
        cfg["default_max_level"] = int(raw.get("default_max_level", 5))
    except Exception:
        cfg["default_max_level"] = 5

    max_level: dict[int, int] = {}
    src = raw.get("max_level") or {}
    if isinstance(src, dict):
        for k, v in src.items():
            try:
                max_level[int(k)] = int(v)
            except Exception:
                continue
    cfg["max_level"] = max_level

    priority: list[int] = []
    src_p = raw.get("priority") or []
    if isinstance(src_p, list):
        for item in src_p:
            try:
                priority.append(int(item))
            except Exception:
                continue
    cfg["priority"] = priority
    cfg["_path"] = str(p)
    return cfg


def max_level_for(key: int, cfg: dict[str, Any]) -> int:
    table = cfg.get("max_level") or {}
    if key in table:
        return int(table[key])
    return int(cfg.get("default_max_level") or 5)


def is_maxed(key: int, level: int, cfg: dict[str, Any]) -> bool:
    return level >= max_level_for(key, cfg)


def _tech_levels(session: GameSession) -> dict[int, dict]:
    """key -> {level, is_open} from /api/lab/list."""
    body = lab_api.lab_list(session.client)
    _raise_if_kick(body, "lab/list")
    out: dict[int, dict] = {}
    if _code(body) not in (0, None):
        return out
    lst = (body.get("_labTechList") or body.get("labTechList") or {}).get("_list") or []
    if not isinstance(lst, list):
        return out
    for it in lst:
        if not isinstance(it, dict):
            continue
        key = _int(it.get("_key") or it.get("key"))
        if key <= 0:
            continue
        out[key] = {
            "level": _int(it.get("_level") or it.get("level")),
            "is_open": _bool(it.get("_isOpen", it.get("isOpen"))),
            "raw": it,
        }
    return out


def pick_run_key(
    techs: dict[int, dict],
    cfg: dict[str, Any],
    *,
    prefer_key: int | None = None,
    log: LogFn | None = None,
) -> int | None:
    """Pick an open, non-maxed tech. Prefer prefer_key if still train-able."""
    if prefer_key and prefer_key in techs:
        info = techs[prefer_key]
        if info.get("is_open") and not is_maxed(prefer_key, int(info["level"]), cfg):
            if log:
                log(
                    f"[*] lab pick same key={prefer_key} "
                    f"level={info['level']}/{max_level_for(prefer_key, cfg)}"
                )
            return prefer_key

    candidates: list[tuple[int, int, int]] = []
    # sort key: priority index, then key
    prio = {k: i for i, k in enumerate(cfg.get("priority") or [])}
    for key, info in techs.items():
        if not info.get("is_open"):
            continue
        level = int(info["level"])
        if is_maxed(key, level, cfg):
            continue
        rank = prio.get(key, 10_000 + key)
        candidates.append((rank, key, level))
    if not candidates:
        if log:
            log("[*] lab no open non-max tech to run")
        return None
    candidates.sort()
    rank, key, level = candidates[0]
    if log:
        log(
            f"[*] lab pick next key={key} level={level}/{max_level_for(key, cfg)} "
            f"(rank={rank})"
        )
    return key


def summarize_lab(lab: dict, server_ms: int | None = None) -> str:
    key = _int(lab.get("_key") or lab.get("key"))
    start = _int(lab.get("_startTime") or lab.get("startTime"))
    complete = _int(lab.get("_completeTime") or lab.get("completeTime"))
    help_req = _bool(lab.get("_isHelpRequested", lab.get("isHelpRequested")))
    help_count = _int(lab.get("_helpCount") or lab.get("helpCount"))
    left = None
    if server_ms is not None and complete:
        left = (complete - server_ms) / 1000.0
    parts = [
        f"key={key}",
        f"start={start}",
        f"complete={complete}",
        f"help={help_req}",
        f"helpCount={help_count}",
    ]
    if left is not None:
        parts.append(f"left={left:.0f}s")
    return " ".join(parts)


def is_lab_complete(lab: dict, server_ms: int) -> bool:
    key = _int(lab.get("_key") or lab.get("key"))
    if key <= 0:
        return False
    complete = _int(lab.get("_completeTime") or lab.get("completeTime"))
    if complete <= 0:
        return False
    return server_ms >= complete


def is_lab_running(lab: dict, server_ms: int) -> bool:
    key = _int(lab.get("_key") or lab.get("key"))
    if key <= 0:
        return False
    complete = _int(lab.get("_completeTime") or lab.get("completeTime"))
    if complete <= 0:
        return True
    return server_ms < complete


def _ask_help(session: GameSession, *, log: LogFn, result: dict) -> dict:
    resp = lab_api.camp_help(session.client, help_content_type=HELP_LAB)
    _raise_if_kick(resp, "camp/help[lab]")
    item = {
        "code": _code(resp),
        "message": resp.get("_message"),
        "help_content_type": HELP_LAB,
        "body": resp,
    }
    result["help"] = item
    if item["code"] in (0, None):
        log("[+] lab camp/help ok (type=Lab)")
        result["helped"] = True
    else:
        log(f"[!] lab camp/help code={item['code']} msg={item['message']}")
    return item


def _run_and_help(
    session: GameSession,
    *,
    run_key: int,
    log: LogFn,
    result: dict,
) -> bool:
    run = lab_api.lab_run(session.client, key=run_key)
    _raise_if_kick(run, f"lab/run[{run_key}]")
    rcode = _code(run)
    result["actions"].append(
        {
            "action": "run",
            "key": run_key,
            "code": rcode,
            "message": run.get("_message"),
        }
    )
    if rcode not in (0, None):
        result["errors"].append(
            {
                "stage": "run",
                "key": run_key,
                "code": rcode,
                "message": run.get("_message"),
            }
        )
        log(f"[!] lab run key={run_key} code={rcode} msg={run.get('_message')}")
        return False

    result["ran_key"] = run_key
    log(f"[+] lab run key={run_key} ok")
    after_run = _lab_from(run)
    if after_run:
        result["lab_after"] = after_run
        help_requested = _bool(
            after_run.get("_isHelpRequested", after_run.get("isHelpRequested"))
        )
    else:
        help_requested = False

    if not help_requested:
        _ask_help(session, log=log, result=result)
        result["actions"].append({"action": "help", **(result.get("help") or {})})
    else:
        log("[*] lab help already requested after run")
    return True


def run_lab_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    config_path: Path | None = None,
) -> dict:
    """Check lab; if completed, complete -> pick next key -> run -> camp help."""
    result: dict[str, Any] = {
        "ok": False,
        "skipped_reason": None,
        "lab_before": None,
        "lab_after": None,
        "completed_key": None,
        "ran_key": None,
        "helped": False,
        "actions": [],
        "errors": [],
        "config_path": None,
    }

    cfg = load_lab_config(config_path)
    result["config_path"] = cfg.get("_path") or str(config_path or LAB_CONFIG_PATH)
    log(f"[*] lab config -> {result['config_path']}")

    server_ms = current_server_ms(session, login_wall=login_wall)
    info = lab_api.lab_info(session.client)
    _raise_if_kick(info, "lab/info")
    if _code(info) not in (0, None):
        result["skipped_reason"] = f"info_code={_code(info)}"
        result["errors"].append(
            {"stage": "info", "code": _code(info), "message": info.get("_message")}
        )
        log(f"[-] lab info code={_code(info)} msg={info.get('_message')}")
        return result

    lab = _lab_from(info)
    result["lab_before"] = lab
    log(f"[*] lab status {summarize_lab(lab, server_ms)}")

    key = _int(lab.get("_key") or lab.get("key"))
    help_requested = _bool(lab.get("_isHelpRequested", lab.get("isHelpRequested")))

    if is_lab_complete(lab, server_ms):
        log(f"[*] lab complete ready key={key}")
        done = lab_api.lab_complete(session.client, key=key)
        _raise_if_kick(done, f"lab/complete[{key}]")
        dcode = _code(done)
        result["actions"].append(
            {
                "action": "complete",
                "key": key,
                "code": dcode,
                "message": done.get("_message"),
            }
        )
        if dcode not in (0, None):
            result["errors"].append(
                {
                    "stage": "complete",
                    "key": key,
                    "code": dcode,
                    "message": done.get("_message"),
                }
            )
            log(f"[!] lab complete key={key} code={dcode} msg={done.get('_message')}")
            return result

        result["completed_key"] = key
        log(f"[+] lab complete key={key} ok")
        after_complete_lab = _lab_from(done)
        if after_complete_lab:
            result["lab_after_complete"] = after_complete_lab

        techs = _tech_levels(session)
        # after complete, finished key level should have +1 in list
        if key in techs:
            lv = int(techs[key]["level"])
            log(
                f"[*] lab key={key} now level={lv}/{max_level_for(key, cfg)} "
                f"maxed={is_maxed(key, lv, cfg)}"
            )
        run_key = pick_run_key(techs, cfg, prefer_key=key, log=log)
        if run_key is None:
            result["ok"] = True
            result["skipped_reason"] = "all_maxed_or_locked"
            log("[*] lab no further tech to run after complete")
            return result

        ok = _run_and_help(session, run_key=run_key, log=log, result=result)
        result["ok"] = True if ok or result.get("completed_key") else False
        return result

    if is_lab_running(lab, server_ms):
        if not help_requested:
            log(f"[*] lab running key={key} without help; request camp help")
            _ask_help(session, log=log, result=result)
            result["actions"].append(
                {"action": "help", **(result.get("help") or {})}
            )
            result["ok"] = True
            result["skipped_reason"] = None
        else:
            result["ok"] = True
            result["skipped_reason"] = "running"
            log(f"[*] lab skip running key={key} help already requested")
        info2 = lab_api.lab_info(session.client)
        if _code(info2) in (0, None):
            result["lab_after"] = _lab_from(info2)
        return result

    # idle: optionally start a non-max open tech
    techs = _tech_levels(session)
    run_key = pick_run_key(techs, cfg, prefer_key=None, log=log)
    if run_key is None:
        result["ok"] = True
        result["skipped_reason"] = "idle"
        log("[*] lab skip idle (no active training / nothing to start)")
        return result

    log(f"[*] lab idle -> start key={run_key}")
    ok = _run_and_help(session, run_key=run_key, log=log, result=result)
    result["ok"] = ok
    if not ok:
        result["skipped_reason"] = "run_failed"
    return result
