"""Rich Live TUI dashboard for farm/session status."""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .runtime_state import RuntimeState


def _short_url(url: str) -> str:
    try:
        p = urlparse(url)
        return p.path or url
    except Exception:
        return url


def _kv_table(rows: list[tuple[str, str]], title: str | None = None) -> Table:
    t = Table(show_header=False, box=None, expand=True, padding=(0, 1))
    t.add_column("k", style="bold cyan", ratio=1)
    t.add_column("v", style="white", ratio=3)
    for k, v in rows:
        t.add_row(k, str(v))
    return t


def build_dashboard(state: RuntimeState) -> Layout:
    snap = state.snapshot()
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    layout["left"].split_column(
        Layout(name="session", size=9),
        Layout(name="target", size=8),
        Layout(name="drops", ratio=1),
    )
    layout["right"].split_column(
        Layout(name="http", size=10),
        Layout(name="events", ratio=1),
    )

    title = Text("DIGIMON UP  ·  Protocol Client", style="bold white")
    sub = Text(
        f"mode={snap['mode']}  status={snap['status']}  recover={snap['recover']}",
        style="dim",
    )
    layout["header"].update(
        Panel(Align.center(Group(title, sub)), box=box.HEAVY, style="blue")
    )

    sk = snap["session_key"] or "-"
    if len(sk) > 18:
        sk = sk[:8] + "…" + sk[-6:]
    session_rows = [
        ("UID", snap["public_uid"] or "-"),
        ("Server", snap["server_num"] if snap["server_num"] != "" else "-"),
        ("Session", sk),
        ("Runs", f"{snap['runs']}  win={snap['wins']}  fail={snap['fails']}"),
        ("Loop", f"{snap['loop_i']}/{snap['loop_total']}"),
    ]
    layout["session"].update(Panel(_kv_table(session_rows), title="Session", border_style="cyan"))

    target_rows = [
        ("Region", snap["region"]),
        ("Stage", snap["stage"]),
        ("Sector", snap["sector"]),
        ("Repeat", snap["repeat"]),
        ("Last drops", snap["last_drops"] or "-"),
    ]
    layout["target"].update(Panel(_kv_table(target_rows), title="Current Stage", border_style="green"))

    drop_table = Table(expand=True, box=box.SIMPLE_HEAVY)
    drop_table.add_column("Item", style="yellow")
    drop_table.add_column("Total", justify="right")
    drop_table.add_column("Stage wins", justify="left", style="dim")
    totals = snap["drop_totals"] or {}
    if totals:
        for label, total in sorted(totals.items(), key=lambda x: (-x[1], x[0]))[:12]:
            drop_table.add_row(label, str(total), "")
    else:
        drop_table.add_row("(none)", "0", "")
    sw = snap["stage_wins"] or {}
    if sw:
        drop_table.add_row("", "", ", ".join(f"{k}:{v}" for k, v in list(sw.items())[-6:]))
    layout["drops"].update(Panel(drop_table, title="Drop Stats", border_style="magenta"))

    http_table = Table(expand=True, box=box.SIMPLE)
    http_table.add_column("T", width=8, style="dim")
    http_table.add_column("HTTP", width=6)
    http_table.add_column("Path")
    http_table.add_column("ms", justify="right", width=7)
    for ev in (snap["http_events"] or [])[:8]:
        ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        if ev.phase == "req":
            http_table.add_row(ts, "...", _short_url(ev.url), "")
        else:
            st = str(ev.status) if ev.status is not None else "?"
            style = "green" if (ev.status or 0) < 400 else "red"
            http_table.add_row(ts, Text(st, style=style), _short_url(ev.url), f"{ev.ms:.0f}" if ev.ms else "")
    layout["http"].update(Panel(http_table, title="HTTP", border_style="white"))

    ev_text = Text()
    events = snap["events"] or []
    if not events:
        ev_text.append("(waiting)", style="dim")
    else:
        for line in events[:16]:
            style = "red" if "fail" in line.lower() or "[-]" in line else (
                "yellow" if "recover" in line.lower() else "white"
            )
            ev_text.append(line + "\n", style=style)
    layout["events"].update(Panel(ev_text, title="Events", border_style="yellow"))

    err = snap["last_error"] or "-"
    foot = Text(f"Last HTTP: {snap['last_http']}\nLast error: {err}", style="dim")
    layout["footer"].update(Panel(foot, box=box.ROUNDED, border_style="dim"))
    return layout


class FarmTUI:
    def __init__(self, state: RuntimeState, *, refresh_hz: float = 4.0):
        self.state = state
        self.console = Console()
        self.refresh_hz = refresh_hz
        self._live: Optional[Live] = None

    def __enter__(self) -> "FarmTUI":
        self._live = Live(
            build_dashboard(self.state),
            console=self.console,
            refresh_per_second=self.refresh_hz,
            screen=self.console.is_terminal,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def tick(self) -> None:
        if self._live is not None:
            self._live.update(build_dashboard(self.state))

    def run_until(self, done_flag, interval: float = 0.25) -> None:
        while not done_flag.is_set():
            self.tick()
            done_flag.wait(interval)
        self.tick()
