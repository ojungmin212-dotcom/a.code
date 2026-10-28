"""틱 → 1분봉 집계. 시각은 수신 시각 기준(거래소 체결시각 필드명이 확인되기 전까지)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from harness.models import Market


@dataclass(frozen=True)
class Tick:
    market: Market
    symbol: str
    ts: datetime
    price: Decimal
    volume: int  # 이 체결의 수량(누적 아님)


@dataclass(frozen=True)
class Bar:
    market: Market
    symbol: str
    start: datetime  # 분 시작 시각
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class BarAggregator:
    """종목별 1분봉. 새 분의 첫 틱이 오면 직전 분봉을 마감해 돌려준다.

    거래가 없는 분은 봉을 만들지 않는다(빈 봉을 채우지 않음).
    """

    def __init__(self) -> None:
        self._open: dict[tuple[Market, str], Bar] = {}

    def add(self, t: Tick) -> Bar | None:
        if t.ts.tzinfo is None:
            raise ValueError("Tick.ts 는 timezone-aware 여야 합니다")
        key = (t.market, t.symbol)
        minute = t.ts.replace(second=0, microsecond=0)
        cur = self._open.get(key)
        if cur is not None and minute < cur.start:
            return None  # 늦게 도착한 이전 분 틱은 버린다
        if cur is None or minute > cur.start:
            self._open[key] = Bar(t.market, t.symbol, minute, t.price, t.price, t.price, t.price, t.volume)
            return cur
        self._open[key] = Bar(t.market, t.symbol, cur.start, cur.open, max(cur.high, t.price),
                              min(cur.low, t.price), t.price, cur.volume + t.volume)
        return None

    def flush(self, market: Market, symbol: str) -> Bar | None:
        """장 마감 등으로 다음 틱이 오지 않을 때 진행 중인 봉을 강제 마감."""
        return self._open.pop((market, symbol), None)
