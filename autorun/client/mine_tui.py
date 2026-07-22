"""Interactive Textual TUI for 数码世界 / mine explore.

Live coords:
  - _col: 0..4 (lanes) — table rows
  - _row: depth (…3625…) — table columns, right = deeper

Board is a DataTable 5×7 so every cell is the same size.
Click: empty/芯 = move; 石 = drill (must be in legal range).
Board locks until HTTP + refresh finish.

Usage: python3 main.py ts
"""
from __future__ import annotations

import traceback
from typing import Any, Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Log, Static
from textual.worker import get_current_worker

from .apis import farm as farm_api
from .apis import mine as mine_api
from .heartbeat import HeartbeatService
from .mine_care import (
    CELL_EMPTY,
    CELL_REWARD,
    CELL_ROCK,
    _cells_map,
    _code,
    _goods_value,
    _int,
    _mine_from,
    _pos,
    is_legal_move,
)
from .session import GameSession

LogFn = Callable[[str], None]

# Server window: 5 lanes × 7 depth
BOARD_W = 7
BOARD_H = 5
CODE_NO_GOODS = -31002


def _cell_type(cell: dict | None) -> int:
    if not cell:
        return CELL_EMPTY
    return _int(cell.get("_type") or cell.get("type"))


def _visited(cell: dict | None) -> bool:
    if not cell:
        return False
    v = cell.get("_isVisited") or cell.get("isVisited") or cell.get("visited")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


