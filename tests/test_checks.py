"""H1: 첫 실서버 점검 도구(harness/checks.py)를 가짜 NH 로 검증 · H2 판정 로직."""
import queue
import shutil
from datetime import datetime
from decimal import Decimal

import pytest

from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker
from harness.checks import Checker, Deps, apply_verified, judge_volume
from harness.market.nh_feed import FeedFields
from harness.market.notices import FillNotice

from .conftest import KST, ROOT

MOCK = lambda: "https://moapi.nhplug.com:8443"
IN_SESSION = datetime(2026, 10, 28, 10, 0, tzinfo=KST)
BF = BalanceFields("Output_1", "iem_cd", "itg_bnc_qty", "phs_pr")
FF = FeedFields("price", "movolume")


class FakeNH:
    def __init__(self, order_no=True, appear_after=1, qty_field="itg_bnc_qty"):
        self.order_no, self.appear_after, self.qty_field = order_no, appear_after, qty_field
        self.polls, self.sent, self.held, self.no = 0, [], 0, 500

    def __call__(self, path, i0):
        if path == "/n2/acctinfo":
            return {"Output_0": [{"acct_no": "1", "acct_type": "03"}]}
        if path.endswith("/inquiry/v1/balance"):
            self.polls += 1
            rows = []
            if self.held and self.polls >= self.appear_after:
                rows = [{"iem_cd": "005930", self.qty_field: str(self.held), "phs_pr": "52000"}]
            return {"Output_1": rows, "message": {"usr_msg": "조회"}}
        self.sent.append(path)
        self.held += 1 if path.endswith("cashBuy") else -min(1, self.held)
        self.no += 1
        out = {"mkt_orr_no": self.no} if self.order_no else {}
        return {"Output_0": out, "message": {"usr_msg": "주문완료"}}


class FakeNotices:
    def __init__(self, nh, mode="match"):
        self.nh, self.mode = nh, mode
        self.notices: queue.Queue = queue.Queue()

    def start(self):
        pass

    def feed(self):
        if self.mode == "none":
            return
        for k in range(len(self.nh.sent)):
            no = self.nh.no - len(self.nh.sent) + 1 + k
            if self.mode == "other":
                no += 9000
            self.notices.put(FillNotice(str(no).zfill(10), "005930", 1, Decimal(52000), False))


def per_trade_pushes(n=12, cumulative=False):
    out, cum = [], 1000
    for i in range(n):
        q = 1 + i % 3
        cum += q
        out.append({"header": {"tr_key": "005930"},
                    "body": {"price": "52000", "volume": str(cum), "movolume": str(cum if cumulative else q)}})
    return out


def make(nh=None, notices="match", pushes=None, now=IN_SESSION, raw_dir=None):
    nh = nh or FakeNH()
    sleeps = []
    nf = FakeNotices(nh, notices)

    def sleep(sec):
        sleeps.append(sec)
        nf.feed()

    def subscribe(keys, cb, **kw):
        for m in (per_trade_pushes() if pushes is None else pushes):
            cb(m)
        return 0

    broker = NHPlugBroker("1", dry_run=False, order_no_field=["mkt_orr_no"], call=nh, base_url=MOCK,
                          balance_fields=BF)
    deps = Deps(broker=broker, feed_fields=FF, subscribe=subscribe, clock=lambda: now, sleep=sleep,
                notice_feed=lambda: nf, run_ping=lambda name: (True, "ok"), raw_dir=raw_dir)
    return Checker(deps), nh, sleeps


def by_name(rep):
    return {r["name"]: r["ok"] for r in rep.results}


def test_happy_path_confirms_everything(tmp_path):
    c, nh, _ = make(raw_dir=tmp_path / "raw")
    rep = c.run(send=True)
    assert rep.apply_ok, rep.results
    assert nh.sent == ["/krstock/order/v1/cashBuy", "/krstock/order/v1/cashSell"] and nh.held == 0
    assert rep.facts == {"notice_order_no_matches": True, "volume_is_per_trade": True}
    assert all(by_name(rep).values())
    assert list((tmp_path / "raw").glob("*.json"))


