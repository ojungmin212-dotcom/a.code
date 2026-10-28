from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from harness.market import BarAggregator, Tick, compute_indicators
from harness.market.nh_feed import FeedFields, NHTickFeed, parse_push
from harness.models import Market
from harness.research.thesis import Thesis, ThesisStore

from .conftest import KST
from .helpers import make_bars


def tick(sec, price, vol=10):
    return Tick(Market.KR, "005930", datetime(2026, 10, 28, 10, 0, tzinfo=KST) + timedelta(seconds=sec),
                Decimal(price), vol)


def test_bar_aggregation():
    agg = BarAggregator()
    assert agg.add(tick(1, 100)) is None
    assert agg.add(tick(20, 105)) is None
    assert agg.add(tick(40, 98)) is None
    bar = agg.add(tick(61, 101))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 105, 98, 98, 30)
    assert agg.add(tick(30, 200)) is None  # 늦게 온 이전 분 틱은 버림
    assert agg.flush(Market.KR, "005930").close == 101


def test_indicators():
    bars = make_bars([100 + i for i in range(21)], spread=1, last_volume=3000)
    ind = compute_indicators(bars)
    assert ind["last"] == 120
    assert ind["ret_5m_pct"] == pytest.approx((120 / 115 - 1) * 100, abs=1e-3)
    assert ind["ma20"] == pytest.approx(110.5)
    assert ind["volume_ratio"] == 3.0
    assert ind["atr14"] == pytest.approx(2.0)  # 고가-전일종가 = 1 + 1


def test_parse_push():
    f = FeedFields(price="price", volume="cvolume")
    msg = {"header": {"tr_cd": "mc", "tr_key": "005930"}, "body": {"price": "+61,200", "cvolume": "15"}}
    t = parse_push(msg, f)
    assert t.price == Decimal(61200) and t.volume == 15 and t.symbol == "005930"
    assert parse_push({"header": {"tr_key": "005930"}, "body": {}}, f) is None
    assert parse_push({"header": {"tr_key": "005930"}, "body": {"price": "x", "cvolume": 1}}, f) is None


def test_feed_counts_dropped():
    feed = NHTickFeed(["005930"], FeedFields("p", "v"), subscribe=lambda *a, **k: 0)
    feed._on_message({"header": {"tr_key": "005930"}, "body": {"p": "100", "v": "1"}})
    feed._on_message({"header": {"tr_key": "005930"}, "body": {}})
    assert feed.ticks.qsize() == 1 and feed.dropped == 1


def test_thesis_store(tmp_path):
    y = tmp_path / "t.yaml"
    y.write_text("""
- {market: KR, symbol: "005930", direction: LONG, rationale: a,
   valid_from: "2026-10-28T08:00:00+09:00", valid_until: "2026-10-28T12:00:00+09:00"}
- {market: KR, symbol: "000660", direction: LONG, rationale: b,
   valid_from: "2026-10-29T08:00:00+09:00", valid_until: "2026-10-29T12:00:00+09:00"}
""", encoding="utf-8")
    s = ThesisStore.from_yaml(y)
    now = datetime(2026, 10, 28, 10, 0, tzinfo=KST)
    assert s.watchlist(now) == [(Market.KR, "005930")]
    assert s.active(Market.KR, "005930", now + timedelta(hours=2)) is None


def test_thesis_rejects_short_and_naive_time():
    base = dict(market="KR", symbol="1", rationale="r", valid_from="2026-10-28T08:00:00+09:00",
                valid_until="2026-10-28T12:00:00+09:00")
    with pytest.raises(ValidationError):
        Thesis(direction="SHORT", **base)
    with pytest.raises(ValidationError):
        Thesis(direction="LONG", **{**base, "valid_from": "2026-10-28T08:00:00"})
