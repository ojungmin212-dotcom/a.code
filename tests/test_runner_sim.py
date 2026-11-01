"""G4: 운영 루프 시뮬레이션 — 가짜 NH(주문·잔고·체결통보)와 가짜 시세로 장중 하루를 통째로 돌린다.

실서버 없이 검증하는 것: 시세 → 1분봉 → 판단 → 주문 → 체결통보/잔고 대조 → 손절·익절·마감 청산
→ 장 마감 요약 → 종료, 그리고 킬 스위치·장부 불일치 정지.
"""
import math
import queue
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from harness.audit import AuditLog
from harness.broker import PaperBroker
from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker
from harness.decision.policy import StrategyConfig
from harness.engine import TradingEngine, unreconciled_symbols
from harness.market.bars import Tick
from harness.market.nh_feed import FeedFields
from harness.market.notices import FillNotice
from harness.models import Market, OrderType
from harness.risk import Ledger, RiskGate, RiskLimits
from harness.runner import Lane, LoopConfig, MarketSpec, run_loop

from .conftest import KST, ROOT
from .helpers import store
from .test_engine import AlwaysBuy

DAY = datetime(2026, 10, 28, tzinfo=KST).date()
SYM = "005930"
KEY = (Market.KR, SYM)


class SimClock:
    def __init__(self, t: datetime, hooks=None):
        self.t, self.hooks = t, hooks or []

    def __call__(self):
        for at, fn in list(self.hooks):
            if self.t >= at:
                fn()
                self.hooks.remove((at, fn))
        if self.t > datetime.combine(DAY, time(18, 0), KST):
            raise RuntimeError("시뮬레이션이 장 마감 후에도 끝나지 않음(무한 루프)")
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


class SimQueue:
    def __init__(self, clock, ticks, prices):
        self.clock, self.ticks, self.prices = clock, list(ticks), prices

    def get(self, timeout=1.0):
        if self.ticks and self.ticks[0].ts <= self.clock.t + timedelta(seconds=timeout):
            t = self.ticks.pop(0)
            self.clock.t = max(self.clock.t, t.ts)
            self.prices[t.symbol] = t.price
            return t
        self.clock.t += timedelta(seconds=timeout)
        raise queue.Empty


class SimFeed:
    def __init__(self, q):
        self.ticks = q

    def start(self):
        pass

    def alive(self):
        return True


class SimNotices:
    def __init__(self):
        self.notices: queue.Queue = queue.Queue()

    def start(self):
        pass

    def alive(self):
        return True


class FakeNH:
    """주문 즉시 현재가로 체결, 체결통보 발송, 잔고 반영."""

    def __init__(self, prices, notices):
        self.prices, self.notices = prices, notices
        self.holdings: dict[str, tuple[int, Decimal]] = {}
        self.extra: dict[str, int] = {}  # 하네스 모르게 생긴 보유(수동 매매 가정)
        self.order_no = 100
        self.orders: list[tuple[str, dict]] = []

    def __call__(self, path, i0):
        if path.endswith("/inquiry/v1/balance"):
            rows = []
            for s in set(self.holdings) | set(self.extra):
                q, avg = self.holdings.get(s, (0, Decimal(0)))
                rows.append({"iem_cd": s, "itg_bnc_qty": str(q + self.extra.get(s, 0)), "phs_pr": str(avg)})
            return {"Output_1": rows, "message": {"usr_msg": "조회완료"}}
        self.orders.append((path, i0))
        sym, qty, px = i0["iem_cd"], i0["orr_qty"], self.prices[i0["iem_cd"]]
        held, avg = self.holdings.get(sym, (0, Decimal(0)))
        if path.endswith("cashSell"):
            if qty > held:
                return {"message": {"usr_msg": "매도가능수량 부족"}}
            left = held - qty
            self.holdings[sym] = (left, avg if left else Decimal(0))
        else:
            self.holdings[sym] = (held + qty, (avg * held + px * qty) / (held + qty))
        self.order_no += 1
        self.notices.notices.put(FillNotice(str(self.order_no).zfill(10), sym, qty, px, False))
        return {"Output_0": {"mkt_orr_no": self.order_no}, "message": {"usr_msg": "주문완료"}}


