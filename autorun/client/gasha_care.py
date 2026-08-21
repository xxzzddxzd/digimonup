"""One-shot gasha spawn helpers for legacy gasha helpers (CLI removed)."""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .apis import gasha as gasha_api
from .session import GameSession

LogFn = Callable[[str], None]

GASHA_ALIASES = {
    "ga1": gasha_api.GASHA_PARTNER,  # partner / supporter
    "ga2": gasha_api.GASHA_SP,       # sp / holy weapon
}

GASHA_LABELS = {
    gasha_api.GASHA_PARTNER: "partner(supporter)",
    gasha_api.GASHA_SP: "sp(holy-weapon)",
}


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


def _bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes"):
            return True
        if s in ("0", "false", "no"):
            return False
    return None


def _gasha_list(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    gl = payload.get("_gashaList") or payload.get("gashaList") or {}
    if isinstance(gl, dict):
        lst = gl.get("_list") or gl.get("list") or []
    elif isinstance(gl, list):
        lst = gl
    else:
        lst = []
    return [x for x in lst if isinstance(x, dict)]


def summarize_gasha_item(item: dict) -> dict[str, Any]:
    key = item.get("_key") if item.get("_key") is not None else item.get("key")
    return {
        "key": key,
        "count": item.get("_count", item.get("count")),
        "isDailyCanGet": _bool(item.get("_isDailyCanGet", item.get("isDailyCanGet"))),
        "startTime": item.get("_startTime", item.get("startTime")),
        "endTime": item.get("_endTime", item.get("endTime")),
    }


def summarize_rewards(body: dict) -> list[dict[str, Any]]:
    """Best-effort flatten of common reward fields."""
    out: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return out

    # Direct spawn list
    spawn = body.get("_gashaSpawn") or body.get("gashaSpawn") or {}
    if isinstance(spawn, dict):
        lst = spawn.get("_list") or spawn.get("list") or spawn.get("_spawnList") or []
        if isinstance(lst, dict):
            lst = lst.get("_list") or lst.get("list") or []
        if isinstance(lst, list):
            for it in lst:
                if isinstance(it, dict):
                    out.append(
                        {
                            "source": "gashaSpawn",
                            "type": it.get("_type", it.get("type")),
                            "value": it.get("_value", it.get("value")),
                            "count": it.get("_count", it.get("count")),
                            "grade": it.get("_grade", it.get("grade")),
                            "raw": {k: it[k] for k in list(it)[:12]},
                        }
                    )

    reward_all = body.get("_rewardAllList") or body.get("rewardAllList") or {}
    if isinstance(reward_all, dict):
        # rewardList
        rl = reward_all.get("_rewardList") or reward_all.get("rewardList") or {}
        items = rl.get("_list") if isinstance(rl, dict) else rl
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    out.append(
                        {
                            "source": "rewardList",
                            "type": it.get("_type", it.get("type")),
                            "value": it.get("_value", it.get("value")),
                            "count": it.get("_count", it.get("count")),
                        }
                    )
    return out


def resolve_key(alias_or_key: str | int) -> int:
    if isinstance(alias_or_key, int):
        return alias_or_key
    s = str(alias_or_key).strip().lower()
    if s in GASHA_ALIASES:
        return GASHA_ALIASES[s]
    return int(s)


def run_gasha(
    session: GameSession,
    *,
    key: int,
    count: int = 1,
    is_daily: bool = False,
    fetch_infos: bool = True,
    log: LogFn = print,
) -> dict[str, Any]:
    label = GASHA_LABELS.get(int(key), str(key))
    result: dict[str, Any] = {
        "ok": False,
        "key": int(key),
        "label": label,
        "count": int(count),
        "is_daily": bool(is_daily),
    }

    if fetch_infos:
        try:
            infos = gasha_api.gasha_infos(session.client)
            result["infos_code"] = _code(infos)
            items = _gasha_list(infos)
            result["infos"] = [summarize_gasha_item(x) for x in items]
            for it in result["infos"]:
                if int(it.get("key") or -1) == int(key):
                    result["before"] = it
                    break
            log(
                f"[*] gasha infos code={result['infos_code']} "
                f"items={result['infos']}"
            )
        except Exception as exc:
            result["infos_error"] = str(exc)
            log(f"[!] gasha infos failed: {exc}")

    log(
        f"[*] gasha spawn key={key} ({label}) count={count} isDaily={bool(is_daily)}"
    )
    body = gasha_api.gasha_spawn(
        session.client,
        key=int(key),
        count=int(count),
        is_daily=bool(is_daily),
    )
    code = _code(body)
    result["code"] = code
    result["message"] = body.get("_message") if isinstance(body, dict) else None
    result["body_keys"] = sorted(body.keys()) if isinstance(body, dict) else []
    result["ok"] = code == 0

    # updated gasha item in response
    gasha = body.get("_gasha") if isinstance(body, dict) else None
    if isinstance(gasha, dict):
        result["after"] = summarize_gasha_item(gasha)

    result["rewards"] = summarize_rewards(body if isinstance(body, dict) else {})
    # keep a trimmed body for dump (avoid huge blobs)
    slim = {}
    if isinstance(body, dict):
        for k in ("_code", "_message", "_gasha", "_gashaSpawn", "_questList"):
            if k in body:
                slim[k] = body[k]
        if "_rewardAllList" in body and isinstance(body["_rewardAllList"], dict):
            # keep reward list only
            ra = body["_rewardAllList"]
            slim["_rewardAllList"] = {
                kk: ra[kk]
                for kk in ra
                if kk in ("_rewardList", "rewardList", "_code")
            }
    result["response"] = slim

    if result["ok"]:
        log(f"[+] gasha ok key={key} rewards={len(result['rewards'])} after={result.get('after')}")
    else:
        log(f"[-] gasha failed code={code} message={result.get('message')}")
    return result
