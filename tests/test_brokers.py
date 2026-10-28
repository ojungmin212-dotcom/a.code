from decimal import Decimal

import pytest
from nhplug import NhplugError

from harness.broker import DuplicateOrderError, PaperBroker
from harness.broker.nhplug_broker import LIVE_ENV_VALUE, LIVE_ENV_VAR, LiveTradingBlocked, NHPlugBroker
from harness.models import Market, OrderRequest, OrderStatus, OrderType, Side

MOCK = lambda: "https://moapi.nhplug.com:8443"
LIVE = lambda: "https://api.nhplug.com:8443"


def req(cid="c1", side=Side.BUY, qty=2, price=Decimal(50000), market=Market.KR, symbol="005930"):
    return OrderRequest(client_order_id=cid, market=market, symbol=symbol, side=side,
                        quantity=qty, order_type=OrderType.LIMIT, limit_price=price)


# ---------------------------------------------------------------- PaperBroker
def test_paper_round_trip_with_fees():
    b = PaperBroker(Decimal(500000))
    b.set_price(Market.KR, "005930", Decimal(50000))
    assert b.place_order(req("b1")).status is OrderStatus.FILLED
    assert b.cash_krw == Decimal(500000) - Decimal(100000) - Decimal(15)
    b.set_price(Market.KR, "005930", Decimal(51000))
    assert b.place_order(req("s1", side=Side.SELL, price=Decimal(51000))).status is OrderStatus.FILLED
    assert b.account_state().positions == ()
    assert b.cash_krw == Decimal(500000) + Decimal(2000) - Decimal(15) - Decimal("15.3")


def test_paper_rejects_short_and_overspend_and_duplicates():
    b = PaperBroker(Decimal(50000))
    b.set_price(Market.KR, "005930", Decimal(50000))
    assert b.place_order(req("x1", side=Side.SELL)).status is OrderStatus.REJECTED
    assert b.place_order(req("x2")).status is OrderStatus.REJECTED
    with pytest.raises(DuplicateOrderError):
        b.place_order(req("x2"))


# ---------------------------------------------------------------- NHPlugBroker
class FakeCall:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, path, input_0):
        self.calls.append((path, input_0))
        if self.error:
            raise self.error
        return self.response


def test_live_server_blocked_by_default(monkeypatch):
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    with pytest.raises(LiveTradingBlocked):
        NHPlugBroker("123", call=FakeCall(), base_url=LIVE)
    with pytest.raises(LiveTradingBlocked):  # allow_live 만으로는 부족
        NHPlugBroker("123", allow_live=True, call=FakeCall(), base_url=LIVE)
    monkeypatch.setenv(LIVE_ENV_VAR, LIVE_ENV_VALUE)
    NHPlugBroker("123", allow_live=True, call=FakeCall(), base_url=LIVE)


def test_env_rechecked_before_send():
    url = {"v": MOCK()}
    b = NHPlugBroker("123", dry_run=False, call=FakeCall({}), base_url=lambda: url["v"])
    url["v"] = LIVE()
    with pytest.raises(LiveTradingBlocked):
        b.place_order(req())


def test_dry_run_does_not_call():
    fake = FakeCall()
    r = NHPlugBroker("123", call=fake, base_url=MOCK).place_order(req())
    assert r.status is OrderStatus.DRY_RUN and fake.calls == []
    assert r.raw["path"] == "/krstock/order/v1/cashBuy"
    assert r.raw["Input_0"]["orr_pr"] == 50000 and r.raw["Input_0"]["nmn_pr_tp_cd"] == "01"


def test_market_order_payload():
    b = NHPlugBroker("123", call=FakeCall(), base_url=MOCK)
    r = b.place_order(OrderRequest(client_order_id="m", market=Market.KR, symbol="005930",
                                   side=Side.SELL, quantity=1, order_type=OrderType.MARKET))
    assert r.raw["path"].endswith("cashSell")
    assert r.raw["Input_0"]["nmn_pr_tp_cd"] == "05" and "orr_pr" not in r.raw["Input_0"]


def test_http_200_without_order_no_is_unknown():
    fake = FakeCall({"rsp_cd": "00000", "rsp_msg": "정상처리", "Output_0": {}})
    r = NHPlugBroker("123", dry_run=False, call=fake, base_url=MOCK).place_order(req())
    assert r.status is OrderStatus.UNKNOWN and r.message == "정상처리"


def test_accepted_only_with_order_no_field():
    fake = FakeCall({"rsp_msg": "주문완료", "Output_0": {"ord_no": "0001234"}})
    b = NHPlugBroker("123", dry_run=False, order_no_field="ord_no", call=fake, base_url=MOCK)
    r = b.place_order(req())
    assert r.status is OrderStatus.ACCEPTED and r.broker_order_id == "0001234"


@pytest.mark.parametrize("category,expected", [
    ("rate_limit", OrderStatus.REJECTED), ("auth", OrderStatus.REJECTED),
    ("network", OrderStatus.UNKNOWN), ("http", OrderStatus.UNKNOWN),
])
def test_error_mapping(category, expected):
    fake = FakeCall(error=NhplugError("x", category=category))
    r = NHPlugBroker("123", dry_run=False, call=fake, base_url=MOCK).place_order(req())
    assert r.status is expected


def test_no_retry_after_unknown():
    fake = FakeCall(error=NhplugError("timeout", category="network"))
    b = NHPlugBroker("123", dry_run=False, call=fake, base_url=MOCK)
    assert b.place_order(req("dup")).status is OrderStatus.UNKNOWN
    with pytest.raises(DuplicateOrderError):
        b.place_order(req("dup"))
    assert len(fake.calls) == 1


def test_us_and_fractional_rejected_without_consuming_id():
    b = NHPlugBroker("123", call=FakeCall(), base_url=MOCK)
    with pytest.raises(NotImplementedError):
        b.place_order(req("u", market=Market.US, symbol="AAPL", price=Decimal(100)))
    with pytest.raises(ValueError):
        b.place_order(req("f", price=Decimal("50000.5")))
    assert b.place_order(req("u")).status is OrderStatus.DRY_RUN
