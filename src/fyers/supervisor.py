"""Bounded lifecycle owner for the read-only FYERS recorder subprocess."""

from __future__ import annotations

import asyncio
import os
import sys
import time

from src.fyers.journal import Journal
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.sessions import NseSessionCalendar


class RecorderSupervisor:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or RuntimeConfig.load()
        self.calendar = NseSessionCalendar(ROOT / self.config.holidays_file,
                                           self.config.session_start, self.config.session_end)
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.last_exit: int | None = None
        self.last_error: str | None = None
        self.manual_stop_date: str | None = None
        self._restart_times: list[float] = []
        self._last_start_attempt = 0.0
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        try:
            journal = Journal(ROOT / self.config.database)
            saved = journal.get("recorder_supervisor", {})
            journal.close()
            if isinstance(saved, dict):
                self.last_exit = saved.get("last_exit")
                self.last_error = saved.get("last_error")
                self.manual_stop_date = saved.get("manual_stop_date")
        except Exception:  # A damaged journal must not defeat the session gate.
            self.last_error = "supervisor state unavailable"

    def _persist(self) -> None:
        value = {"started_at": self.started_at, "last_exit": self.last_exit,
                 "last_error": self.last_error, "manual_stop_date": self.manual_stop_date,
                 "updated_at": time.time(), "live_enabled": False}
        try:
            journal = Journal(ROOT / self.config.database)
            with journal.db:
                journal.put("recorder_supervisor", value)
            journal.close()
        except Exception:
            self.last_error = "supervisor state persistence failed"

    def session_state(self, now=None):
        return self.calendar.state(now)

    def eligible(self, now=None) -> bool:
        return self.session_state(now).eligible

    def _observe_exit(self) -> bool:
        if self.process is None or self.process.returncode is None:
            return False
        self.last_exit = self.process.returncode
        if self.last_exit != 0:
            self.last_error = f"recorder exited with code {self.last_exit}"
        self.process = None
        self._persist()
        return True

    def status(self) -> dict:
        self._observe_exit()
        session = self.session_state()
        if self.manual_stop_date and self.manual_stop_date != session.session_date:
            self.manual_stop_date = None
            self._restart_times.clear()
            self._persist()
        running = self.process is not None and self.process.returncode is None
        return {"running": running, "pid": self.process.pid if running else None,
                "started_at": self.started_at, "last_exit": self.last_exit,
                "last_error": self.last_error, "session_eligible": session.eligible,
                "session": session.as_dict(), "manual_stop": self.manual_stop_date is not None,
                "restart_count": len(self._restart_times), "live_enabled": False}

    async def start(self, *, manual: bool = True) -> dict:
        async with self._lock:
            self._observe_exit()
            if self.process is not None:
                return self.status()
            if not self.eligible():
                raise ValueError("Recorder starts only during the configured NSE session")
            if manual:
                self.manual_stop_date = None
                self._restart_times.clear()
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", "fyers", "record", "--seconds", "86400",
                cwd=ROOT, env=os.environ.copy(), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self.started_at = time.time()
            self._last_start_attempt = time.monotonic()
            self.last_error = None
            self._persist()
            return self.status()

    async def stop(self, *, manual: bool = False) -> dict:
        async with self._lock:
            if manual:
                self.manual_stop_date = self.session_state().session_date
            if self.process is not None and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 10)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
                self.last_exit = self.process.returncode
                self.process = None
            self._persist()
            return self.status()

    def _restart_allowed(self, now: float) -> bool:
        window = self.config.recorder_restart_window_seconds
        self._restart_times = [stamp for stamp in self._restart_times if now - stamp <= window]
        return len(self._restart_times) <= self.config.recorder_max_restarts

    async def run_once(self) -> None:
        """Apply one scheduling decision; split out for deterministic tests."""
        crashed = self._observe_exit()
        session = self.session_state()
        if self.manual_stop_date and self.manual_stop_date != session.session_date:
            self.manual_stop_date = None
            self._restart_times.clear()
        if not self.config.auto_record or not session.eligible:
            if self.process is not None:
                await self.stop()
            return
        if self.manual_stop_date is not None or self.process is not None:
            return
        now = time.monotonic()
        if crashed:
            self._restart_times.append(now)
        if (crashed or self._restart_times) and not self._restart_allowed(now):
            self.last_error = "automatic recorder restart limit reached"
            self._persist()
            return
        if now - self._last_start_attempt < self.config.recorder_backoff_seconds:
            return
        try:
            await self.start(manual=False)
        except (OSError, ValueError) as exc:
            self.last_error = f"recorder start failed: {type(exc).__name__}"
            self._restart_times.append(now)
            self._persist()

    async def schedule(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.config.recorder_poll_seconds)
