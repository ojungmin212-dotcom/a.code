"""G9: 성질 기반 테스트 — 어떤 입력에도 돈을 지키는 규칙이 깨지지 않는지 무작위 수천 건으로 확인."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from harness.audit import AuditLog
from harness.broker import PaperBroker
from harness.decision.policy import StrategyConfig
from harness.engine import TradingEngine
from harness.market.bars import Bar
from harness.models import AccountState, EntrySignal, Market, OrderType, Position
from harness.risk import Ledger, RiskGate, RiskLimits

from .conftest import KST, ROOT
from .helpers import store
from .test_engine import AlwaysBuy

LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
CFG = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
DAY = datetime(2026, 10, 28, tzinfo=KST)  # 수요일 거래일

prices = st.integers(min_value=1_000, max_value=900_000)
# 경계 근처를 많이 뽑는다: 대부분 진입 가능 시간대(09:10~15:15), 일부는 하루 전체
_inside = st.integers(9 * 60 + 5, 15 * 60 + 20)
minutes = st.one_of(_inside, _inside, _inside, _inside, st.integers(0, 24 * 60 - 1))


@st.composite
def signals(draw):
    entry = draw(prices)
    risk_bp = draw(st.one_of(st.integers(5, 300), st.integers(1, 5000)))          # 손절폭(bp)
    stop = max(1, min(entry - 1, entry - entry * risk_bp // 10_000))
    edge_bp = draw(st.one_of(st.integers(60, 400), st.integers(1, 10_000)))       # 기대수익(bp)
    tp = entry + max(1, entry * edge_bp // 10_000)
    now = DAY + timedelta(minutes=draw(minutes))
    conf = draw(st.one_of(st.floats(0.6, 1.0), st.floats(0, 1)))
    return EntrySignal(market=Market.KR, symbol="005930", entry_price=Decimal(entry), stop_price=Decimal(stop),
                       take_profit_price=Decimal(tp), confidence=conf, created_at=now), now


@st.composite
def accounts(draw):
    n = draw(st.sampled_from([0, 0, 0, 1, 1, 2, 3]))
    def one():
        avg = draw(prices)
        move_bp = draw(st.one_of(st.integers(-300, 300), st.integers(-5000, 5000)))
        last = max(1, avg + avg * move_bp // 10_000)
        return Position(market=Market.KR, symbol=draw(st.sampled_from(["005930", "000660", "035420", "A"])),
                        quantity=draw(st.integers(1, 5)), avg_price=Decimal(avg), last_price=Decimal(last))
    pos = tuple(one() for _ in range(n))
    cash = draw(st.one_of(st.integers(100_000, 600_000), st.integers(0, 2_000_000)))
    return AccountState(cash_krw=Decimal(cash), positions=pos)


@st.composite
def ledgers(draw):
    led = Ledger()
    for _ in range(draw(st.sampled_from([0, 0, 0, 1, 2, 5]))):
        day = DAY.date() - timedelta(days=draw(st.integers(0, 3)))
        led.record_exit(day, Market.KR, Decimal(draw(st.one_of(st.integers(-6_000, 6_000), st.integers(-60_000, 30_000)))))
    for _ in range(draw(st.sampled_from([0, 0, 0, 1, 3, 8]))):
        led.record_entry(DAY.date(), Market.KR)
    if draw(st.integers(0, 9)) == 0:
        led.halt("test")
    return led


@settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(signals(), accounts(), ledgers())
def test_gate_never_breaks_money_rules(sig_now, acct, led):
    sig, now = sig_now
    gate = RiskGate(LIMITS, led)
    was_halted = led.halted
    d = gate.evaluate(sig, acct, now)
    if not d.approved:
        assert d.quantity == 0
        return
    qty = Decimal(d.quantity)
    total_now = led.cumulative_realized_krw + acct.unrealized_pnl_krw
    # 1회 손실 · 남은 총손실 여유 · 종목당 금액 · 현금 — 어느 것도 넘지 않는다
    assert qty >= 1
    assert qty * (sig.entry_price - sig.stop_price) <= LIMITS.max_loss_per_trade_krw
    assert qty * (sig.entry_price - sig.stop_price) <= LIMITS.max_total_loss_krw + total_now
    assert qty * sig.entry_price <= LIMITS.max_position_krw
    assert qty * sig.entry_price <= acct.cash_krw
    # 정지·시간·한도 조건에서는 승인되지 않는다
    assert not was_halted and not led.halted
    assert total_now > -LIMITS.max_total_loss_krw
    s = gate.sessions[Market.KR]
    assert s.is_open(now)
    assert s.minutes_since_open(now) >= LIMITS.no_entry_after_open_min
    assert s.minutes_to_close(now) > LIMITS.no_entry_before_close_min
    assert led.daily_realized(now.date()) + acct.unrealized_pnl_krw > -LIMITS.max_daily_loss_krw
    assert led.trades(now.date(), Market.KR) < LIMITS.max_trades_per_day[Market.KR]
    assert sig.confidence >= LIMITS.min_confidence
    assert not acct.holds(Market.KR, sig.symbol) and len(acct.positions) < LIMITS.max_open_positions
    edge = (sig.take_profit_price - sig.entry_price) / sig.entry_price * 100
    assert float(edge) >= LIMITS.round_trip_cost_pct[Market.KR] * LIMITS.min_edge_cost_multiple


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(signals(), accounts(), st.integers(-99_000, 0))
def test_sizing_invariants_when_conditions_pass(sig_now, acct, past_loss):
    """시간·신뢰도·정지 조건을 통과시키고 수량 계산 경계만 집중 검사(승인 사례를 많이 만든다)."""
    sig, _ = sig_now
    sig = sig.model_copy(update={"confidence": 0.9})
    now = DAY + timedelta(hours=11)
    led = Ledger()
    led.record_exit(DAY.date() - timedelta(days=5), Market.KR, Decimal(past_loss))
    acct = AccountState(cash_krw=acct.cash_krw, positions=acct.positions[:1])
    d = RiskGate(LIMITS, led).evaluate(sig, acct, now)
    if d.approved:
        qty = Decimal(d.quantity)
        room = LIMITS.max_total_loss_krw + led.cumulative_realized_krw + acct.unrealized_pnl_krw
        assert qty * (sig.entry_price - sig.stop_price) <= min(LIMITS.max_loss_per_trade_krw, room)
        assert qty * sig.entry_price <= min(LIMITS.max_position_krw, acct.cash_krw)
        # 최대성: 1주 더 사면 어느 한도든 넘는다(보수적으로 너무 적게 사지도 않음)
        q1 = qty + 1
        assert (q1 * (sig.entry_price - sig.stop_price) > min(LIMITS.max_loss_per_trade_krw, room)
                or q1 * sig.entry_price > min(LIMITS.max_position_krw, acct.cash_krw))


# ---------------------------------------------------------------- 엔진 전체: 무작위 가격 경로
@st.composite
def price_paths(draw):
    start = draw(st.integers(20_000, 200_000))
    steps = draw(st.lists(st.integers(-300, 300), min_size=30, max_size=390))
    gaps = draw(st.lists(st.integers(-3000, 3000), min_size=0, max_size=3))  # 급락·급등 갭
    path, p = [], start
    for i, s in enumerate(steps):
        p = max(1_000, p + s * 10 + (gaps[i % len(gaps)] * 10 if gaps and i % 97 == 50 else 0))
        path.append(p)
    return path


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(price_paths(), st.integers(0, 300))
def test_engine_invariants_on_random_days(path, start_offset):
    gate = RiskGate(LIMITS, Ledger())
    broker = PaperBroker(LIMITS.initial_capital_krw, slippage_bps=Decimal(10))
    eng = TradingEngine(gate=gate, broker=broker, policy=AlwaysBuy(CFG), theses=store(), min_bars=CFG.min_bars,
                        audit=AuditLog(), entry_order_type=OrderType.MARKET)
    t0 = datetime.combine(DAY.date(), time(9, 0), KST) + timedelta(minutes=start_offset)
    for i, c in enumerate(path):
        start = t0 + timedelta(minutes=i)
        hi = Decimal(c + 50)
        lo = Decimal(max(1, c - 50))
        bar = Bar(Market.KR, "005930", start, Decimal(c), hi, lo, Decimal(c), 1000)
        broker.set_price(Market.KR, "005930", bar.close)
        now = start + timedelta(minutes=1)
        eng.on_bar(bar, now)
        eng.flatten_if_due(now)
        # 불변식: 보유 수 한도, 현금 음수 없음, 청산 구간에는 보유 없음, 정지 후 신규 보유 없음
        assert len(eng.positions) <= LIMITS.max_open_positions
        assert broker.cash_krw >= 0 and eng.cash_krw >= 0
        if gate.should_flatten(Market.KR, now):
            assert not eng.positions
        assert gate.ledger.trades(DAY.date(), Market.KR) <= LIMITS.max_trades_per_day[Market.KR]
    # 진입 시점 기준 손절폭 손실은 1회 한도 이내(갭·슬리피지로 실제 손실은 넘을 수 있음 → 아래는 진입 시점 검사)
    for e in eng.audit.events("order"):
        sig = e["payload"].get("signal")
        if sig:
            qty = e["payload"]["request"]["quantity"]
            risk = (Decimal(sig["entry_price"]) - Decimal(sig["stop_price"])) * qty
            assert risk <= LIMITS.max_loss_per_trade_krw
