"""정규장 시간. 서머타임은 zoneinfo, 휴장일·늦은 개장·조기폐장은 exchange_calendars(F6)가 처리한다.

exchange_calendars 가 없거나 달력 범위(약 1년 뒤까지)를 벗어나면 평일·고정 시간으로 대체하고 한 번 경고한다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from harness.models import Market

_warned: set[str] = set()


def _warn(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"⚠️ {msg}", file=sys.stderr)


@lru_cache(maxsize=4)
def _calendar(code: str):
    try:
        import exchange_calendars as xc
    except ImportError:
        _warn("exchange_calendars 미설치 — 휴장일 미반영(평일 고정 시간 사용)")
        return None
    return xc.get_calendar(code)


@dataclass(frozen=True)
class MarketSession:
    tz: ZoneInfo
    open: time
    close: time
    calendar: str | None = None   # XKRX / XNYS
    _cache: dict = field(default_factory=dict, compare=False, hash=False, repr=False)

    def local(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("now 는 timezone-aware 여야 합니다")
        return now.astimezone(self.tz)

    def trading_date(self, now: datetime) -> date:
        """현지 날짜. 미국장 23:30 KST 개장 시점의 현지 날짜는 KST 날짜와 같다."""
        return self.local(now).date()

    def _day(self, d: date) -> tuple[bool, datetime, datetime]:
        """(거래일 여부, 개장, 마감) — 날짜별 캐시."""
        if d in self._cache:
            return self._cache[d]
        default = (d.weekday() < 5, datetime.combine(d, self.open, self.tz),
                   datetime.combine(d, self.close, self.tz))
        result = default
        cal = _calendar(self.calendar) if self.calendar else None
        if cal is not None:
            import pandas as pd
            ts = pd.Timestamp(d)
            if cal.first_session <= ts <= cal.last_session:
                if cal.is_session(ts):
                    result = (True, cal.session_open(ts).to_pydatetime().astimezone(self.tz),
                              cal.session_close(ts).to_pydatetime().astimezone(self.tz))
                else:
                    result = (False, default[1], default[2])
            else:
                _warn(f"{self.calendar} 달력 범위 밖({d}) — 평일 고정 시간 사용. exchange_calendars 를 업데이트하세요")
        self._cache[d] = result
        return result

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        _, o, c = self._day(self.trading_date(now))
        return o, c

    def is_trading_day(self, now: datetime) -> bool:
        return self._day(self.trading_date(now))[0]

    def is_weekday(self, now: datetime) -> bool:  # 이전 이름 호환
        return self.is_trading_day(now)

    def is_open(self, now: datetime) -> bool:
        start, end = self.bounds(now)
        return self.is_trading_day(now) and start <= self.local(now) < end

    def done_for_day(self, now: datetime) -> bool:
        """오늘(현지) 더 이상 정규장이 없다: 휴장일이거나 마감 이후."""
        return not self.is_trading_day(now) or self.minutes_to_close(now) < 0

    def minutes_since_open(self, now: datetime) -> float:
        return (self.local(now) - self.bounds(now)[0]) / timedelta(minutes=1)

    def minutes_to_close(self, now: datetime) -> float:
        return (self.bounds(now)[1] - self.local(now)) / timedelta(minutes=1)


SESSIONS: dict[Market, MarketSession] = {
    Market.KR: MarketSession(ZoneInfo("Asia/Seoul"), time(9, 0), time(15, 30), "XKRX"),
    Market.US: MarketSession(ZoneInfo("America/New_York"), time(9, 30), time(16, 0), "XNYS"),
}