def test_outside_session_sends_no_orders():
    c, nh, _ = make(now=datetime(2026, 10, 28, 15, 25, tzinfo=KST))
    rep = c.run(send=True)
    assert nh.sent == [] and by_name(rep)["장중 확인"] is False and not rep.apply_ok


def test_missing_order_no_still_restores():
    c, nh, _ = make(nh=FakeNH(order_no=False))
    rep = c.run(send=True)
    assert by_name(rep)["주문번호 필드"] is False and not rep.apply_ok
    assert nh.sent[-1].endswith("cashSell") and nh.held == 0          # 원상복구는 시도


def test_wrong_quantity_field_blocks_apply_but_sells():
    c, nh, _ = make(nh=FakeNH(qty_field="rsdl_qty"))                  # 설정 필드(itg_bnc_qty)가 응답에 없음
    rep = c.run(send=True)
    assert by_name(rep)["잔고 필드"] is False and not rep.apply_ok
    assert nh.sent[-1].endswith("cashSell") and nh.held == 0


def test_delayed_fill_waits_up_to_30s():
    c, nh, sleeps = make(nh=FakeNH(appear_after=3))
    rep = c.run(send=True)
    assert by_name(rep)["잔고 필드"] is True and sleeps[:3] == [5, 5, 5]


def test_never_filled_reports_and_warns(capsys):
    c, nh, sleeps = make(nh=FakeNH(appear_after=99))
    rep = c.run(send=True)
    assert by_name(rep)["잔고 필드"] is False and sleeps[:6] == [5] * 6
    assert "1주가 남았는지" in capsys.readouterr().out


@pytest.mark.parametrize("mode,fact,ok", [("other", False, False), ("none", None, False)])
def test_notice_order_no_mismatch_or_missing(mode, fact, ok):
    c, _, _ = make(notices=mode)
    rep = c.run(send=True)
    assert rep.facts["notice_order_no_matches"] is fact and by_name(rep)["체결통보 주문번호"] is ok
    assert rep.apply_ok  # 체결통보는 보조 수단 — 필드 확정(apply)을 막지는 않음


def test_cumulative_volume_blocks_apply():
    c, _, _ = make(pushes=per_trade_pushes(cumulative=True))
    rep = c.run(send=True)
    assert rep.facts["volume_is_per_trade"] is False and not rep.apply_ok


def test_no_pushes_after_close():
    c, _, _ = make(pushes=[])
    rep = c.run(send=False)
    assert by_name(rep)["실시간 필드"] is False and not rep.apply_ok


def test_live_server_stops_immediately(monkeypatch):
    from harness.broker import nhplug_broker as nb
    monkeypatch.setenv(nb.LIVE_ENV_VAR, nb.LIVE_ENV_VALUE)
    b = NHPlugBroker("1", allow_live=True, call=FakeNH(), base_url=lambda: "https://api.nhplug.com:8443")
    rep = Checker(Deps(broker=b, feed_fields=FF, subscribe=lambda *a, **k: 0, clock=lambda: IN_SESSION,
                       sleep=lambda s: None)).run(send=True)
    assert [r["name"] for r in rep.results] == ["모의투자 서버"]


@pytest.mark.parametrize("bodies,expect", [
    ([{"volume": "10", "movolume": "1"}] * 2, None),                                   # 표본 부족
    ([{"volume": str(10 + i), "movolume": "1"} for i in range(6)], True),
    ([{"volume": str(10 + 5 * i), "movolume": "1"} for i in range(6)], False),         # 증가분과 불일치
])
def test_judge_volume(bodies, expect):
    assert judge_volume(bodies, "movolume")[0] is expect


def test_apply_verified(tmp_path):
    for name in ("broker_fields.yaml", "feed.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / name)
    apply_verified(tmp_path)
    for name in ("broker_fields.yaml", "feed.yaml"):
        assert "verified_by_mock: true" in (tmp_path / name).read_text(encoding="utf-8")
    assert "verified_by_mock: false" in (ROOT / "config" / "feed.yaml").read_text(encoding="utf-8")


