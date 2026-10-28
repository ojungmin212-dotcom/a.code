from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from harness.audit import AuditLog
from harness.broker import DuplicateOrderError, PaperBroker
from harness.broker.nhplug_broker import NHPlugBroker
from harness.decision.policy import Decision, StrategyConfig, build_signal
from harness.engine import TradingEngine, restore_ledger, unreconciled_symbols
from harness.market.bars import Bar
from harness.models import Market, OrderStatus
from harness.replay import read_bars_csv, run_replay
from harness.risk import Ledger, RiskGate

from .conftest import KST, ROOT
from .helpers import make_bars, store


class AlwaysBuy:
    name = "always"

    def __init__(self, cfg):
        self.cfg, self.calls = cfg, 0

    def decide(self, ctx):
        self.calls += 1
        return Decision(build_signal(ctx, self.cfg, 0.9), "test")


@pytest.fixture
def cfg():
    return StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")


def setup(limits, cfg, broker=None, audit=None):
    audit = audit or AuditLog()
    broker = broker or PaperBroker(limits.initial_capital_krw, guard=audit.guard())
    gate = RiskGate(limits, Ledger())
    policy = AlwaysBuy(cfg)
    eng = TradingEngine(gate=gate, broker=broker, policy=policy, theses=store(),
                        min_bars=cfg.min_bars, audit=audit)
    return eng, broker, gate, policy, audit


def feed(eng, broker, bars):
    for b in bars:
        if isinstance(broker, PaperBroker):
            broker.set_price(b.market, b.symbol, b.close)
        eng.on_bar(b, b.start + timedelta(minutes=1))


RISING = [50000 + 100 * i for i in range(25)]
KEY = (Market.KR, "005930")


def test_waits_for_min_bars_and_thesis(limits, cfg):
    eng, broker, *_ , policy, _ = setup(limits, cfg)
    feed(eng, broker, make_bars(RISING[:19]))
    assert policy.calls == 0
    feed(eng, broker, make_bars([50000] * 25, symbol="000660"))  # 논지 없는 종목
    assert policy.calls == 0


def test_enter_then_take_profit(limits, cfg):
    eng, broker, gate, _, audit = setup(limits, cfg)
    bars = make_bars(RISING[:20])
    feed(eng, broker, bars)
    pos = eng.positions[KEY]
    assert pos.quantity == 4  # 1주 위험 400원 → 12주 가능하나 종목당 25만원 한도(51,900원)로 4주
    tp = pos.take_profit
    last = bars[-1].start
    up = Bar(Market.KR, "005930", last + timedelta(minutes=1), tp, tp + 100, tp, tp, 1000)
    feed(eng, broker, [up])
    assert KEY not in eng.positions
    pnl = audit.events("exit")[0]["payload"]["pnl_krw"]
    assert Decimal(pnl) > 0 and gate.ledger.cumulative_realized_krw == Decimal(pnl)


def test_stop_loss_and_cash_accounting(limits, cfg):
    eng, broker, gate, *_ = setup(limits, cfg)
    bars = make_bars(RISING[:20])
    feed(eng, broker, bars)
    stop = eng.positions[KEY].stop
    down = Bar(Market.KR, "005930", bars[-1].start + timedelta(minutes=1),
               stop, stop, stop - 100, stop - 100, 1000)
    feed(eng, broker, [down])
    assert KEY not in eng.positions
    realized = gate.ledger.cumulative_realized_krw
    assert realized < 0
    assert eng.cash_krw == limits.initial_capital_krw + realized


def test_flatten_before_close(limits, cfg):
    eng, broker, *_ = setup(limits, cfg)
    feed(eng, broker, make_bars(RISING[:20], start=datetime(2026, 10, 28, 14, 30, tzinfo=KST)))
    assert KEY in eng.positions
    eng.flatten_if_due(datetime(2026, 10, 28, 15, 16, tzinfo=KST))
    assert KEY not in eng.positions


