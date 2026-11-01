"""K7 독립 리뷰 지적 10건 수정 회귀 테스트."""
from datetime import datetime, time, timedelta
from decimal import Decimal

import requests

from harness.audit import AuditLog
from harness.broker.nhplug_broker import NHPlugBroker
from harness.decision import JevClient, JevPolicy, StrategyConfig
from harness.decision.shadow import ShadowObserver
from harness.engine import TradingEngine, restore_ledger
from harness.models import Market, OrderType, Side
from harness.research.repo import ThesisRepo
from harness.research.service import ResearchConfig, ResearchService
from harness.risk import Ledger, RiskGate, RiskLimits

from .conftest import KST, ROOT
from .helpers import make_bars, store
from .test_engine import RISING, AlwaysBuy
from .test_research import LONG, Resp, fake_client, nested
from .test_review_items import SH

MOCK = lambda: "https://moapi.nhplug.com:8443"
KEY = (Market.KR, "005930")
CFG = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")


def nh_engine(call, entry=OrderType.MARKET, audit=None):
    b = NHPlugBroker("1", dry_run=False, order_no_field=["mkt_orr_no"], call=call, base_url=MOCK)
    e = TradingEngine(gate=RiskGate(LIMITS, Ledger()), broker=b, policy=AlwaysBuy(CFG), theses=store(),
                      min_bars=CFG.min_bars, audit=audit or AuditLog(), entry_order_type=entry)
    for bar in make_bars(RISING[:20]):
        e.on_bar(bar, bar.start + timedelta(minutes=1))
    return e


# 1 — 대기 중 매수가 보유·현금 심사에 반영
def test_pending_buy_counts_toward_limits():
    e = nh_engine(lambda p, i: {"message": {"usr_msg": "접수"}})   # 주문번호 없음 → UNKNOWN 대기
    po = e.pending[KEY]
    acct = e.account()
    assert acct.holds(*KEY) and len(acct.positions) == 1
    assert acct.cash_krw == LIMITS.initial_capital_krw - po.signal.entry_price * po.qty_ordered


# 2 — 실패했지만 과금됐을 호출도 예산에 기록
def test_failed_but_billed_grok_call_is_charged(tmp_path):
    c = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
    audit = AuditLog()
    client, _ = fake_client([Resp({"output_text": "형식 오류"})])            # 200 + 파싱 실패 → 과금
    ResearchService(c, client, ThesisRepo(":memory:"), audit).run(datetime(2026, 10, 28, 8, 0, tzinfo=KST))
    assert [x["payload"]["bot"] for x in audit.events("research_cost")] == ["sentiment"]
    audit2 = AuditLog()
    client2, _ = fake_client([requests.ConnectionError("x")])               # 연결 실패 → 과금 없음
    ResearchService(c, client2, ThesisRepo(":memory:"), audit2).run(datetime(2026, 10, 28, 8, 0, tzinfo=KST))
    assert audit2.events("research_cost") == []
    audit3 = AuditLog()
    bad = {**LONG, "stance": "MAYBE"}                                         # 스키마 불일치 → 과금
    client3, _ = fake_client([Resp(nested(bad))])
    ResearchService(c, client3, ThesisRepo(":memory:"), audit3).run(datetime(2026, 10, 28, 8, 0, tzinfo=KST))
    assert len(audit3.events("research_cost")) == 1


# 3 — 취소 실패를 성공으로 처리하지 않음
def test_cancel_failure_keeps_pending_then_halts():
    def call(path, i0):
        if path.endswith("/cancel"):
            raise requests.ConnectionError("down")
        return {"Output_0": {"mkt_orr_no": 55}}
    e = nh_engine(call, entry=OrderType.LIMIT)
    t0 = e.pending[KEY].sent_at
    for k in range(1, 4):
        e.reconcile({}, t0 + timedelta(seconds=60 + 5 * k), {KEY})
        assert KEY in e.pending                         # 살아 있을 수 있는 주문을 지우지 않는다
    assert e.pending[KEY].cancel_attempts == 3 and e.gate.ledger.halted
    assert "취소 3회 실패" in e.gate.ledger.halt_reason


def test_limit_without_order_no_halts_instead_of_dropping():
    e = nh_engine(lambda p, i: {"message": {"usr_msg": "?"}}, entry=OrderType.LIMIT)
    e.reconcile({}, e.pending[KEY].sent_at + timedelta(seconds=61), {KEY})
    assert KEY in e.pending and e.gate.ledger.halted


