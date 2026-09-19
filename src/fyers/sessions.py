"""Fail-closed NSE recording-session calendar."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

IST = ZoneInfo("Asia/Kolkata")


class SessionPhase(str, Enum):
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    BEFORE_SESSION = "before_session"
    RECORDING_WINDOW = "recording_window"
    AFTER_SESSION = "after_session"


@dataclass(frozen=True)
class SessionState:
    phase: SessionPhase
    eligible: bool
    reason: str
    now: str
    session_date: str
    next_session_start: str | None

    def as_dict(self) -> dict:
        result = asdict(self)
        result["phase"] = self.phase.value
        return result


class NseSessionCalendar:
    """Local, reviewed calendar. Invalid or wrong-year data closes the gate."""

    def __init__(self, path: Path, start: str, end: str) -> None:
        self.path, self.start, self.end = path, start, end

    @staticmethod
    def _local(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(IST)
        return now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)

    def _holidays(self, year: int) -> set[date]:
        raw = yaml.safe_load(self.path.read_text())
        if not isinstance(raw, dict) or raw.get("year") != year or not isinstance(raw.get("dates"), list):
            raise ValueError("holiday calendar missing or for the wrong year")
        dates: set[date] = set()
        for value in raw["dates"]:
            parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
            if parsed.year != year:
                raise ValueError("holiday outside calendar year")
            dates.add(parsed)
        return dates

    def _next_start(self, current: date, holidays: set[date], include_current: bool) -> str | None:
        candidate = current if include_current else current + timedelta(days=1)
        while candidate.year == current.year:
            if candidate.weekday() < 5 and candidate not in holidays:
                return f"{candidate.isoformat()}T{self.start}:00+05:30"
            candidate += timedelta(days=1)
        return None

    def state(self, now: datetime | None = None) -> SessionState:
        local = self._local(now)
        today = local.date()
        base = {"now": local.isoformat(), "session_date": today.isoformat()}
        try:
            holidays = self._holidays(today.year)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            return SessionState(SessionPhase.CALENDAR_UNAVAILABLE, False,
                                "reviewed NSE holiday calendar unavailable", **base,
                                next_session_start=None)
        if today.weekday() >= 5:
            return SessionState(SessionPhase.WEEKEND, False, "NSE regular session closed on weekends",
                                **base, next_session_start=self._next_start(today, holidays, False))
        if today in holidays:
            return SessionState(SessionPhase.HOLIDAY, False, "configured NSE trading holiday",
                                **base, next_session_start=self._next_start(today, holidays, False))
        clock = local.strftime("%H:%M")
        if clock < self.start:
            return SessionState(SessionPhase.BEFORE_SESSION, False, "before recorder start",
                                **base, next_session_start=self._next_start(today, holidays, True))
        if clock <= self.end:
            return SessionState(SessionPhase.RECORDING_WINDOW, True,
                                "within configured NSE recording window", **base,
                                next_session_start=None)
        return SessionState(SessionPhase.AFTER_SESSION, False, "after recorder stop", **base,
                            next_session_start=self._next_start(today, holidays, False))
