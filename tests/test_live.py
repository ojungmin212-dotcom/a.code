from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from harness.audit import AuditLog
from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker, NotConfigured
from harness.decision.policy import StrategyConfig
from harness.engine import TradingEngine, restore_ledger, unreconciled_symbols
from harness.models import Market, OrderType
from harness.notify import make_notifier
from harness.promotion import PromotionCriteria, evaluate
from harness.risk import Ledger, RiskGate, RiskLimits

from .conftest import KST, ROOT
from .helpers import make_bars, store
from .test_engine import RISING, AlwaysBuy

MOCK = lambda: "https://moapi.nhplug.com:8443"
KEY = (Market.KR, "005930")
SCOPE = {KEY}


class Calls:
    def __init__(self, order_resp=None, balance=None):
        self.order_resp = order_resp or {"rsp_msg": "주문접수", "Output_0": {}}
        self.balance, self.paths = balance, []

    def __call__(self, path, input_0):
        self.paths.append(path)
        return self.balance if "balance" in path else self.order_resp


@pytest.fixture
def cfg():
    return StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")


def live_engine(cfg, limits=None, audit=None, msgs=None):
    limits = limits or RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    calls = Calls()
    broker = NHPlugBroker("123", dry_run=False, call=calls, base_url=MOCK)
    audit = audit or AuditLog()
    msgs = msgs if msgs is not None else []
    eng = TradingEngine(gate=RiskGate(limits, Ledger()), broker=broker, policy=AlwaysBuy(cfg),
                        theses=store(), min_bars=cfg.min_bars, audit=audit,
                        entry_order_type=OrderType.MARKET, notify=msgs.append)
    return eng, calls, audit, msgs


def feed(eng, bars):
    for b in bars:
        eng.on_bar(b, b.start + timedelta(minutes=1))


BARS = make_bars(RISING[:20])
T = BARS[-1].start + timedelta(minutes=1)


def test_market_entry_payload_and_pending(cfg):
    eng, calls, _, _ = live_engine(cfg)
    feed(eng, BARS)
    assert eng.pending[KEY].qty_ordered == 4 and eng.pending[KEY].signal is not None
    assert calls.paths == ["/krstock/order/v1/cashBuy"]


def test_reconcile_confirms_buy_with_broker_avg_price(cfg):
    eng, _, audit, msgs = live_engine(cfg)
    feed(eng, BARS)
    eng.reconcile({KEY: (4, Decimal(52000))}, T + timedelta(seconds=3), SCOPE)
    p = eng.positions[KEY]
    assert KEY not in eng.pending and p.quantity == 4 and p.avg_price == 52000
    assert p.stop == Decimal(51500) + 100  # 신호가 51,900 → 체결 52,000: 손절도 100원 이동
    assert unreconciled_symbols(audit) == ["KR:005930"]  # 보유 중
    assert any("진입" in m for m in msgs)


def test_reconcile_buy_timeout_without_fill(cfg):
    eng, _, audit, _ = live_engine(cfg)
    feed(eng, BARS)
    eng.reconcile({}, T + timedelta(seconds=10), SCOPE)
    assert KEY in eng.pending  # 아직 기다림
    eng.reconcile({}, T + timedelta(seconds=61), SCOPE)
    assert KEY not in eng.pending and KEY not in eng.positions
    assert unreconciled_symbols(audit) == []


def test_reconcile_sell_and_estimated_pnl(cfg):
    eng, _, audit, _ = live_engine(cfg)
    feed(eng, BARS)
    eng.reconcile({KEY: (4, Decimal(51900))}, T, SCOPE)
    stop = eng.positions[KEY].stop
    feed(eng, make_bars([int(stop) - 100], start=BARS[-1].start + timedelta(minutes=1), spread=0))
    assert eng.pending[KEY].reason == "stop"
    eng.reconcile({}, T + timedelta(minutes=1, seconds=5), SCOPE)
    assert KEY not in eng.positions
    ex = audit.events("exit")[0]["payload"]
    assert ex["estimated_price"] is True and Decimal(ex["pnl_krw"]) < 0
    assert unreconciled_symbols(audit) == []


def test_sell_timeout_reissues_next_minute(cfg):
    eng, calls, _, msgs = live_engine(cfg)
    feed(eng, BARS)
    eng.reconcile({KEY: (4, Decimal(51900))}, T, SCOPE)
    stop = eng.positions[KEY].stop
    t1 = BARS[-1].start + timedelta(minutes=1)
    feed(eng, make_bars([int(stop) - 100], start=t1, spread=0))
    eng.reconcile({KEY: (4, Decimal(51900))}, T + timedelta(minutes=3), SCOPE)
    assert KEY not in eng.pending and any("재주문" in m for m in msgs)
    feed(eng, make_bars([int(stop) - 200], start=t1 + timedelta(minutes=2), spread=0))
    assert calls.paths.count("/krstock/order/v1/cashSell") == 2


