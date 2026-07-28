"""Daily shop auto-buy for a fixed target list.

Policy (user):
  - Targets: daily-limited shops excluding Cash / paid Diamond / AD / Shop_PVPCoin
  - Keep free Diamond (cost 0), Crystal, CampCoin
  - Fetch /api/shop/list; server `_buyCount` is remaining quota
  - If remain > 0, POST /api/shop/buy with {_key, _count: remain}
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .apis import shop as shop_api
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK_CODE = -19006


class SessionKicked(RuntimeError):
    def __init__(self, where: str, *, body: Any = None):
        super().__init__(f"session kick -19006 at {where}")
        self.where = where
        self.body = body


# key, daily_limit, currency, unit_cost
DAILY_BUY_TARGETS: tuple[tuple[int, int, str, int], ...] = (
    (201016, 1, "Diamond", 0),
    (202209, 1, "Crystal", 480),
    (202210, 1, "Crystal", 480),
    (202213, 1, "Crystal", 160),
    (202214, 5, "Crystal", 100),
    (320004, 10, "Crystal", 20),
    (340015, 1, "CampCoin", 400),
    (340016, 1, "CampCoin", 400),
    (340017, 1, "CampCoin", 1000),
    (340018, 1, "CampCoin", 400),
    (340019, 1, "CampCoin", 400),
    (340020, 2, "CampCoin", 500),
    (340021, 2, "CampCoin", 500),
)


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
    if _code(body) == SESSION_KICK_CODE:
        raise SessionKicked(where, body=body)


def _shop_state_map(payload: Any) -> dict[int, dict]:
    if not isinstance(payload, dict):
        return {}
    sl = payload.get("_shopList") or payload.get("shopList") or {}
    lst = sl.get("_list") if isinstance(sl, dict) else sl
    if not isinstance(lst, list):
        return {}
    out: dict[int, dict] = {}
    for it in lst:
        if not isinstance(it, dict):
            continue
        key = it.get("_key", it.get("key"))
        try:
            out[int(key)] = it
        except Exception:
            continue
    return out


def _as_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def remain_for(key: int, *, limit: int, state: dict | None) -> int:
    """Remaining daily buys.

    Server `_buyCount` is **remaining** quota (not used).
    Confirmed via /api/shop/buy and AD shop/spawn responses:
      limit=2 -> buyCount 1 after first, 0 after second.
    Missing state => assume full limit still available.
    """
    lim = max(0, int(limit))
    if lim <= 0:
        return 0
    if not state:
        return lim
    # Prefer explicit remaining field; clamp to table limit.
    rem = _as_int(state.get("_buyCount", state.get("buyCount")), 0)
    return max(0, min(lim, rem))


def plan_daily_buys(shop_list_resp: Any) -> list[dict[str, Any]]:
    state = _shop_state_map(shop_list_resp)
    plan: list[dict[str, Any]] = []
    for key, limit, currency, cost in DAILY_BUY_TARGETS:
        st = state.get(int(key))
        rem = remain_for(int(key), limit=int(limit), state=st)
        used = max(0, int(limit) - int(rem)) if st is not None else 0
        plan.append(
            {
                "key": int(key),
                "limit": int(limit),
                "currency": currency,
                "cost": int(cost),
                "used": int(used),
                "remain": int(rem),
                "buyCount_raw": _as_int((st or {}).get("_buyCount"), 0) if st else None,
                "in_list": st is not None,
            }
        )
    return plan


def run_shop_daily_buy(
    session: GameSession,
    *,
    log: LogFn = print,
) -> dict[str, Any]:
    """Fetch shop list and buy each target with remaining daily count."""
    result: dict[str, Any] = {
        "ok": False,
        "bought": [],
        "skipped": [],
        "failed": [],
        "plan": [],
        "list_code": None,
    }

    listed = shop_api.shop_list(session.client)
    _raise_if_kick(listed, "shop/list")
    result["list_code"] = _code(listed)
    if result["list_code"] not in (0, None):
        result["list_message"] = listed.get("_message") if isinstance(listed, dict) else None
        log(f"[-] shop/list fail code={result['list_code']} msg={result.get('list_message')}")
        return result

    plan = plan_daily_buys(listed)
    result["plan"] = plan
    todo = [p for p in plan if int(p["remain"]) > 0]
    log(
        f"[*] shop daily targets={len(plan)} remain>0={len(todo)} "
        f"keys={[p['key'] for p in todo]}"
    )
    if not todo:
        result["ok"] = True
        log("[*] shop daily: nothing to buy")
        return result

    for p in todo:
        key = int(p["key"])
        count = int(p["remain"])
        unit = int(p["cost"])
        currency = p["currency"]
        log(
            f"[*] shop buy key={key} count={count} "
            f"currency={currency} unit={unit} total={unit * count}"
        )
        try:
            body = shop_api.shop_buy(session.client, key=key, count=count)
        except Exception as exc:
            result["failed"].append(
                {"key": key, "count": count, "error": str(exc), "currency": currency}
            )
            log(f"[-] shop buy key={key} exception: {exc}")
            continue

        _raise_if_kick(body, f"shop/buy[{key}]")
        code = _code(body)
        entry = {
            "key": key,
            "count": count,
            "currency": currency,
            "unit_cost": unit,
            "code": code,
            "message": body.get("_message") if isinstance(body, dict) else None,
        }
        if isinstance(body, dict) and isinstance(body.get("_shop"), dict):
            entry["after_buyCount"] = body["_shop"].get("_buyCount")
        if code == 0:
            result["bought"].append(entry)
            log(
                f"[+] shop buy ok key={key} count={count} "
                f"afterBuyCount={entry.get('after_buyCount')}"
            )
        else:
            result["failed"].append(entry)
            log(
                f"[-] shop buy fail key={key} count={count} "
                f"code={code} msg={entry.get('message')}"
            )

    # items already full
    for p in plan:
        if int(p["remain"]) <= 0:
            result["skipped"].append(
                {
                    "key": p["key"],
                    "reason": "no_remain",
                    "used": p["used"],
                    "limit": p["limit"],
                    "currency": p["currency"],
                }
            )

    result["ok"] = len(result["failed"]) == 0
    log(
        f"[*] shop daily done bought={len(result['bought'])} "
        f"failed={len(result['failed'])} skipped={len(result['skipped'])}"
    )
    return result
