"""F1 · F4 · F9 · D1 반영 테스트."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from harness.audit import AuditLog
from harness.broker import PaperBroker
from harness.broker.nhplug_broker import NHPlugBroker, server_message
from harness.decision import JevClient, JevPolicy, StrategyConfig
from harness.decision.policy import ShadowSettings
from harness.decision.shadow import ShadowObserver
from harness.engine import TradingEngine
from harness.evaluation import jev_calibration
from harness.models import Market, OrderRequest, OrderStatus, OrderType, Side
from harness.risk import Ledger, RiskGate, RiskLimits
from harness.runner import Lane, LoopConfig, run_loop

from .conftest import KST, ROOT
from .helpers import make_bars, store
from .test_decision import FakeResp, answers

MOCK = lambda: "https://moapi.nhplug.com:8443"


# ---------------------------------------------------------------- F1
def test_spec_defaults_loaded():
    from harness.bootstrap import broker_fields, feed_fields, fields_ready
    order_no, bf = broker_fields()
    assert order_no == ["mkt_orr_no", "orr_no"]
    assert (bf.holdings_list, bf.symbol_field) == ("Output_1", "iem_cd")
    f = feed_fields(Market.KR)
    assert (f.price, f.volume) == ("price", "movolume")  # volume(누적)이 아님
    assert feed_fields(Market.US).price == "trdprc_1z17"
    assert not fields_ready()  # 모의 실측(verified_by_mock) 전에는 승격·실거래 잠금


def test_server_message_both_envelopes():
    assert server_message({"rsp_msg": "정상"}) == "정상"
    assert server_message({"message": {"usr_msg": "주문완료", "msg_code": "00000"}}) == "주문완료 | 00000"
    assert server_message({}) is None


@pytest.mark.parametrize("out,status,no", [
    ({"mkt_orr_no": 12345}, OrderStatus.ACCEPTED, "12345"),     # 국내 명세
    ({"orr_no": 777}, OrderStatus.ACCEPTED, "777"),             # 미국 명세
    ({"mkt_orr_no": 0}, OrderStatus.UNKNOWN, None),             # 0 은 주문번호 아님
    ({}, OrderStatus.UNKNOWN, None),
])
def test_order_no_fields(out, status, no):
    b = NHPlugBroker("1", dry_run=False, order_no_field=["mkt_orr_no", "orr_no"],
                     call=lambda p, i: {"Output_0": out, "message": {"usr_msg": "m"}}, base_url=MOCK)
    r = b.place_order(OrderRequest(client_order_id=str(out), market=Market.KR, symbol="005930",
                                   side=Side.BUY, quantity=1, order_type=OrderType.MARKET))
    assert r.status is status and r.broker_order_id == no and r.message == "m"


# ---------------------------------------------------------------- F4
SH = ShadowSettings(enabled=True, extra_symbols={"KR": ["000660"]}, max_calls_per_day=3,
                    monthly_budget_krw=1000, usd_krw=1400, usd_per_m_input_tokens=0.042)


@pytest.fixture
def cfg():
    return StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")


def jev_engine(cfg, body, shadow=SH, theses=None):
    sent = []

    def post(url, **kw):
        sent.append(kw["json"]["state"])
        return FakeResp(body=body)

    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    audit = AuditLog()
    policy = JevPolicy(cfg, JevClient("jev-1.13.0", api_key="k", post=post))
    eng = TradingEngine(gate=RiskGate(limits, Ledger()), broker=PaperBroker(limits.initial_capital_krw),
                        policy=policy, theses=theses or store(), min_bars=cfg.min_bars, audit=audit,
                        entry_order_type=OrderType.MARKET,
                        shadow=ShadowObserver(policy, audit, shadow) if shadow else None)
    return eng, audit, sent


def feed(eng, bars):
    for b in bars:
        eng.broker.set_price(b.market, b.symbol, b.close)
        eng.on_bar(b, b.start + timedelta(minutes=1))


FLAT = [50000] * 23


def test_shadow_records_without_thesis_and_never_trades(cfg):
    eng, audit, sent = jev_engine(cfg, answers(p_align=0.9))
    feed(eng, make_bars(FLAT, symbol="000660"))  # 논지 없는 섀도 전용 종목
    sh = audit.events("shadow")
    assert len(sh) == 3 and all(not e["payload"]["has_thesis"] for e in sh)  # 일 상한 3
    assert "No research thesis" in sent[0]["thesis"]["rationale"]
    assert not eng.positions and not audit.events("order")
    assert audit.events("shadow_stopped")[0]["payload"]["reason"].startswith("일 호출 상한")


def test_shadow_skips_bars_with_real_decision(cfg):
    eng, audit, _ = jev_engine(cfg, answers(p_align=0.1), shadow=SH.model_copy(update={"max_calls_per_day": 99}))
    feed(eng, make_bars(FLAT))  # 논지 있는 종목: 매 봉 실제 판단 → 섀도 없음
    assert len(audit.events("decision")) == 4 and not audit.events("shadow")


def test_shadow_monthly_budget(cfg):
    body = {**answers(), "usage": {"input_tokens": 20_000_000, "output_tokens": 0}}  # 1회 ≈ 1,176원
    eng, audit, _ = jev_engine(cfg, body, shadow=SH.model_copy(update={"max_calls_per_day": 99}))
    feed(eng, make_bars(FLAT, symbol="000660"))
    assert len(audit.events("shadow")) == 1
    assert "월 예산" in audit.events("shadow_stopped")[0]["payload"]["reason"]


def test_calibration_uses_shadow_and_splits(cfg):
    a = AuditLog()
    t0 = datetime(2026, 10, 28, 10, 0, tzinfo=KST)
    for i in range(40):
        a.record("bar", {"start": t0 + timedelta(minutes=i), "c": 100 + i}, market="KR", symbol="X")
    det = lambda p: {"answers": {"thesis_aligned": {"noul": p}}, "state": {"indicators": {"last": 100}}}
    a.record("decision", {"detail": det(0.8)}, market="KR", symbol="X", ts=t0 + timedelta(minutes=1))
    for i in range(3):
        a.record("shadow", {"has_thesis": False, "detail": det(0.7)}, market="KR", symbol="X",
                 ts=t0 + timedelta(minutes=2 + i))
    assert jev_calibration(a).n == 4
    assert jev_calibration(a, has_thesis=True).n == 1
    assert jev_calibration(a, has_thesis=False).n == 3


# ---------------------------------------------------------------- F9
def test_only_one_broker_lane(cfg, tmp_path):
    nh = lambda: NHPlugBroker("1", call=lambda p, i: {}, base_url=MOCK)
    eng, *_ = jev_engine(cfg, answers(), shadow=None)
    with pytest.raises(ValueError):
        run_loop([Lane(eng, nh()), Lane(eng, nh())], LoopConfig([], tmp_path / "KILL"))


def test_paper_slippage_is_adverse():
    b = PaperBroker(Decimal(1_000_000), slippage_bps=Decimal(10))
    b.set_price(Market.KR, "X", Decimal(10000))
    buy = b.place_order(OrderRequest(client_order_id="b", market=Market.KR, symbol="X", side=Side.BUY,
                                     quantity=1, order_type=OrderType.MARKET))
    sell = b.place_order(OrderRequest(client_order_id="s", market=Market.KR, symbol="X", side=Side.SELL,
                                      quantity=1, order_type=OrderType.MARKET))
    assert buy.fill_price == Decimal(10010) and sell.fill_price == Decimal(9990)


# ---------------------------------------------------------------- D1
def test_us_blocked_while_p4_paused():
    from harness.bootstrap import StartupBlocked, assemble, p4_enabled
    assert not p4_enabled()
    with pytest.raises(StartupBlocked, match="보류"):
        assemble(mode="mock", theses_path="x.yaml", policies=["jev"], broker_policy="jev",
                 risk_file="risk.yaml", send=False, entry_order_type=OrderType.MARKET,
                 markets=(Market.KR, Market.US))
