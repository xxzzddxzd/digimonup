"""Always-on session heartbeat (/api/account/heartbeat every 60s)."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .session import GameSession


DEFAULT_HEARTBEAT_INTERVAL_SEC = 60.0
SESSION_KICK_CODE = -19006


class HeartbeatService:
    def __init__(
        self,
        session: GameSession,
        *,
        interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.session = session
        self.interval_sec = DEFAULT_HEARTBEAT_INTERVAL_SEC  # fixed 60s; arg ignored
        self.log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_code: Optional[int] = None
        self.kicked = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.kicked.clear()
        self.last_code = None

        def _worker() -> None:
            while not self._stop.wait(1.0):
                try:
                    resp = self.session.ensure_heartbeat(DEFAULT_HEARTBEAT_INTERVAL_SEC)
                    if resp is not None:
                        code = resp.get("_code", 0)
                        try:
                            self.last_code = int(code) if code is not None else 0
                        except Exception:
                            self.last_code = None
                        if self.last_code == SESSION_KICK_CODE:
                            self.kicked.set()
                            self.log(f"[!] heartbeat session kick code={self.last_code}")
                        else:
                            self.log(f"[*] heartbeat ok code={code}")
                except Exception as exc:
                    self.log(f"[-] heartbeat failed: {exc}")
                    msg = str(exc)
                    if "19006" in msg:
                        self.kicked.set()

        self._thread = threading.Thread(target=_worker, name="heartbeat", daemon=True)
        self._thread.start()
        self.log(f"[*] heartbeat loop on, interval={DEFAULT_HEARTBEAT_INTERVAL_SEC:.0f}s")

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None

    def rebind(self, session: GameSession) -> None:
        """Point heartbeat at a new session after re-auth."""
        self.session = session
        self.kicked.clear()
        self.last_code = None
