from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from harness.audit import AuditLog
from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker, UsConfig
from harness.decision.policy import StrategyConfig
from harness.engine import TradingEngine
from harness.market.fx import FxRate, FxStale
from harness.market.instruments import gic_map
from harness.market.nh_feed import FeedFields, NHTickFeed, parse_push
from harness.models import Market, OrderRequest, OrderType, Side
from harness.promotion import PromotionCriteria, evaluate_p4
from harness.research.thesis import Thesis, ThesisStore
from harness.risk import Ledger, RiskGate, RiskLimits

from .conftest import KST, ROOT
from .helpers import make_bars
from .test_engine import AlwaysBuy

MOCK = lambda: "https://moapi.nhplug.com:8443"
US_CFG = UsConfig("/gbstock/order/v1/sell",
                  BalanceFields("Output_1", "iem_cd", "hld_qty", "avg_pr"))


class Calls:
    def __init__(self, kr=None, us=None):
        self.kr, self.us, self.sent = kr, us, []

    def __call__(self, path, input_0):
        self.sent.append((path, input_0))
        if path.endswith("/krstock/inquiry/v1/balance"):
            return self.kr
        if path.endswith("/gbstock/inquiry/v1/balance"):
            return self.us
        return {"rsp_msg": "접수", "Output_0": {}}


def us_req(side, cid="u1"):
    return OrderRequest(client_order_id=cid, market=Market.US, symbol="AAPL", side=side,
                        quantity=1, order_type=OrderType.MARKET)


def test_us_orders_need_sell_path():
    with pytest.raises(NotImplementedError):
        NHPlugBroker("1", call=Calls(), base_url=MOCK).place_order(us_req(Side.BUY))


def test_us_payloads():
    b = NHPlugBroker("1", call=Calls(), base_url=MOCK, us=US_CFG)
    buy = b.place_order(us_req(Side.BUY, "b")).raw
    sell = b.place_order(us_req(Side.SELL, "s")).raw
    assert buy["path"] == "/gbstock/order/v1/buy" and sell["path"] == "/gbstock/order/v1/sell"
    assert buy["Input_0"]["fc_sec_trd_nat_cd"] == "200" and buy["Input_0"]["ahi_nmn_pr_tp_cd"] == "03"
    lim = OrderRequest(client_order_id="l", market=Market.US, symbol="AAPL", side=Side.BUY, quantity=1,
                       order_type=OrderType.LIMIT, limit_price=Decimal("187.456"))
    i0 = b.place_order(lim).raw["Input_0"]
    assert i0["ahi_nmn_pr_tp_cd"] == "00" and i0["fc_orr_uit_pr"] == 187.46


def test_holdings_kr_and_us():
    kr = {"Output_1": [{"k": "005930", "q": "2", "a": "50000"}]}
    us = {"Output_1": [{"iem_cd": "AAPL", "hld_qty": "1", "avg_pr": "187.5"}]}
    b = NHPlugBroker("1", call=Calls(kr, us), base_url=MOCK,
                     balance_fields=BalanceFields("Output_1", "k", "q", "a"), us=US_CFG)
    assert b.holdings() == {(Market.KR, "005930"): (2, Decimal(50000)),
                            (Market.US, "AAPL"): (1, Decimal("187.5"))}


def test_fx_rate_checks():
    now = datetime(2026, 10, 28, 22, 0, tzinfo=KST)
    ok = FxRate.from_dict({"usd_krw": 1400, "as_of": "2026-10-27", "max_age_days": 3})
    assert ok.check(now) == 1400
    with pytest.raises(FxStale):
        FxRate.from_dict({"usd_krw": 1400, "as_of": "2026-10-20"}).check(now)
    with pytest.raises(FxStale):
        FxRate.from_dict({"usd_krw": None, "as_of": None}).check(now)
    with pytest.raises(FxStale):
        FxRate.from_dict({"usd_krw": 14, "as_of": "2026-10-28"}).check(now)  # 단위 실수


def test_gic_map():
    rows = [{"symbol": "AAPL", "country_code": "USA", "gic": "G0000AAPL000001"},
            {"symbol": "AAPL", "country_code": "DEU", "gic": "G0000AAPLDE0001"},
            {"symbol": "MSFT", "country_code": "USA", "gic": "G0000MSFT000001"}]
    assert gic_map(["aapl"], loader=lambda k: rows) == {"AAPL": "G0000AAPL000001"}
    with pytest.raises(ValueError):
        gic_map(["NVDA"], loader=lambda k: rows)
    with pytest.raises(ValueError):
        gic_map(["AAPL"], loader=lambda k: rows + [{"symbol": "AAPL", "country_code": "USA", "gic": "X"}])


