from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from harness.models import AccountState, EntrySignal, Market, Position
from harness.risk import RiskLimits

from .conftest import KST, ROOT, kr_signal


def test_approves_and_sizes_by_per_trade_loss(gate, kr_open, cash):
    # 1주당 위험 1,000원 → 5,000원 / 1,000 = 5주. 금액 25만원 / 5만 = 5주. 둘 다 5주.
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert d.approved and d.quantity == 5


def test_sizing_capped_by_position_amount(gate, kr_open, cash):
    # 위험 100원/주 → 50주 가능하지만 종목당 25만원 한도로 5주
    sig = kr_signal(kr_open, stop_price=Decimal(49900))
    d = gate.evaluate(sig, cash, kr_open)
    assert d.quantity == 5 and "종목당 최대 금액" in d.reasons[0]


def test_sizing_capped_by_cash(gate, kr_open):
    d = gate.evaluate(kr_signal(kr_open), AccountState(cash_krw=Decimal(120000)), kr_open)
    assert d.quantity == 2 and "가용 현금" in d.reasons[0]


def test_rejects_when_one_share_exceeds_risk(gate, kr_open, cash):
    sig = kr_signal(kr_open, entry_price=Decimal(300000), stop_price=Decimal(290000),
                    take_profit_price=Decimal(320000))
    d = gate.evaluate(sig, cash, kr_open)
    assert not d.approved and "수량 0주" in d.reasons[0]


@pytest.mark.parametrize("hh,mm,reason", [
    (8, 59, "정규장"), (9, 5, "시작 직후"), (15, 15, "마감 직전"), (15, 30, "정규장"),
])
def test_time_filters(gate, cash, hh, mm, reason):
    now = datetime(2026, 10, 28, hh, mm, tzinfo=KST)
    d = gate.evaluate(kr_signal(now), cash, now)
    assert not d.approved and reason in d.reasons[0]


def test_weekend_rejected(gate, cash):
    sat = datetime(2026, 10, 31, 10, 30, tzinfo=KST)
    assert not gate.evaluate(kr_signal(sat), cash, sat).approved


def test_us_session_respects_dst(gate, cash):
    # 2026-11-01 미국 서머타임 종료: 개장은 10/30 22:30 KST, 11/2 23:30 KST
    us = lambda now: kr_signal(now, market=Market.US, symbol="AAPL", entry_price=Decimal(100),
                               stop_price=Decimal(99), take_profit_price=Decimal(103),
                               fx_rate_krw=Decimal(1400))
    before_dst_end = datetime(2026, 10, 30, 22, 50, tzinfo=KST)
    after_dst_end = datetime(2026, 11, 2, 22, 50, tzinfo=KST)
    assert gate.evaluate(us(before_dst_end), cash, before_dst_end).approved
    d = gate.evaluate(us(after_dst_end), cash, after_dst_end)
    assert not d.approved and "정규장" in d.reasons[0]


def test_low_confidence(gate, kr_open, cash):
    d = gate.evaluate(kr_signal(kr_open, confidence=0.6), cash, kr_open)
    assert not d.approved and "신뢰도" in d.reasons[0]


def test_edge_must_cover_costs(gate, kr_open, cash):
    # KR 왕복비용 0.25% × 3 = 0.75% 필요. 0.5% 기대수익은 거부
    d = gate.evaluate(kr_signal(kr_open, take_profit_price=Decimal(50250)), cash, kr_open)
    assert not d.approved and "기대수익" in d.reasons[0]


def test_daily_loss_includes_unrealized(gate, ledger, kr_open):
    day = kr_open.date()
    ledger.record_exit(day, Market.KR, Decimal(-10000))
    pos = Position(market=Market.KR, symbol="000660", quantity=1, avg_price=Decimal(100000),
                   last_price=Decimal(95000))
    acct = AccountState(cash_krw=Decimal(400000), positions=(pos,))
    d = gate.evaluate(kr_signal(kr_open), acct, kr_open)
    assert not d.approved and "일일 손실" in d.reasons[0]


def test_daily_loss_shared_across_markets(gate, ledger, cash, kr_open):
    ledger.record_exit(kr_open.date(), Market.US, Decimal(-15000))
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert not d.approved and "일일 손실" in d.reasons[0]


def test_consecutive_losses_reset_on_win(gate, ledger, cash, kr_open):
    day = kr_open.date()
    for pnl in (-100, -100, 50, -100, -100):
        ledger.record_exit(day, Market.KR, Decimal(pnl))
    assert gate.evaluate(kr_signal(kr_open), cash, kr_open).approved
    ledger.record_exit(day, Market.KR, Decimal(-100))
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert not d.approved and "연속 손절" in d.reasons[0]


def test_trade_count_limit(gate, ledger, cash, kr_open):
    for _ in range(6):
        ledger.record_entry(kr_open.date(), Market.KR)
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert not d.approved and "거래 횟수" in d.reasons[0]


def test_duplicate_and_max_positions(gate, kr_open):
    p = lambda s: Position(market=Market.KR, symbol=s, quantity=1, avg_price=Decimal(1000),
                           last_price=Decimal(1000))
    one = AccountState(cash_krw=Decimal(400000), positions=(p("005930"),))
    assert "이미 보유" in gate.evaluate(kr_signal(kr_open), one, kr_open).reasons[0]
    two = AccountState(cash_krw=Decimal(400000), positions=(p("A"), p("B")))
    assert "동시 보유" in gate.evaluate(kr_signal(kr_open), two, kr_open).reasons[0]


def test_total_loss_halts_until_manual_resume(gate, ledger, cash, kr_open):
    ledger.record_exit(kr_open.date() - timedelta(days=7), Market.KR, Decimal(-100000))
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert not d.approved and ledger.halted and "총 손실" in d.reasons[0]
    with pytest.raises(ValueError):
        ledger.resume("  ")
    ledger.resume("한도 재설정 후 수동 재개")
    # 누적 손실이 그대로면 다음 심사에서 다시 정지된다
    assert not gate.evaluate(kr_signal(kr_open), cash, kr_open).approved and ledger.halted


def test_trade_risk_shrinks_near_total_limit(gate, ledger, cash, kr_open):
    ledger.record_exit(kr_open.date() - timedelta(days=7), Market.KR, Decimal(-97000))
    d = gate.evaluate(kr_signal(kr_open), cash, kr_open)
    assert d.approved and d.quantity == 3  # 남은 여유 3,000원 / 1,000원


def test_should_flatten(gate):
    assert not gate.should_flatten(Market.KR, datetime(2026, 10, 28, 15, 14, tzinfo=KST))
    assert gate.should_flatten(Market.KR, datetime(2026, 10, 28, 15, 15, tzinfo=KST))
    assert gate.should_flatten(Market.KR, datetime(2026, 10, 28, 20, 0, tzinfo=KST))


def test_signal_validation(kr_open):
    with pytest.raises(ValidationError):
        kr_signal(kr_open, stop_price=Decimal(51000))
    with pytest.raises(ValidationError):
        kr_signal(datetime(2026, 10, 28, 10, 30), created_at=datetime(2026, 10, 28, 10, 30))


def test_limits_file_validation(tmp_path):
    raw = (ROOT / "config" / "risk.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(raw.replace("max_loss_per_trade_krw: 5000", "max_loss_per_trade_krw: 50000"),
                   encoding="utf-8")
    with pytest.raises(ValidationError):
        RiskLimits.from_yaml(bad)
    typo = tmp_path / "typo.yaml"
    typo.write_text(raw + "\nmax_dayly_loss: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        RiskLimits.from_yaml(typo)