def _reward_key(cell: dict | None) -> int | None:
    if not cell:
        return None
    raw = cell.get("_rewardKey", cell.get("rewardKey"))
    if raw is None:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _window_1d(present: list[int], center: int, size: int) -> list[int]:
    present = sorted(set(present))
    if not present:
        half = size // 2
        return list(range(center - half, center - half + size))
    if len(present) >= size:
        if center in present:
            idx = present.index(center)
            start = max(0, min(idx - size // 2, len(present) - size))
            return present[start : start + size]
        if center < present[0]:
            return present[:size]
        return present[-size:]
    lo, hi = present[0], present[-1]
    mid = center if lo <= center <= hi else (lo + hi) // 2
    start = mid - size // 2
    return list(range(start, start + size))


def window_depth_rows(cells: dict[tuple[int, int], dict], py: int) -> list[int]:
    return _window_1d([r for (_, r) in cells.keys()], py, BOARD_W)


def window_lanes(cells: dict[tuple[int, int], dict], px: int) -> list[int]:
    return _window_1d([c for (c, _) in cells.keys()], px, BOARD_H)


def _glyph(
    *,
    col: int,
    row: int,
    cell: dict | None,
    px: int,
    py: int,
    pending: tuple[int, int] | None,
    legal: set[tuple[int, int]],
    locked: bool,
) -> Text:
    """Fixed-width 3-char cell content, equal in every table cell."""
    # always 3 display columns wide with spaces
    def pack(ch: str, style: str) -> Text:
        # center one CJK / symbol in 3 cols
        return Text(f" {ch} ", style=style)

    if pending == (col, row) and (col, row) != (px, py):
        return pack("…", "bold #11111b on #fab387")
    if (col, row) == (px, py):
        return pack("我", "bold #11111b on #f5c2e7")

    ctype = _cell_type(cell)
    vis = _visited(cell)
    is_legal = (not locked) and ((col, row) in legal)

    if cell is None:
        return pack("·", "#313244 on #11111b")

    if ctype == CELL_ROCK:
        return pack("石", "bold #cdd6f4 on #313244")

    if ctype == CELL_REWARD:
        if vis:
            return pack("取", "#6c7086 on #1e1e2e")
        return pack("芯", "bold #f9e2af on #2a2410")

    # empty
    if is_legal:
        return pack("·", "#89dceb on #252536")
    if vis:
        return pack("·", "#45475a on #1e1e2e")
    return pack("·", "#585b70 on #1e1e2e")


class MineTUIApp(App[int]):
    TITLE = "DIGIMON UP · 数码世界"
    CSS = """
    Screen {
        layout: vertical;
        background: #11111b;
    }
    #status {
        height: 3;
        padding: 0 1;
        background: #181825;
        color: #cdd6f4;
        border: solid #313244;
    }
    #you-banner {
        height: 1;
        padding: 0 1;
        background: #181825;
        color: #f5c2e7;
        text-style: bold;
        text-align: center;
        border-bottom: solid #313244;
    }
    #board-title {
        height: 1;
        width: 100%;
        content-align: center middle;
        color: #89b4fa;
        text-style: bold;
        margin-top: 1;
    }
    #board-panel {
        height: auto;
        width: 100%;
        align: center middle;
        padding: 1 2;
    }
    #board-table {
        width: auto;
        height: auto;
        min-height: 9;
        max-height: 12;
        background: #1e1e2e;
        border: solid #585b70;
        padding: 0 1;
    }
    DataTable {
        height: auto;
    }
    DataTable > .datatable--header {
        background: #181825;
        color: #89b4fa;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #45475a;
        color: #cdd6f4;
    }
    DataTable > .datatable--hover {
        background: #313244;
    }
    #legend {
        height: 1;
        width: 100%;
        content-align: center middle;
        color: #6c7086;
        margin-bottom: 1;
    }
    #toolbar {
        height: 3;
        padding: 0 1;
        layout: horizontal;
        align: center middle;
    }
    #toolbar Button {
        margin: 0 1;
        min-width: 10;
        background: #313244;
        color: #cdd6f4;
        border: none;
    }
    #toolbar Button:hover {
        background: #45475a;
    }
    #toolbar Button:disabled {
        color: #585b70;
        background: #1e1e2e;
    }
    #log {
        height: 1fr;
        border: solid #313244;
        margin: 0 1 0 1;
        background: #181825;
        color: #a6adc8;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "退出", show=True),
        Binding("r", "refresh", "刷新", show=True),
        Binding("f", "dash", "冲锋", show=True),
        Binding("c", "claim", "里程", show=True),
        Binding("enter", "confirm_cell", "确认", show=False),
    ]

    def __init__(self, session: GameSession, *, http_log: bool = False) -> None:
        super().__init__()
        self.session = session
        self.session.client.log_enabled = http_log
        self.hb: HeartbeatService | None = None
        self.busy = False
        self.stamina = 0
        self.drill = 0
        self.dash = 0
        self.px = 0
        self.py = 0
        self.distance = 0
        self.reward_distance = 0
        self.cells: dict[tuple[int, int], dict] = {}
        self.depth_rows: list[int] = []
        self.lanes: list[int] = []
        self._pending: tuple[int, int] | None = None
        self._busy_reason = ""
        self._exit_code = 0
        self._table_ready = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("登录中…", id="status")
        yield Static("▶ 加载中…", id="you-banner")
        yield Static("5×7", id="board-title")
        with Vertical(id="board-panel"):
            yield DataTable(
                id="board-table",
                cursor_type="cell",
                zebra_stripes=False,
                show_header=True,
                show_row_labels=False,
            )
        yield Static("我=你  石=岩  芯=道具  取=已捡  ·=空/可走  …=请求中", id="legend")
        with Horizontal(id="toolbar"):
            yield Button("刷新 [r]", id="btn-refresh")
            yield Button("冲锋 [f]", id="btn-dash")
            yield Button("里程 [c]", id="btn-claim")
            yield Button("退出 [q]", id="btn-quit")
        yield Log(id="log", highlight=True, max_lines=200, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#board-table", DataTable)
        table.cursor_type = "cell"
        # wider columns so each cell looks square-ish
        table.styles.width = "auto"
        self._log("[*] 5×7 表格式棋盘 | 点格/回车操作 | 点石=钻 点空=走")
        self._bootstrap()

    # ── logging / status ─────────────────────────────────────────

    def _log(self, msg: str) -> None:
        try:
            log = self.query_one("#log", Log)
            text = str(msg).rstrip("\n")
            if hasattr(log, "write_line"):
                log.write_line(text)
            else:
                log.write(text + "\n")
            try:
                log.scroll_end(animate=False)
            except Exception:
                pass
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#status", Static).update(text)
        except Exception:
            pass
        try:
            self.query_one("#you-banner", Static).update(self._you_banner())
        except Exception:
            pass

    def _you_banner(self) -> str:
        if self.stamina <= 0:
            return (
                f"⚠ 体力=0 无法移动  可点石用钻  "
                f"@{self.px},{self.py}  钻{self.drill} 冲{self.dash}"
            )
        return (
            f"▶ @{self.px},{self.py}  体力{self.stamina} "
            f"钻{self.drill} 冲{self.dash}  右=+深"
        )

    def _status_line(self) -> str:
        wait = ""
        if self.busy:
            pend = (
                f"→{self._pending[0]},{self._pending[1]}"
                if self._pending
                else ""
            )
            wait = f"  ⏳{self._busy_reason}{pend}"
        stam = f"体力{self.stamina}" + ("⚠" if self.stamina <= 0 else "")
        return (
            f"【@{self.px},{self.py}】 {stam} 钻{self.drill} 冲{self.dash} "
            f"距离{self.distance}/{self.reward_distance}{wait}\n"
            f"5×7 表  道=行  深=列→  |  -31002=体力不足"
        )

    @staticmethod
    def _fmt_err(code, msg) -> str:
        if code == CODE_NO_GOODS:
            return f"code={code} 体力不足(Mine_Stamina) msg={msg}"
        return f"code={code} msg={msg}"

    # ── snapshot ─────────────────────────────────────────────────

    def _fetch_snapshot(self) -> dict[str, Any]:
        goods = farm_api.goods_list(self.session.client)
        body = mine_api.mine_list(self.session.client)
        mine = _mine_from(body)
        cells = _cells_map(mine)
        px, py = _pos(mine)
        return {
            "stamina": _goods_value(goods, mine_api.GOODS_STAMINA),
            "drill": _goods_value(goods, mine_api.GOODS_DRILL),
            "dash": _goods_value(goods, mine_api.GOODS_DASH),
            "list_code": _code(body),
            "list_msg": body.get("_message"),
            "cells": cells,
            "px": px,
            "py": py,
            "distance": _int(mine.get("_distance") or mine.get("distance")),
            "reward_distance": _int(
                mine.get("_rewardDistance") or mine.get("rewardDistance")
            ),
            "depth_rows": window_depth_rows(cells, py),
            "lanes": window_lanes(cells, px),
        }

    def _apply_snapshot(self, snap: dict[str, Any], *, unlock: bool = True) -> None:
        if snap.get("list_code") not in (0, None):
            self._log(
                f"[!] list code={snap.get('list_code')} msg={snap.get('list_msg')}"
            )
        self.stamina = int(snap["stamina"])
        self.drill = int(snap["drill"])
        self.dash = int(snap["dash"])
        self.px = int(snap["px"])
        self.py = int(snap["py"])
        self.distance = int(snap["distance"])
        self.reward_distance = int(snap["reward_distance"])
        self.cells = snap["cells"]
        self.depth_rows = list(snap["depth_rows"])
        self.lanes = list(snap["lanes"])
        if unlock:
            self.busy = False
            self._busy_reason = ""
            self._pending = None
        self._rebuild_board()
        self._apply_toolbar_lock()
        self._set_status(self._status_line())
        n_star = n_rock = 0
        for (c, r), cell in self.cells.items():
            if c not in self.lanes or r not in self.depth_rows:
                continue
            t = _cell_type(cell)
            if t == CELL_ROCK:
                n_rock += 1
            elif t == CELL_REWARD and not _visited(cell):
                n_star += 1
        warn = " ⚠体力=0" if self.stamina <= 0 else ""
        self._log(
            f"[*] {'解锁' if unlock else '更新'} "
            f"@{self.px},{self.py} 体力{self.stamina} 钻{self.drill} "
            f"芯{n_star} 石{n_rock}{warn}"
        )

    @work(thread=True, exclusive=True)
    def _bootstrap(self) -> None:
        worker = get_current_worker()
        try:
            self.session.run_login_pipeline()
            if worker.is_cancelled:
                return
            self.app.call_from_thread(self._log, "[+] login ok")
            self.hb = HeartbeatService(
                self.session,
                log=lambda m: self.app.call_from_thread(self._log, m),
            )
            self.hb.start()
            snap = self._fetch_snapshot()
            if worker.is_cancelled:
                return
            self.app.call_from_thread(self._apply_snapshot, snap, unlock=True)
        except Exception as exc:
            self.app.call_from_thread(self._log, f"[-] bootstrap: {exc}")
            self.app.call_from_thread(self._log, traceback.format_exc())
            self.app.call_from_thread(self._force_unlock)
            self._exit_code = 1

    @work(thread=True, exclusive=True)
    def refresh_state(self) -> None:
        worker = get_current_worker()
        try:
            snap = self._fetch_snapshot()
            if worker.is_cancelled:
                return
            self.app.call_from_thread(self._apply_snapshot, snap, unlock=True)
        except Exception as exc:
            self.app.call_from_thread(self._log, f"[-] refresh: {exc}")
            self.app.call_from_thread(self._log, traceback.format_exc())
            self.app.call_from_thread(self._force_unlock)

    def _force_unlock(self) -> None:
        self.busy = False
        self._busy_reason = ""
        self._pending = None
        try:
            self._rebuild_board()
        except Exception:
            pass
        self._apply_toolbar_lock()
        self._set_status(self._status_line())

    # ── board (DataTable) ────────────────────────────────────────

    def _rebuild_board(self) -> None:
        depth = list(self.depth_rows or window_depth_rows(self.cells, self.py))
        lanes = list(self.lanes or window_lanes(self.cells, self.px))
        if len(depth) != BOARD_W:
            depth = _window_1d(depth or [self.py], self.py, BOARD_W)
        if len(lanes) != BOARD_H:
            lanes = _window_1d(lanes or [self.px], self.px, BOARD_H)
        self.depth_rows = depth
        self.lanes = lanes

        legal: set[tuple[int, int]] = set()
        for c in lanes:
            for r in depth:
                if is_legal_move(self.px, self.py, c, r):
                    legal.add((c, r))

        table = self.query_one("#board-table", DataTable)
        # Remember cursor if possible
        try:
            old = table.cursor_coordinate
        except Exception:
            old = None

        table.clear(columns=True)
        # col0 = lane label; col1..7 = depth
        headers = ["#"] + [f"{r % 100:02d}" for r in depth]
        table.add_columns(*headers)

        for c in lanes:
            row_cells: list[Text | str] = [
                Text(f" {c} ", style="bold #f5c2e7 on #313244")
                if c == self.px
                else Text(f" {c} ", style="#6c7086 on #181825")
            ]
            for r in depth:
                row_cells.append(
                    _glyph(
                        col=c,
                        row=r,
                        cell=self.cells.get((c, r)),
                        px=self.px,
                        py=self.py,
                        pending=self._pending,
                        legal=legal,
                        locked=self.busy,
                    )
                )
            table.add_row(*row_cells)

        table.disabled = self.busy
        self._table_ready = True

        # put cursor on player cell
        try:
            pr = lanes.index(self.px)
            pc = depth.index(self.py) + 1  # +1 for lane col
            table.move_cursor(row=pr, column=pc, animate=False)
        except Exception:
            if old is not None:
                try:
                    table.move_cursor(
                        row=old.row, column=old.column, animate=False
                    )
                except Exception:
                    pass

        try:
            self.query_one("#board-title", Static).update(
                f"5×7  @{self.px},{self.py}  "
                f"深{depth[0]}→{depth[-1]}  道{lanes[0]}↓{lanes[-1]}"
            )
        except Exception:
            pass

    def _lock_board(
        self, *, reason: str, pending: tuple[int, int] | None = None
    ) -> None:
        self.busy = True
        self._busy_reason = reason
        self._pending = pending
        self._apply_toolbar_lock()
        # repaint pending mark
        self._rebuild_board()
        self._set_status(self._status_line())
        pend_s = f" {pending[0]},{pending[1]}" if pending else ""
        self._log(f"[*] 锁定 — {reason}{pend_s}")

    def _apply_toolbar_lock(self) -> None:
        try:
            for btn in self.query(Button):
                bid = str(btn.id or "")
                if bid.startswith("btn-") and bid != "btn-quit":
                    btn.disabled = self.busy
            table = self.query_one("#board-table", DataTable)
            table.disabled = self.busy
        except Exception:
            pass

    # ── input ────────────────────────────────────────────────────

    def _coords_from_table(self, row: int, column: int) -> tuple[int, int] | None:
        """Map table coordinate → world (col, row). column 0 is lane label."""
        if column <= 0:
            return None
        if row < 0 or row >= len(self.lanes):
            return None
        di = column - 1
        if di < 0 or di >= len(self.depth_rows):
            return None
        return self.lanes[row], self.depth_rows[di]

    def _handle_cell(self, col: int, row: int) -> None:
        if self.busy:
            return
        if (col, row) == (self.px, self.py):
            self._log("[*] already here")
            return
        if not is_legal_move(self.px, self.py, col, row):
            self._log(
                f"[!] 超出范围 ({self.px},{self.py})→({col},{row}) 需 col±1 或 row±1"
            )
            return
        cell = self.cells.get((col, row))
        ctype = _cell_type(cell)
        if ctype == CELL_ROCK:
            if self.drill <= 0:
                self._log("[!] 没有钻头")
                return
            self._lock_board(reason="钻头", pending=(col, row))
            self._run_action("drill", col=col, row=row)
            return
        if self.stamina <= 0:
            self._log("[!] 体力=0 无法移动(-31002)，可点石用钻")
            return
        self._lock_board(reason="移动", pending=(col, row))
        self._run_action("move", col=col, row=row, move_type=mine_api.MOVE_CELL)

    @on(DataTable.CellSelected)
    def _on_table_cell(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id != "board-table":
            return
        if self.busy:
            return
        coord = event.coordinate
        mapped = self._coords_from_table(coord.row, coord.column)
        if mapped is None:
            return
        self._handle_cell(mapped[0], mapped[1])

    def action_confirm_cell(self) -> None:
        if self.busy:
            return
        try:
            table = self.query_one("#board-table", DataTable)
            coord = table.cursor_coordinate
            mapped = self._coords_from_table(coord.row, coord.column)
            if mapped:
                self._handle_cell(mapped[0], mapped[1])
        except Exception:
            pass

    @on(Button.Pressed, "#btn-refresh")
    def _on_refresh(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#btn-dash")
    def _on_dash_btn(self) -> None:
        self.action_dash()

    @on(Button.Pressed, "#btn-claim")
    def _on_claim_btn(self) -> None:
        self.action_claim()

    @on(Button.Pressed, "#btn-quit")
    def _on_quit_btn(self) -> None:
        self.action_quit()

    def action_refresh(self) -> None:
        if self.busy:
            return
        self._lock_board(reason="刷新", pending=None)
        self._run_action("refresh")

    def action_dash(self) -> None:
        if self.busy:
            return
        if self.dash <= 0:
            self._log("[!] 没有冲锋")
            return
        if self.stamina <= 0:
            self._log("[!] 体力=0，冲锋也会失败")
            return
        ty = self.py + 3
        self._log(f"[*] 冲锋 {self.px},{self.py}→{self.px},{ty}")
        self._lock_board(reason="冲锋", pending=(self.px, ty))
        self._run_action("move", col=self.px, row=ty, move_type=mine_api.MOVE_DASH)

    def action_claim(self) -> None:
        if self.busy:
            return
        self._lock_board(reason="里程", pending=None)
        self._run_action("claim")

    def action_quit(self) -> None:
        self._shutdown()
        self.exit(self._exit_code)

    def _shutdown(self) -> None:
        try:
            if self.hb is not None:
                self.hb.stop()
        except Exception:
            pass
        self.hb = None

    def on_unmount(self) -> None:
        self._shutdown()

    @work(thread=True, exclusive=True)
    def _run_action(
        self,
        kind: str,
        *,
        col: int = 0,
        row: int = 0,
        move_type: int = 0,
    ) -> None:
        worker = get_current_worker()
        try:
            if kind == "move":
                resp = mine_api.cell_move(
                    self.session.client, col=col, row=row, move_type=move_type
                )
                code = _code(resp)
                tag = "dash" if move_type == mine_api.MOVE_DASH else "move"
                if code in (0, None):
                    self.app.call_from_thread(
                        self._log,
                        f"[+] {tag} ok → ({col},{row})"
                        + (" +reward" if resp.get("_rewardAllList") else ""),
                    )
                else:
                    self.app.call_from_thread(
                        self._log,
                        f"[-] {tag} fail {self._fmt_err(code, resp.get('_message'))}",
                    )
            elif kind == "drill":
                resp = mine_api.cell_broken(
                    self.session.client,
                    col=col,
                    row=row,
                    broken_type=mine_api.BROKEN_DRILL,
                )
                code = _code(resp)
                if code in (0, None):
                    self.app.call_from_thread(
                        self._log, f"[+] drill ok ({col},{row})"
                    )
                else:
                    self.app.call_from_thread(
                        self._log,
                        f"[-] drill fail {self._fmt_err(code, resp.get('_message'))}",
                    )
            elif kind == "claim":
                resp = mine_api.distance_reward(self.session.client)
                code = _code(resp)
                if code in (0, None):
                    self.app.call_from_thread(self._log, "[+] distance reward ok")
                else:
                    self.app.call_from_thread(
                        self._log,
                        f"[*] distance {self._fmt_err(code, resp.get('_message'))}",
                    )
            elif kind == "refresh":
                pass

            if worker.is_cancelled:
                return
            snap = self._fetch_snapshot()
            if worker.is_cancelled:
                return
            self.app.call_from_thread(self._apply_snapshot, snap, unlock=True)
        except Exception as exc:
            self.app.call_from_thread(self._log, f"[-] {kind} error: {exc}")
            self.app.call_from_thread(self._log, traceback.format_exc())
            self.app.call_from_thread(self._force_unlock)


def run_mine_tui(session: GameSession, *, http_log: bool = False) -> int:
    app = MineTUIApp(session, http_log=http_log)
    return int(app.run() or 0)
