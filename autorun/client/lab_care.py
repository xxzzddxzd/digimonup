"""Lab / 训练 maintain: claim completed training, restart, ask camp help.

Flow from capture + UI:
  1) /api/lab/info -> {_lab: key/start/complete/isHelpRequested}
  2) if completeTime <= serverTime: /api/lab/complete {_key}
  3) start same key again: /api/lab/run {_key}
  4) ask help: /api/camp/help {_helpContentType: 2}

Also: if currently running and help not requested, only call camp/help.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .apis import lab as lab_api
from .partner_care import current_server_ms
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
HELP_LAB = lab_api.HELP_LAB


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


def run_lab_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
) -> dict:
    """Check lab; if completed, complete -> run same key -> camp help."""
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
    }

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
        # Prefer same key as the finished training (capture: complete/run same body).
        run_key = key
        after_complete_lab = _lab_from(done)
        if after_complete_lab:
            result["lab_after_complete"] = after_complete_lab

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
            # still mark ok if complete succeeded
            result["ok"] = True
            return result

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
            result["actions"].append(
                {"action": "help", **(result.get("help") or {})}
            )
        else:
            log("[*] lab help already requested after run")

        result["ok"] = True
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
        # refresh
        info2 = lab_api.lab_info(session.client)
        if _code(info2) in (0, None):
            result["lab_after"] = _lab_from(info2)
        return result

    # idle / no current training
    result["ok"] = True
    result["skipped_reason"] = "idle"
    log("[*] lab skip idle (no active training)")
    return result
