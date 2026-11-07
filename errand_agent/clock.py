"""UTC persistence clock with a fixed IST business view for dates and slots."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc


class Clock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def now_ist(self) -> datetime:
        return self.now_utc().astimezone(IST)

    def epoch(self) -> float:
        return self.now_utc().timestamp()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class FakeClock(Clock):
    def __init__(self, initial: datetime | None = None) -> None:
        self._now = (initial or datetime(2026, 11, 7, 10, 0, tzinfo=IST)).astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