def test_us_feed_maps_gic_back_to_ticker():
    got = {}
    feed = NHTickFeed(["AAPL"], FeedFields("p", "v"), market=Market.US, tr_cd="RC",
                      keys={"AAPL": "G1"}, subscribe=lambda keys, cb, **kw: got.update(keys=keys, **kw))
    feed.start()
    feed._thread.join()
    assert got["keys"] == ["G1"] and got["tr_cd"] == "RC"
    feed._on_message({"header": {"tr_key": "G1"}, "body": {"p": "187.25", "v": "3"}})
    t = feed.ticks.get_nowait()
    assert (t.market, t.symbol, t.price) == (Market.US, "AAPL", Decimal("187.25"))
    assert parse_push({"header": {"tr_key": "G9"}, "body": {"p": "1", "v": "1"}}, FeedFields("p", "v"),
                      market=Market.US, key_to_symbol={"G1": "AAPL"}) is None


# ---------------------------------------------------------------- 엔진: 미국 + 환율
def us_engine(fx):
    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    from harness.broker import PaperBroker
    broker = PaperBroker(limits.initial_capital_krw)
    t0 = datetime(2026, 10, 28, 23, 40, tzinfo=KST)  # 미국 10:40 EDT
    theses = ThesisStore([Thesis(market=Market.US, symbol="AAPL", direction="LONG", rationale="t",
                                 valid_from=t0 - timedelta(hours=1), valid_until=t0 + timedelta(hours=5))])
    eng = TradingEngine(gate=RiskGate(limits, Ledger()), broker=broker, policy=AlwaysBuy(cfg),
                        theses=theses, min_bars=cfg.min_bars, audit=AuditLog(),
                        entry_order_type=OrderType.MARKET, fx_rates=fx)
    bars = [b.__class__(Market.US, "AAPL", b.start, b.open, b.high, b.low, b.close, b.volume)
            for b in make_bars([Decimal("150") + Decimal("0.3") * i for i in range(20)], start=t0,
                               spread=Decimal("0.4"))]
    return eng, broker, bars


def run(eng, broker, bars):
    for b in bars:
        broker.set_price(Market.US, "AAPL", b.close)
        eng.on_bar(b, b.start + timedelta(minutes=1))


def test_no_us_entry_without_fx():
    eng, broker, bars = us_engine({Market.KR: Decimal(1)})
    run(eng, broker, bars)
    assert not eng.positions and eng.policy.calls == 0


def test_us_entry_and_exit_pnl_uses_exit_fx():
    eng, broker, bars = us_engine({Market.KR: Decimal(1), Market.US: Decimal(1400)})
    run(eng, broker, bars)
    key = (Market.US, "AAPL")
    pos = eng.positions[key]
    assert pos.fx == 1400 and pos.quantity >= 1
    eng.set_fx({Market.US: Decimal(1300)}, bars[-1].start)  # 원화 강세 → 환차손
    eng.flatten_if_due(datetime(2026, 10, 29, 4, 50, tzinfo=KST))  # 마감 10분 전(EDT 15:50)
    assert key not in eng.positions
    pnl = Decimal(eng.audit.events("exit")[0]["payload"]["pnl_krw"])
    assert pnl < -pos.avg_price * pos.quantity * 50  # 환율 100원 하락분 손실 반영


def test_fx_removal_blocks_new_entries():
    eng, broker, bars = us_engine({Market.KR: Decimal(1), Market.US: Decimal(1400)})
    eng.set_fx({}, bars[0].start)
    run(eng, broker, bars)
    assert not eng.positions and eng.audit.events("fx_unavailable")


# ---------------------------------------------------------------- P4 심사
def log(market, pnls, start, slip_bps=10, halts=0):
    a = AuditLog()
    for i, p in enumerate(pnls):
        a.record("fill", {"side": "BUY", "qty": 1, "price": 100 * (1 + slip_bps / 10_000),
                          "signal_price": 100}, market=market, symbol="X", ts=start + timedelta(hours=i))
        a.record("exit", {"pnl_krw": Decimal(p), "qty": 1}, market=market, symbol="X",
                 ts=start + timedelta(hours=i, minutes=30))
    for _ in range(halts):
        a.record("halt", {"reason": "킬 스위치"})
    return a


def test_p4_promotion():
    c = PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml").p4
    start = datetime(2026, 9, 1, 10, 0, tzinfo=KST)
    now = start + timedelta(weeks=5)
    live, mock = log("KR", [50] * 20, start), log("US", [30] * 30, start)
    assert evaluate_p4(live, mock, c, now, True).passed
    fails = lambda **kw: [n for n, ok, _ in evaluate_p4(
        kw.get("live", live), kw.get("mock", mock), c, kw.get("now", now), True).checks if not ok]
    assert fails(live=log("KR", [50] * 20, start, slip_bps=40)) == ["체결 슬리피지 중앙값"]
    assert fails(live=log("KR", [50] * 20, start, halts=1)) == ["실거래 정지 이력"]
    assert fails(mock=log("US", [-30] * 30, start)) == ["미국 모의 손익 > 0"]
    assert fails(mock=log("KR", [30] * 30, start)) == ["미국 모의 기간", "미국 모의 횟수", "미국 모의 손익 > 0"]