# 5 — 장 전 1회 논지는 장 마감까지 유효
def test_premarket_thesis_lasts_until_close():
    from harness.research.aggregate import aggregate
    from harness.research.bots import BearOpinion, BotOpinion
    c = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
    ops = {k: BotOpinion(stance="LONG", confidence=0.9, reasons=["r"], risks=[]) for k in ("sentiment", "fundamental")}
    t, _, _ = aggregate(Market.KR, "005930", ops, BearOpinion(invalidation_prob=0.1, counterarguments=[]),
                        c.aggregate, datetime(2026, 10, 28, 8, 0, tzinfo=KST), c.thesis_ttl_min)
    assert t.valid_until == datetime(2026, 10, 28, 15, 30, tzinfo=KST)


# 6·7 — 섀도: 오류 후 일시 중단, 정지 중에는 호출 안 함
def test_shadow_pauses_after_error_and_skips_when_halted():
    calls = []

    def post(url, **kw):
        calls.append(1)
        raise requests.Timeout("t")
    audit = AuditLog()
    policy = JevPolicy(CFG, JevClient("jev-1.13.0", api_key="k", post=post))
    e = TradingEngine(gate=RiskGate(LIMITS, Ledger()), broker=NHPlugBroker("1", call=lambda p, i: {}, base_url=MOCK),
                      policy=policy, theses=store(), min_bars=CFG.min_bars, audit=audit,
                      shadow=ShadowObserver(policy, audit, SH.model_copy(update={"max_calls_per_day": 999})))
    for bar in make_bars([50000] * 24, symbol="000660"):   # 20번째 봉에서 오류 → 이후 4분은 쉼
        e.on_bar(bar, bar.start + timedelta(minutes=1))
    assert len(calls) == 1 and len(audit.events("shadow_error")) == 1   # 첫 오류 후 5분간 쉼
    e.gate.ledger.halt("x")
    n = len(calls)
    for bar in make_bars([50000] * 5, symbol="000660", start=datetime(2026, 10, 28, 11, 0, tzinfo=KST)):
        e.on_bar(bar, bar.start + timedelta(minutes=1))
    assert len(calls) == n


# 8 — dry-run 주문은 거래 횟수로 복원하지 않음
def test_restore_ignores_dry_run_entries():
    a = AuditLog()
    req = {"side": Side.BUY.value, "quantity": 1}
    for status in ("DRY_RUN", "DRY_RUN", "ACCEPTED"):
        a.record("order", {"request": req, "result": {"status": status}}, market="KR", symbol="005930",
                 ts=datetime(2026, 10, 28, 10, 0, tzinfo=KST))
    g = RiskGate(LIMITS, Ledger())
    restore_ledger(a, g)
    assert g.ledger.trades(datetime(2026, 10, 28).date(), Market.KR) == 1


# 10 — 벤치마크 자본은 설정값 사용
def test_promotion_capital_from_config(monkeypatch):
    from harness import benchmark
    from harness.promotion import PromotionCriteria, evaluate
    seen = {}
    real = benchmark.compare

    def spy(audit, cap, *a, **k):
        seen["cap"] = cap
        return real(audit, cap, loaders=[("fake", lambda s, e: [100.0, 100.0])])
    monkeypatch.setattr(benchmark, "compare", spy)
    a = AuditLog()
    evaluate(a, AuditLog(), PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml"),
             datetime(2026, 11, 1, tzinfo=KST), True)
    assert seen["cap"] == LIMITS.initial_capital_krw
    evaluate(a, AuditLog(), PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml"),
             datetime(2026, 11, 1, tzinfo=KST), True, capital_krw=Decimal(1_000_000))
    assert seen["cap"] == 1_000_000


# 9 — 장 종료 후 청산 못 한 보유가 있으면 30분 뒤 알리고 종료
def test_runner_exits_when_position_stuck_after_close(tmp_path):
    from . import test_runner_sim as sim

    class NoSell(sim.FakeNH):
        def __call__(self, path, i0):
            if path.endswith("cashSell"):
                return {"message": {"usr_msg": "매도 불가"}}
            return super().__call__(path, i0)

    orig = sim.FakeNH
    sim.FakeNH = NoSell
    try:
        lanes, nh, msgs, clock = sim.world(tmp_path, calm_from=time(14, 40))
    finally:
        sim.FakeNH = orig
    assert any("청산되지 않음" in m for m in msgs)
    assert clock.t < datetime.combine(sim.DAY, time(16, 30), KST)
    assert lanes[0].engine.positions or lanes[0].engine.pending
