"""Mine / 探查数码世界: spend stamina, pick chips (reward cells).

Coordinate system (live API 2026-07-21):
  - _col: lane 0..4 only (5 vertical lanes in the TUI)
  - _row: depth, grows as you dig: … 3616, 3617, 3618 … (infinite)
  - Forward / deeper = +_row
  - TUI draws _row on the horizontal axis (right = deeper) so it matches
    in-game “x = 3000,3001,…” feel.

Rules:
  - Legal move: target col in {c±1} (any row in window) OR target row in
    {r±1} (any col in window). Not limited to 4-neighbors.
  - Dash when >=2 of the next 3 forward cells (same col, row+1..+3) are rocks.
  - Drill rocks that block the path; advance prefers +row if no chips.
  - Spend Mine_Stamina (150) down to 0; no refill.
  - Claim distance milestone reward when possible.
  - Ignore battles.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .apis import farm as farm_api
from .apis import mine as mine_api
from .session import GameSession

LogFn = Callable[[str], None]

SESSION_KICK = -19006
CELL_EMPTY = 0
CELL_ROCK = 1
CELL_REWARD = 2
MAX_STEPS = 300


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


def _goods_value(goods_payload: dict, goods_type: int) -> int:
    gl = goods_payload.get("_goodsList") or goods_payload.get("goodsList") or {}
    lst = gl.get("_list") or gl.get("list") or []
    if not isinstance(lst, list):
        return 0
    for it in lst:
        if not isinstance(it, dict):
            continue
        if _int(it.get("_type") or it.get("type")) != goods_type:
            continue
        try:
            return int(float(it.get("_value") or it.get("value") or 0))
        except Exception:
            return 0
    return 0


def _mine_from(payload: dict) -> dict:
    info = payload.get("_mineInfo") or payload.get("mineInfo") or {}
    return info if isinstance(info, dict) else {}


def _cells_map(mine: dict) -> dict[tuple[int, int], dict]:
    out: dict[tuple[int, int], dict] = {}
    cl = mine.get("_cellList") or mine.get("cellList") or {}
    lst = cl.get("_list") or cl.get("list") or []
    if not isinstance(lst, list):
        return out
    for it in lst:
        if not isinstance(it, dict):
            continue
        col = _int(it.get("_col") or it.get("col"))
        row = _int(it.get("_row") or it.get("row"))
        out[(col, row)] = it
    return out


def is_legal_move(px: int, py: int, tx: int, ty: int) -> bool:
    """px/py = current (_col,_row); tx/ty = target. Depth is row."""
    if tx == px and ty == py:
        return False
    return tx in (px - 1, px + 1) or ty in (py - 1, py + 1)


def _pos(mine: dict) -> tuple[int, int]:
    return _int(mine.get("_col") or mine.get("col")), _int(
        mine.get("_row") or mine.get("row")
    )


def _reward_cells(cells: dict[tuple[int, int], dict]) -> list[tuple[int, int, dict]]:
    out = []
    for (c, r), cell in cells.items():
        if _int(cell.get("_type") or cell.get("type")) != CELL_REWARD:
            continue
        if _bool(cell.get("_isVisited") or cell.get("isVisited") or cell.get("visited")):
            continue
        out.append((c, r, cell))
    return out


def _step_targets(
    px: int, py: int, cells: dict[tuple[int, int], dict]
) -> list[tuple[int, int]]:
    """All legal destinations present in the current window (world x/y)."""
    rows = {r for (_, r) in cells}
    cols = {c for (c, _) in cells}
    cands: set[tuple[int, int]] = set()
    for c in (px - 1, px + 1):
        for r in rows:
            if is_legal_move(px, py, c, r) and (c, r) in cells:
                cands.add((c, r))
    for r in (py - 1, py + 1):
        for c in cols:
            if is_legal_move(px, py, c, r) and (c, r) in cells:
                cands.add((c, r))
    return sorted(cands)


def _path_to_reward(
    px: int,
    py: int,
    cells: dict[tuple[int, int], dict],
) -> list[tuple[int, int]]:
    """Return 1- or 2-step path ending on an unvisited reward. Empty if none."""
    rewards = {(c, r) for c, r, _ in _reward_cells(cells)}
    if not rewards:
        return []

    # 1 step
    one = []
    for tx, ty in _step_targets(px, py, cells):
        if (tx, ty) in rewards:
            one.append((tx, ty))
    if one:
        # prefer further +row (deeper/right on TUI), then closer col
        one.sort(key=lambda p: (-p[1], abs(p[0] - px), p[0]))
        return [one[0]]

    # 2 steps via intermediate legal cell
    best: list[tuple[int, int]] | None = None
    for mx, my in _step_targets(px, py, cells):
        mid_type = _int(cells[(mx, my)].get("_type"))
        for tx, ty in _step_targets(mx, my, cells):
            if (tx, ty) not in rewards:
                continue
            path = [(mx, my), (tx, ty)]
            if best is None:
                best = path
                continue
            # prefer reward further +row (deeper), intermediate not rock
            score = (-ty, 0 if mid_type != CELL_ROCK else 1, abs(mx - px))
            bmx, bmy = best[0]
            btx, bty = best[1]
            bmid = _int(cells[(bmx, bmy)].get("_type"))
            bscore = (-bty, 0 if bmid != CELL_ROCK else 1, abs(bmx - px))
            if score < bscore:
                best = path
    return best or []


def _forward_rock_count(px: int, py: int, cells: dict[tuple[int, int], dict]) -> int:
    """Rocks on the next 3 cells deeper (same col, row+1..+3)."""
    n = 0
    for dr in (1, 2, 3):
        cell = cells.get((px, py + dr))
        if cell and _int(cell.get("_type")) == CELL_ROCK:
            n += 1
    return n


def _advance_targets(px: int, py: int, cells: dict[tuple[int, int], dict]) -> list[tuple[int, int]]:
    """No chips: prefer +row (deeper). Same-col first, then col±1."""
    ordered: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for k in range(4, 0, -1):
        for dc in (0, 1, -1):
            tx, ty = px + dc, py + k
            if not is_legal_move(px, py, tx, ty):
                continue
            if (tx, ty) not in cells:
                continue
            if (tx, ty) not in seen:
                ordered.append((tx, ty))
                seen.add((tx, ty))
    for tx, ty in _step_targets(px, py, cells):
        if (tx, ty) in seen:
            continue
        ordered.append((tx, ty))
        seen.add((tx, ty))
    return ordered


def _try_distance_reward(session: GameSession, *, log: LogFn, result: dict) -> None:
    mine_body = mine_api.mine_list(session.client)
    _raise_if_kick(mine_body, "mine/list[reward-check]")
    mine = _mine_from(mine_body)
    dist = _int(mine.get("_distance") or mine.get("distance"))
    rdist = _int(mine.get("_rewardDistance") or mine.get("rewardDistance"))
    # Heuristic: claim when progress is ahead of recorded reward distance.
    if dist <= 0:
        return
    if rdist and dist < rdist:
        return
    resp = mine_api.distance_reward(session.client)
    _raise_if_kick(resp, "mine/reward")
    code = _code(resp)
    item = {
        "code": code,
        "message": resp.get("_message"),
        "distance": dist,
        "reward_distance": rdist,
    }
    result.setdefault("distance_rewards", []).append(item)
    if code in (0, None):
        log(f"[+] mine distance reward ok dist={dist} rdist={rdist}")
        result["distance_claimed"] = result.get("distance_claimed", 0) + 1
    else:
        log(f"[*] mine distance reward skip code={code} msg={resp.get('_message')}")


def run_mine_care(
    session: GameSession,
    *,
    login_wall: float | None = None,
    log: LogFn = print,
    max_steps: int = MAX_STEPS,
) -> dict:
    del login_wall  # stamina only; server time not required
    result: dict[str, Any] = {
        "ok": False,
        "moves": 0,
        "dashes": 0,
        "drills": 0,
        "chips": 0,
        "distance_claimed": 0,
        "stamina_start": None,
        "stamina_end": None,
        "skipped_reason": None,
        "actions": [],
        "errors": [],
        "distance_rewards": [],
    }

    goods = farm_api.goods_list(session.client)
    _raise_if_kick(goods, "goods/list[mine]")
    stamina = _goods_value(goods, mine_api.GOODS_STAMINA)
    drill = _goods_value(goods, mine_api.GOODS_DRILL)
    dash = _goods_value(goods, mine_api.GOODS_DASH)
    result["stamina_start"] = stamina
    log(f"[*] mine start stamina={stamina} drill={drill} dash={dash}")

    if stamina <= 0:
        result["ok"] = True
        result["skipped_reason"] = "no_stamina"
        result["stamina_end"] = 0
        log("[*] mine skip no stamina")
        _try_distance_reward(session, log=log, result=result)
        return result

    _try_distance_reward(session, log=log, result=result)

    steps = 0
    while steps < max_steps:
        goods = farm_api.goods_list(session.client)
        _raise_if_kick(goods, "goods/list[mine-loop]")
        stamina = _goods_value(goods, mine_api.GOODS_STAMINA)
        drill = _goods_value(goods, mine_api.GOODS_DRILL)
        dash = _goods_value(goods, mine_api.GOODS_DASH)
        if stamina <= 0:
            log("[*] mine stamina depleted")
            break

        body = mine_api.mine_list(session.client)
        _raise_if_kick(body, "mine/list")
        if _code(body) not in (0, None):
            result["errors"].append(
                {"stage": "list", "code": _code(body), "message": body.get("_message")}
            )
            log(f"[!] mine list code={_code(body)} msg={body.get('_message')}")
            break

        mine = _mine_from(body)
        cells = _cells_map(mine)
        px, py = _pos(mine)
        if not cells:
            result["errors"].append({"stage": "list", "message": "empty_cells"})
            log("[!] mine empty cell window")
            break

        # Dash: same col, deeper +row by 3 when path ahead is rocky
        if dash > 0 and _forward_rock_count(px, py, cells) >= 2:
            ty = py + 3
            log(f"[*] mine dash from=({px},{py}) toward row={ty} rocks>=2")
            resp = mine_api.cell_move(
                session.client, col=px, row=ty, move_type=mine_api.MOVE_DASH
            )
            _raise_if_kick(resp, f"mine/cell-move[dash,{px},{ty}]")
            code = _code(resp)
            result["actions"].append(
                {
                    "action": "dash",
                    "col": px,
                    "row": ty,
                    "code": code,
                    "message": resp.get("_message"),
                }
            )
            if code in (0, None):
                result["dashes"] += 1
                result["moves"] += 1
                steps += 1
                if resp.get("_rewardAllList"):
                    result["chips"] += 1
                continue
            log(f"[!] mine dash fail code={code} msg={resp.get('_message')}")
            # fall through to normal logic

        path = _path_to_reward(px, py, cells)
        if path:
            tx, ty = path[0]
            cell = cells.get((tx, ty)) or {}
            ctype = _int(cell.get("_type"))
            if ctype == CELL_ROCK:
                if drill <= 0:
                    # cannot clear; try other path step or advance
                    path = []
                else:
                    log(f"[*] mine drill reward-path ({tx},{ty})")
                    resp = mine_api.cell_broken(
                        session.client,
                        col=tx,
                        row=ty,
                        broken_type=mine_api.BROKEN_DRILL,
                    )
                    _raise_if_kick(resp, f"mine/cell-broken[{tx},{ty}]")
                    code = _code(resp)
                    result["actions"].append(
                        {
                            "action": "drill",
                            "col": tx,
                            "row": ty,
                            "code": code,
                            "message": resp.get("_message"),
                        }
                    )
                    if code in (0, None):
                        result["drills"] += 1
                        steps += 1
                        continue
                    log(f"[!] mine drill fail code={code} msg={resp.get('_message')}")
                    result["errors"].append(
                        {
                            "stage": "drill",
                            "col": tx,
                            "row": ty,
                            "code": code,
                            "message": resp.get("_message"),
                        }
                    )
                    break

            if path:
                tx, ty = path[0]
                log(f"[*] mine move to chip-path ({tx},{ty}) from=({px},{py})")
                resp = mine_api.cell_move(
                    session.client, col=tx, row=ty, move_type=mine_api.MOVE_CELL
                )
                _raise_if_kick(resp, f"mine/cell-move[{tx},{ty}]")
                code = _code(resp)
                result["actions"].append(
                    {
                        "action": "move",
                        "col": tx,
                        "row": ty,
                        "code": code,
                        "message": resp.get("_message"),
                        "goal": "chip",
                    }
                )
                if code in (0, None):
                    result["moves"] += 1
                    steps += 1
                    # reward list may include chip
                    if resp.get("_rewardAllList"):
                        result["chips"] += 1
                    continue
                log(f"[!] mine move fail code={code} msg={resp.get('_message')}")
                result["errors"].append(
                    {
                        "stage": "move",
                        "col": tx,
                        "row": ty,
                        "code": code,
                        "message": resp.get("_message"),
                    }
                )
                break

        # No chip path: advance
        progressed = False
        for tx, ty in _advance_targets(px, py, cells):
            cell = cells.get((tx, ty)) or {}
            ctype = _int(cell.get("_type"))
            if ctype == CELL_ROCK:
                if drill <= 0:
                    continue
                log(f"[*] mine drill advance-block ({tx},{ty})")
                resp = mine_api.cell_broken(
                    session.client,
                    col=tx,
                    row=ty,
                    broken_type=mine_api.BROKEN_DRILL,
                )
                _raise_if_kick(resp, f"mine/cell-broken[adv,{tx},{ty}]")
                code = _code(resp)
                result["actions"].append(
                    {
                        "action": "drill",
                        "col": tx,
                        "row": ty,
                        "code": code,
                        "message": resp.get("_message"),
                        "goal": "advance",
                    }
                )
                if code in (0, None):
                    result["drills"] += 1
                    steps += 1
                    progressed = True
                    break
                continue

            log(f"[*] mine advance move ({tx},{ty}) from=({px},{py})")
            resp = mine_api.cell_move(
                session.client, col=tx, row=ty, move_type=mine_api.MOVE_CELL
            )
            _raise_if_kick(resp, f"mine/cell-move[adv,{tx},{ty}]")
            code = _code(resp)
            result["actions"].append(
                {
                    "action": "move",
                    "col": tx,
                    "row": ty,
                    "code": code,
                    "message": resp.get("_message"),
                    "goal": "advance",
                }
            )
            if code in (0, None):
                result["moves"] += 1
                steps += 1
                if resp.get("_rewardAllList"):
                    result["chips"] += 1
                progressed = True
                break
            # try next candidate
            log(f"[*] mine advance candidate fail ({tx},{ty}) code={code}")

        if not progressed:
            log("[*] mine no legal action; stop")
            result["skipped_reason"] = "stuck"
            break

    goods = farm_api.goods_list(session.client)
    _raise_if_kick(goods, "goods/list[mine-end]")
    result["stamina_end"] = _goods_value(goods, mine_api.GOODS_STAMINA)
    _try_distance_reward(session, log=log, result=result)
    result["ok"] = True
    log(
        f"[+] mine done moves={result['moves']} dashes={result['dashes']} "
        f"drills={result['drills']} chips~={result['chips']} "
        f"stamina={result['stamina_start']}->{result['stamina_end']} "
        f"distClaim={result['distance_claimed']}"
    )
    return result