def test_unknown_result_blocks_symbol(limits, cfg):
    class Fake:
        calls = 0

        def __call__(self, path, input_0):
            self.calls += 1
            return {"rsp_msg": "처리", "Output_0": {}}

    fake = Fake()
    nh = NHPlugBroker("123", dry_run=False, call=fake, base_url=lambda: "https://moapi.nhplug.com:8443")
    eng, _, gate, policy, audit = setup(limits, cfg, broker=nh)
    feed(eng, nh, make_bars(RISING))
    assert fake.calls == 1 and KEY in eng.pending and KEY not in eng.positions
    assert policy.calls == 1  # 대조 전에는 다시 판단하지 않음
    assert unreconciled_symbols(audit) == ["KR:005930"]
    eng.resolve_pending(Market.KR, "005930", "HTS 확인")
    assert unreconciled_symbols(audit) == []


def test_duplicate_client_id_survives_restart(limits, cfg, tmp_path):
    db = tmp_path / "a.sqlite"
    a1 = AuditLog(db)
    eng, broker, *_ = setup(limits, cfg, audit=a1)
    bars = make_bars(RISING[:20])
    feed(eng, broker, bars)
    # 재시작 후 같은 봉을 다시 처리해도 같은 client_order_id 라 거부된다
    a2 = AuditLog(db)
    eng2, broker2, *_ = setup(limits, cfg, audit=a2)
    feed(eng2, broker2, bars)
    orders = a2.events("order")
    assert orders[-1]["payload"]["result"]["status"] == OrderStatus.REJECTED.value
    assert "이미 제출된" in orders[-1]["payload"]["result"]["message"]
    with pytest.raises(DuplicateOrderError):
        a2.guard().claim(orders[0]["payload"]["request"]["client_order_id"])


def test_restore_ledger_and_open_position_detection(limits, cfg, tmp_path):
    db = tmp_path / "a.sqlite"
    eng, broker, gate, *_ = setup(limits, cfg, audit=AuditLog(db))
    bars = make_bars(RISING[:20])
    feed(eng, broker, bars)
    assert unreconciled_symbols(AuditLog(db)) == ["KR:005930"]  # 보유 중 재시작 → 시작 거부 대상
    stop = eng.positions[KEY].stop
    feed(eng, broker, [Bar(Market.KR, "005930", bars[-1].start + timedelta(minutes=1),
                           stop, stop, stop, stop, 1)])
    assert unreconciled_symbols(AuditLog(db)) == []

    gate2 = RiskGate(limits, Ledger())
    restore_ledger(AuditLog(db), gate2)
    day = bars[0].start.date()
    assert gate2.ledger.cumulative_realized_krw == gate.ledger.cumulative_realized_krw
    assert gate2.ledger.trades(day, Market.KR) == 1
    assert gate2.ledger.consecutive_losses(day, Market.KR) == 1


def test_halt_flattens_everything(limits, cfg):
    eng, broker, gate, *_ = setup(limits, cfg)
    bars = make_bars(RISING[:20])
    feed(eng, broker, bars)
    gate.ledger.record_exit(bars[0].start.date() - timedelta(days=3), Market.KR, Decimal(-101000))
    feed(eng, broker, make_bars([52000], start=bars[-1].start + timedelta(minutes=1)))
    assert gate.ledger.halted and not eng.positions


def test_replay_end_to_end(limits, cfg, tmp_path):
    csv = tmp_path / "bars.csv"
    rows = ["start,market,symbol,open,high,low,close,volume"]
    for b in make_bars(RISING + [RISING[-1] - 2000] * 3):
        rows.append(f"{b.start.isoformat()},KR,{b.symbol},{b.open},{b.high},{b.low},{b.close},{b.volume}")
    csv.write_text("\n".join(rows), encoding="utf-8")
    r = run_replay(read_bars_csv(csv), AlwaysBuy(cfg), store(), limits, cfg.min_bars)
    assert r.trades >= 1 and len(r.audit.events("decision")) >= 1