def day_ticks(every_sec=20, calm_from=None):
    """기본: 완만한 파동 + 상승 추세. calm_from: 그 시각부터 ±50원만 흔들리는 장(손절·익절 미도달 → 마감 청산)."""
    out = []
    t = datetime.combine(DAY, calm_from or time(9, 0), KST)
    end = datetime.combine(DAY, time(15, 29), KST)
    i = 0
    while t <= end:
        if calm_from:
            p = 50000 + (50 if (i // 3) % 2 else -50)
        else:
            p = 50000 + 1500 * math.sin(i / 90) + i * 2
        out.append(Tick(Market.KR, SYM, t, Decimal(int(p // 50) * 50), 10))
        t += timedelta(seconds=every_sec)
        i += 1
    return out


def world(tmp_path, hooks_fn=None, calm_from=None):
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    clock = SimClock(datetime.combine(DAY, time(8, 55), KST))
    prices: dict[str, Decimal] = {}
    notices = SimNotices()
    nh = FakeNH(prices, notices)
    msgs: list[str] = []

    def engine(broker):
        return TradingEngine(gate=RiskGate(limits, Ledger()), broker=broker, policy=AlwaysBuy(cfg), theses=store(),
                             min_bars=cfg.min_bars, audit=AuditLog(), entry_order_type=OrderType.MARKET,
                             notify=msgs.append)

    live = NHPlugBroker("1", dry_run=False, order_no_field=["mkt_orr_no"], call=nh,
                        base_url=lambda: "https://moapi.nhplug.com:8443",
                        balance_fields=BalanceFields("Output_1", "iem_cd", "itg_bnc_qty", "phs_pr"))
    paper = PaperBroker(limits.initial_capital_krw, slippage_bps=Decimal(10))
    lanes = [Lane(engine(live), live), Lane(engine(paper), paper)]
    spec = MarketSpec(Market.KR, [SYM], FeedFields("price", "movolume"), "mc", {}, [SYM])
    cfg_loop = LoopConfig([spec], tmp_path / "KILL", fill_notices=True, tick_dir=tmp_path / "ticks",
                          heartbeat_min=60, pending_timeout_sec=60)
    if hooks_fn:
        clock.hooks = hooks_fn(tmp_path, nh)
    q = SimQueue(clock, day_ticks(calm_from=calm_from), prices)
    run_loop(lanes, cfg_loop, clock=clock, sleep=clock.sleep,
             feed_factory=lambda s: SimFeed(q), notice_factory=lambda: notices)
    return lanes, nh, msgs, clock


def ts(e):
    return datetime.fromisoformat(e["ts"])


def test_full_day(tmp_path):
    lanes, nh, msgs, clock = world(tmp_path)
    live, paper = lanes[0].engine, lanes[1].engine
    close = datetime.combine(DAY, time(15, 30), KST)

    # 루프가 장 마감 후 스스로 종료하고 요약을 보냈다
    assert clock.t >= close and any("장 마감 요약" in m for m in msgs)
    assert any("생존 신호" in m for m in msgs)
    # 두 레인 모두 거래했고, 끝에는 보유·대기 없음
    for e in (live, paper):
        assert e.audit.events("exit"), e.policy.name
        assert not e.positions and not e.pending
        assert unreconciled_symbols(e.audit) == []
        assert not e.gate.ledger.halted
        # 청산은 모두 장 마감 전, 마감 청산은 15:15 이후
        for x in e.audit.events("exit"):
            assert ts(x) < close
            if x["payload"]["reason"] == "flatten":
                assert ts(x) >= datetime.combine(DAY, time(15, 15), KST)
        # 장부 손익 = 청산 기록 합
        total = sum(Decimal(x["payload"]["pnl_krw"]) for x in e.audit.events("exit"))
        assert e.gate.ledger.cumulative_realized_krw == total
    # 브로커 레인은 체결통보로 정확한 체결가를 확정(추정 아님), 가짜 계좌도 0주
    assert all(x["payload"]["estimated_price"] is False for x in live.audit.events("exit"))
    assert nh.holdings.get(SYM, (0, 0))[0] == 0
    assert not [m for m in live.audit.events("order") if m["payload"]["result"]["status"] == "UNKNOWN"]
    # 하루 거래 횟수 한도 준수, 틱 기록 생성
    assert len([o for o in nh.orders if o[0].endswith("cashBuy")]) <= live.gate.limits.max_trades_per_day[Market.KR]
    assert list((tmp_path / "ticks").glob("*.csv"))


def test_flatten_before_close(tmp_path):
    lanes, nh, msgs, _ = world(tmp_path, calm_from=time(14, 40))
    for ln in lanes:
        exits = ln.engine.audit.events("exit")
        assert exits and all(x["payload"]["reason"] == "flatten" for x in exits), ln.engine.policy.name
        first = ts(exits[0])
        assert datetime.combine(DAY, time(15, 15), KST) <= first < datetime.combine(DAY, time(15, 17), KST)
        assert not ln.engine.positions
    assert nh.holdings.get(SYM, (0, 0))[0] == 0


def test_kill_switch_midday(tmp_path):
    kill_at = datetime.combine(DAY, time(11, 0), KST)
    lanes, nh, msgs, _ = world(tmp_path, lambda d, nh: [(kill_at, lambda: (d / "KILL").touch())])
    for ln in lanes:
        e = ln.engine
        assert e.gate.ledger.halted and "킬 스위치" in e.gate.ledger.halt_reason
        assert not e.positions
        buys = [o for o in e.audit.events("order") if o["payload"]["request"]["side"] == "BUY"]
        assert all(ts(o) < kill_at + timedelta(minutes=1) for o in buys)
    assert nh.holdings.get(SYM, (0, 0))[0] == 0


def test_manual_trade_mismatch_halts_and_flattens_own_position(tmp_path):
    at = datetime.combine(DAY, time(10, 30), KST)
    lanes, nh, msgs, _ = world(tmp_path, lambda d, nh: [(at, lambda: nh.extra.update({SYM: 3}))])
    live = lanes[0].engine
    assert live.gate.ledger.halted and "불일치" in live.gate.ledger.halt_reason
    assert any("불일치" in m for m in msgs)
    assert not live.positions                       # 하네스 자신의 보유분은 청산
    assert not lanes[1].engine.gate.ledger.halted   # 메모리 레인은 영향 없음


@pytest.mark.parametrize("weekday_offset", [3])  # 2026-10-31 토요일
def test_weekend_exits_immediately(tmp_path, weekday_offset):
    global DAY
    saved = DAY
    try:
        DAY = saved + timedelta(days=weekday_offset)
        lanes, nh, msgs, clock = world(tmp_path)
        assert not nh.orders and clock.t < datetime.combine(DAY, time(9, 5), KST)
    finally:
        DAY = saved
