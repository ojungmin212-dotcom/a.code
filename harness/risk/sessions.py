"""정규장 시간. 서머타임은 zoneinfo 가 처리한다.

⚠️ 휴장일·조기폐장(미국 추수감사절 다음날 등)은 P0 범위 밖 — 아직 반영하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from harness.models import Market


@dataclass(frozen=True)
class MarketSession:
    tz: ZoneInfo
    open: time
    close: time

    def local(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("now 는 timezone-aware 여야 합니다")
        return now.astimezone(self.tz)

    def trading_date(self, now: datetime) -> date:
        """현지 날짜. 미국장 23:30 KST 개장 시점의 현지 날짜는 KST 날짜와 같다."""
        return self.local(now).date()

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        d = self.trading_date(now)
        return (datetime.combine(d, self.open, self.tz), datetime.combine(d, self.close, self.tz))

    def is_weekday(self, now: datetime) -> bool:
        return self.local(now).weekday() < 5

    def is_open(self, now: datetime) -> bool:
        start, end = self.bounds(now)
        return self.is_weekday(now) and start <= self.local(now) < end

    def minutes_since_open(self, now: datetime) -> float:
        return (self.local(now) - self.bounds(now)[0]) / timedelta(minutes=1)

    def minutes_to_close(self, now: datetime) -> float:
        return (self.bounds(now)[1] - self.local(now)) / timedelta(minutes=1)


SESSIONS: dict[Market, MarketSession] = {
    Market.KR: MarketSession(ZoneInfo("Asia/Seoul"), time(9, 0), time(15, 30)),
    Market.US: MarketSession(ZoneInfo("America/New_York"), time(9, 30), time(16, 0)),
}
