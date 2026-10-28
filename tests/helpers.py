from datetime import datetime, timedelta
from decimal import Decimal

from harness.market.bars import Bar
from harness.models import Market
from harness.research.thesis import Thesis, ThesisStore

from .conftest import KST


def make_bars(closes, start=datetime(2026, 10, 28, 9, 0, tzinfo=KST), symbol="005930",
              spread=100, volume=1000, last_volume=None):
    bars = []
    for i, c in enumerate(closes):
        c = Decimal(c)
        v = last_volume if (last_volume is not None and i == len(closes) - 1) else volume
        bars.append(Bar(Market.KR, symbol, start + timedelta(minutes=i), c, c + spread, c - spread, c, v))
    return bars


def store(symbol="005930"):
    return ThesisStore([Thesis(market=Market.KR, symbol=symbol, direction="LONG", rationale="테스트",
                               valid_from=datetime(2026, 10, 28, 8, 0, tzinfo=KST),
                               valid_until=datetime(2026, 10, 28, 15, 30, tzinfo=KST))])
