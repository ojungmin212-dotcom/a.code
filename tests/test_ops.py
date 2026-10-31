"""F2 · F3 · F6 · F7 · F11 · F13 테스트."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from harness.audit import AuditLog
from harness.benchmark import compare, kospi_return_pct
from harness.broker.nhplug_broker import NHPlugBroker
from harness.decision.policy import StrategyConfig
from harness.engine import TradingEngine, unreconciled_symbols
from harness.market.bars import Tick
from harness.market.notices import parse_fill_notice
from harness.market.recorder import TickRecorder
from harness.models import Market, OrderType
from harness.risk import SESSIONS, Ledger, RiskGate, RiskLimits
from harness.runner import Lane, summary

from .conftest import KST, ROOT
from .helpers import make_bars, store
from .test_engine import RISING, AlwaysBuy

MOCK = lambda: "https://moapi.nhplug.com:8443"
KEY = (Market.KR, "005930")
SCOPE = {KEY}
NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- F6 휴장일
def test_krx_holiday_and_late_open():
    kr = SESSIONS[Market.KR]
    hangul = datetime(2026, 10, 9, 10, 0, tzinfo=KST)        # 한글날
    assert not kr.is_trading_day(hangul) and not kr.is_open(hangul) and kr.done_for_day(hangul)
    first = datetime(2027, 1, 4, 9, 30, tzinfo=KST)          # 새해 첫 거래일 10시 개장
    assert not kr.is_open(first)
    assert kr.is_open(first + timedelta(minutes=45))


def test_nyse_early_close():
    us = SESSIONS[Market.US]
    after_black_friday_close = datetime(2026, 11, 27, 13, 30, tzinfo=NY)
    assert us.is_trading_day(after_black_friday_close) and not us.is_open(after_black_friday_close)
    gate = RiskGate(RiskLimits.from_yaml(ROOT / "config" / "risk.yaml"), Ledger())
    assert gate.should_flatten(Market.US, datetime(2026, 11, 27, 12, 50, tzinfo=NY))  # 조기폐장 10분 전


# ---------------------------------------------------------------- F2 체결통보
def notice(orderno="0000000123", qty="0000000002", prc="00000052000", rej="0"):
    return {"header": {"tr_cd": "d2"}, "body": {"orderno": orderno, "issuecd": "005930",
                                               "concgty": qty, "concprc": prc, "rejgb": rej}}


def test_parse_fill_notice():
    n = parse_fill_notice(notice())
    assert (n.order_no, n.qty, n.price, n.rejected) == ("0000000123", 2, Decimal(52000), False)
    assert parse_fill_notice(notice(qty="0")) is None          # 체결 아닌 통보
    assert parse_fill_notice(notice(rej="1")).rejected
    assert parse_fill_notice({"body": {}}) is None


class Calls:
    def __init__(self):
        self.sent = []

    def __call__(self, path, input_0):
        self.sent.append((path, input_0))
        return {"Output_0": {"mkt_orr_no": 123}, "message": {"usr_msg": "ok"}}


@pytest.fixture
def cfg():
    return StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")


def engine(cfg, entry=OrderType.MARKET):
    calls = Calls()
    b = NHPlugBroker("1", dry_run=False, order_no_field=["mkt_orr_no"], call=calls, base_url=MOCK)
    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    e = TradingEngine(gate=RiskGate(limits, Ledger()), broker=b, policy=AlwaysBuy(cfg), theses=store(),
                      min_bars=cfg.min_bars, audit=AuditLog(), entry_order_type=entry)
    for bar in make_bars(RISING[:20]):
        e.on_bar(bar, bar.start + timedelta(minutes=1))
    return e, calls


T = datetime(2026, 10, 28, 9, 20, tzinfo=KST)


def test_fill_notice_books_exact_entry_and_exit(cfg):
    e, _ = engine(cfg)
    assert e.pending[KEY].result.broker_order_id == "123"
    e.on_fill_notice("0000000123", 2, Decimal(51950), False, T)   # 부분 체결
    assert KEY in e.pending
    e.on_fill_notice("123", 2, Decimal(52050), False, T)
    assert KEY not in e.pending and e.positions[KEY].avg_price == Decimal(52000)  # VWAP
    e.flatten_if_due(datetime(2026, 10, 28, 15, 20, tzinfo=KST))
    e.on_fill_notice("123", 4, Decimal(52500), False, T)
    ex = e.audit.events("exit")[0]["payload"]
    assert ex["estimated_price"] is False and Decimal(ex["price"]) == 52500
    assert unreconciled_symbols(e.audit) == []


def test_fill_notice_rejection_clears_pending(cfg):
    e, _ = engine(cfg)
    e.on_fill_notice("123", 0, Decimal(0), True, T)
    assert KEY not in e.pending and not e.positions
    e.on_fill_notice("999", 1, Decimal(1), False, T)   # 모르는 주문번호는 기록만
    assert e.audit.events("fill_notice")[-1]["payload"]["matched"] is False


# ---------------------------------------------------------------- F3 지정가 + 취소
def test_limit_entry_cancelled_after_timeout(cfg):
    e, calls = engine(cfg, entry=OrderType.LIMIT)
    path, i0 = calls.sent[0]
    assert path.endswith("cashBuy") and i0["nmn_pr_tp_cd"] == "01" and i0["orr_pr"] == 51900
    sent_at = e.pending[KEY].sent_at
    e.reconcile({}, sent_at + timedelta(seconds=61), SCOPE)
    assert calls.sent[-1][0] == "/krstock/order/v1/cancel"
    assert calls.sent[-1][1] == {"act_no": "1", "org_mkt_orr_no": 123, "all_pat_dit_cd": "1", "iem_cd": "005930"}
    assert KEY in e.pending                                       # 취소 확인 대기
    e.reconcile({KEY: (1, Decimal(51900))}, sent_at + timedelta(seconds=95), SCOPE)
    assert KEY not in e.pending and e.positions[KEY].quantity == 1  # 부분 체결분만 보유


def test_limit_unfilled_resolves_after_cancel(cfg):
    e, _ = engine(cfg, entry=OrderType.LIMIT)
    t0 = e.pending[KEY].sent_at
    e.reconcile({}, t0 + timedelta(seconds=61), SCOPE)
    e.reconcile({}, t0 + timedelta(seconds=95), SCOPE)
    assert KEY not in e.pending and not e.positions and unreconciled_symbols(e.audit) == []


# ---------------------------------------------------------------- F7 벤치마크
def test_kospi_return_and_compare():
    bad = ("bad", lambda s, e: (_ for _ in ()).throw(RuntimeError("x")))
    good = ("fake", lambda s, e: [100.0, 101.0, 102.0])
    assert kospi_return_pct(None, None, [bad, good]) == (pytest.approx(2.0), "fake")
    assert kospi_return_pct(None, None, [bad]) == (None, None)
    a = AuditLog()
    a.record("exit", {"pnl_krw": Decimal(15000)}, market="KR", symbol="X",
             ts=datetime(2026, 10, 28, 10, 0, tzinfo=KST))
    b = compare(a, Decimal(500000), loaders=[good])
    assert b.strategy_pct == pytest.approx(3.0) and b.excess_pct == pytest.approx(1.0)


def test_promotion_index_row(monkeypatch):
    from harness import benchmark
    from harness.promotion import PromotionCriteria, evaluate
    monkeypatch.setattr(benchmark, "kospi_return_pct", lambda s, e, l=None: (50.0, "fake"))
    c = PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml")
    a = AuditLog()
    a.record("exit", {"pnl_krw": Decimal(1000), "qty": 1}, market="KR", symbol="X")
    a.record("fill", {"side": "BUY", "qty": 1, "price": 1, "signal_price": 1}, market="KR", symbol="X")
    rows = {n: ok for n, ok, _ in evaluate(a, AuditLog(), c, datetime.now(timezone.utc), True).checks}
    assert rows["KOSPI 대비 초과수익"] is False


# ---------------------------------------------------------------- F11 · F13
def test_tick_recorder(tmp_path):
    r = TickRecorder(tmp_path)
    t = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)
    r.write(Tick(Market.KR, "005930", t, Decimal(52000), 3))
    r.write(Tick(Market.KR, "005930", t, Decimal(52100), 1))
    r.close()
    lines = (tmp_path / "20261028-KR.csv").read_text().splitlines()
    assert lines[0] == "ts,market,symbol,price,volume" and len(lines) == 3


def test_summary_text(cfg):
    e, _ = engine(cfg)
    s = summary([Lane(e, e.broker)], T, "📊 요약")
    assert "📊 요약" in s and "always" in s and "대기 1" in s