def test_mismatch_halts_but_ignores_out_of_scope(cfg, tmp_path):
    db = tmp_path / "a.sqlite"
    eng, _, audit, msgs = live_engine(cfg, audit=AuditLog(db))
    eng.reconcile({(Market.KR, "000660"): (10, Decimal(1))}, T, SCOPE)  # 계좌의 다른 종목
    assert not eng.gate.ledger.halted
    eng.reconcile({KEY: (3, Decimal(1))}, T, SCOPE)  # 하네스가 모르는 보유
    assert eng.gate.ledger.halted and any("불일치" in m for m in msgs)
    # 정지는 재시작해도 유지, resume 기록 후 해제
    g = RiskGate(eng.gate.limits, Ledger())
    restore_ledger(AuditLog(db), g)
    assert g.ledger.halted
    AuditLog(db).record("resume", {"note": "확인 완료"})
    g2 = RiskGate(eng.gate.limits, Ledger())
    restore_ledger(AuditLog(db), g2)
    assert not g2.ledger.halted


def test_exit_attempts_deduped_within_minute(cfg):
    eng, calls, _, _ = live_engine(cfg)
    feed(eng, BARS)
    eng.reconcile({KEY: (4, Decimal(51900))}, T, SCOPE)
    close = datetime(2026, 10, 28, 15, 20, tzinfo=KST)
    for s in range(5):  # 루프가 1초마다 돌아도 같은 분에는 한 번만 매도 시도
        eng.pending.clear()
        eng.flatten_if_due(close + timedelta(seconds=s))
    assert calls.paths.count("/krstock/order/v1/cashSell") == 1


def test_holdings_parsing():
    bal = {"rsp_msg": "조회완료", "Output_1": [
        {"iem_cd": "005930", "bal_qty": "4", "pchs_avg_pr": "52,000"},
        {"iem_cd": "000660", "bal_qty": "0", "pchs_avg_pr": "0"}]}
    f = BalanceFields("Output_1", "iem_cd", "bal_qty", "pchs_avg_pr")
    b = NHPlugBroker("1", call=Calls(balance=bal), base_url=MOCK, balance_fields=f)
    assert b.holdings() == {KEY: (4, Decimal(52000))}
    with pytest.raises(RuntimeError):
        NHPlugBroker("1", call=Calls(balance={"rsp_msg": "오류"}), base_url=MOCK,
                     balance_fields=f).holdings()
    with pytest.raises(NotConfigured):
        NHPlugBroker("1", call=Calls(), base_url=MOCK).holdings()


def test_notifier_survives_telegram_failure(monkeypatch, capsys):
    import requests
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []

    def post(url, **kw):
        sent.append(kw["json"]["text"])
        raise requests.ConnectionError("down")

    make_notifier("live", post=post)("체결")
    assert "체결" in sent[0] and "체결" in capsys.readouterr().out


def test_live_limits_file():
    lim = RiskLimits.from_yaml(ROOT / "config" / "risk.live.yaml")
    assert lim.initial_capital_krw == 200000 and lim.max_trades_per_day[Market.US] == 0


# ---------------------------------------------------------------- 승격 심사
def fill_log(pnls, start, with_cal=0, rise=True):
    a = AuditLog()
    for i, p in enumerate(pnls):
        a.record("fill", {"side": "BUY", "qty": 1, "price": 1, "signal_price": 1},
                 market="KR", symbol="X", ts=start + timedelta(hours=i))
        a.record("exit", {"pnl_krw": Decimal(p), "qty": 1}, market="KR", symbol="X",
                 ts=start + timedelta(hours=i, minutes=30))
    t0 = start
    for i in range(with_cal + 20):
        a.record("bar", {"start": t0 + timedelta(minutes=i), "c": 100 + i if rise else 100 - i},
                 market="KR", symbol="X")
    for i in range(with_cal):
        p = 0.9 if i % 2 else 0.6
        a.record("decision", {"detail": {"answers": {"thesis_aligned": {"noul": p}},
                                         "state": {"indicators": {"last": 100 + i if i % 2 else 10_000}}}},
                 market="KR", symbol="X", ts=t0 + timedelta(minutes=i + 1))
    return a


def test_promotion_pass_and_fail():
    c = PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml")
    start = datetime(2026, 9, 1, 10, 0, tzinfo=KST)
    now = start + timedelta(weeks=7)
    good = fill_log([100] * 100, start, with_cal=200)
    base = fill_log([10] * 50, start)
    r = evaluate(good, base, c, now, fields_ready=True)
    assert r.passed, r.render()
    assert not evaluate(good, base, c, now, fields_ready=False).passed
    assert not evaluate(good, base, c, start + timedelta(weeks=5), True).passed
    assert not evaluate(fill_log([100] * 99, start, 200), base, c, now, True).passed
    assert not evaluate(good, fill_log([1000] * 100, start), c, now, True).passed  # 비교군이 더 좋음
    names = [n for n, ok, _ in evaluate(fill_log([100] * 100, start, 0), base, c, now, True).checks if not ok]
    assert names == ["Jev 확률 보정"]
