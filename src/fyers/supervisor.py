"""Bounded lifecycle owner for the read-only FYERS recorder subprocess."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta

from src.fyers.journal import Journal
from src.fyers.models import ROOT, RuntimeConfig
from src.fyers.sessions import IST, NseSessionCalendar, SessionPhase


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
        self._last_finalization_attempt = 0.0
        self._finalization_attempts = 0
        self.finalization: dict | None = None
        self._finalizing = False
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        try:
            journal = Journal(ROOT / self.config.database)
            saved = journal.get("recorder_supervisor", {})
            if isinstance(saved, dict):
                self.last_exit = saved.get("last_exit")
                self.last_error = saved.get("last_error")
                self.manual_stop_date = saved.get("manual_stop_date")
                self._finalization_attempts = int(saved.get("finalization_attempts", 0))
                self._last_finalization_attempt = float(saved.get("last_finalization_attempt", 0))
            self.finalization = journal.get("session_finalization")
            journal.close()
        except Exception:  # A damaged journal must not defeat the session gate.
            self.last_error = "supervisor state unavailable"

    def _persist(self) -> None:
        value = {"started_at": self.started_at, "last_exit": self.last_exit,
                 "last_error": self.last_error, "manual_stop_date": self.manual_stop_date,
                 "finalization_attempts": self._finalization_attempts,
                 "last_finalization_attempt": self._last_finalization_attempt,
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
        from src.fyers.qualification import qualification

        qualified, _ = qualification(
            ROOT / self.config.qualification_file, ROOT / self.config.trials_file,
        )
        authentication = None
        try:
            journal = Journal(ROOT / self.config.database)
            authentication = journal.get("authentication")
            self.finalization = journal.get("session_finalization")
            journal.close()
        except Exception:
            pass
        if isinstance(authentication, dict) and authentication.get("state") == "AUTH_REQUIRED":
            lifecycle = "AUTH_REQUIRED"
        elif running:
            lifecycle = "RECORDING"
        elif self._finalizing:
            lifecycle = "FINALIZING"
        elif qualified:
            lifecycle = "QUALIFIED_PAPER"
        elif session.phase is SessionPhase.BEFORE_SESSION:
            lifecycle = "PREOPEN"
        elif isinstance(self.finalization, dict) and self.finalization.get("status") == "complete":
            lifecycle = "RESEARCH_READY"
        else:
            lifecycle = "OBSERVATION_ONLY"
        return {"running": running, "pid": self.process.pid if running else None,
                "started_at": self.started_at, "last_exit": self.last_exit,
                "last_error": self.last_error, "session_eligible": session.eligible,
                "session": session.as_dict(), "manual_stop": self.manual_stop_date is not None,
                "restart_count": len(self._restart_times), "live_enabled": False,
                "lifecycle": lifecycle, "authentication": authentication,
                "finalization": self.finalization,
                "finalization_attempts": self._finalization_attempts,
                "strategy_qualified": qualified}

    async def start(self, *, manual: bool = True) -> dict:
        async with self._lock:
            self._observe_exit()
            if self.process is not None:
                return self.status()
            status = self.status()
            authentication = status.get("authentication")
            if isinstance(authentication, dict) and authentication.get("state") == "AUTH_REQUIRED":
                raise ValueError("FYERS authentication required; renew the daily access token")
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

    def _pending_session_date(self):
        try:
            journal = Journal(ROOT / self.config.database)
            row = journal.db.execute(
                "SELECT received FROM events WHERE kind='tick' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            finalized = journal.get("session_finalization", {})
            journal.close()
            if not row:
                return None
            day = datetime.fromtimestamp(row[0], IST).date()
            if isinstance(finalized, dict) and finalized.get("session_date") == day.isoformat() \
                    and finalized.get("status") == "complete":
                return None
            _, end = self.calendar.session_bounds(day)
            if datetime.now(IST) < end + timedelta(minutes=self.config.finalization_delay_minutes):
                return None
            return day
        except (OSError, ValueError):
            return None

    async def _finalize_pending(self) -> None:
        day = self._pending_session_date()
        if day is None or self._finalizing:
            return
        if isinstance(self.finalization, dict) and self.finalization.get("session_date") != day.isoformat():
            self._finalization_attempts = 0
        if self._finalization_attempts >= self.config.finalization_max_retries:
            self.last_error = "session finalization retry limit reached"
            return
        now = time.monotonic()
        if now - self._last_finalization_attempt < self.config.finalization_backoff_seconds:
            return
        from src.fyers.operations import finalize_session
        self._finalizing = True
        self._finalization_attempts += 1
        self._last_finalization_attempt = now
        try:
            self.finalization = await finalize_session(self.config, day)
            if self.finalization.get("status") != "complete":
                self.last_error = "session finalization incomplete"
            else:
                self._finalization_attempts = 0
        except Exception as exc:
            self.last_error = f"session finalization failed: {type(exc).__name__}"
        finally:
            self._finalizing = False
            self._persist()

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
            await self._finalize_pending()
            return
        status = self.status()
        authentication = status.get("authentication")
        if isinstance(authentication, dict) and authentication.get("state") == "AUTH_REQUIRED":
            self.last_error = "FYERS authentication required"
            self._persist()
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
