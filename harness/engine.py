"""빠른 루프 엔진: 1분봉 마감마다 청산 감시 → 진입 판단 → 리스크 게이트 → 주문.

포지션·현금은 엔진 내부 장부로 관리한다(체결 결과 FILLED 만 반영).
브로커가 ACCEPTED·UNKNOWN 을 돌려주면 그 종목을 '대조 필요(pending)'로 묶고 추가 주문을 하지 않는다.
대조는 체결통보/잔고 파싱(P1 미완) 또는 운영자 확인 후 resolve_pending() 으로 푼다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from harness.audit import AuditLog
from harness.broker.base import Broker, DuplicateOrderError
from harness.decision.policy import DecisionContext, Policy
from harness.market.bars import Bar
from harness.market.indicators import compute_indicators
from harness.models import (
    AccountState, Market, OrderRequest, OrderResult, OrderStatus, OrderType, Position, Side,
)
from harness.research.thesis import ThesisStore
from harness.risk.gate import RiskGate

Key = tuple[Market, str]


@dataclass
class OpenPosition:
    quantity: int
    avg_price: Decimal
    stop: Decimal
    take_profit: Decimal
    fx: Decimal
    last: Decimal


class TradingEngine:
    def __init__(self, *, gate: RiskGate, broker: Broker, policy: Policy, theses: ThesisStore,
                 min_bars: int, audit: AuditLog | None = None,
                 fx_rates: dict[Market, Decimal] | None = None, history: int = 120):
        self.gate, self.broker, self.policy, self.theses = gate, broker, policy, theses
        self.min_bars = min_bars
        self.audit = audit or AuditLog()
        self.fx = fx_rates or {Market.KR: Decimal(1)}
        self.cash_krw = gate.limits.initial_capital_krw
        self.positions: dict[Key, OpenPosition] = {}
        self.pending: dict[Key, OrderResult] = {}
        self._bars: dict[Key, deque[Bar]] = {}
        self._history = history

    # ------------------------------------------------------------------ 상태
    def account(self) -> AccountState:
        return AccountState(
            cash_krw=max(self.cash_krw, Decimal(0)),
            positions=tuple(Position(market=m, symbol=s, quantity=p.quantity, avg_price=p.avg_price,
                                     last_price=p.last, fx_rate_krw=p.fx)
                            for (m, s), p in self.positions.items()),
        )

    def resolve_pending(self, market: Market, symbol: str, note: str) -> None:
        """운영자가 브로커 화면/잔고로 확인한 뒤 호출. 실제 체결이 있었다면 장부도 수동 보정해야 한다."""
        res = self.pending.pop((market, symbol), None)
        self.audit.record("pending_resolved", {"note": note, "result": res},
                          market=market.value, symbol=symbol)

    # ------------------------------------------------------------------ 메인 루프
    def on_bar(self, bar: Bar, now: datetime) -> None:
        key = (bar.market, bar.symbol)
        hist = self._bars.setdefault(key, deque(maxlen=self._history))
        hist.append(bar)
        self.audit.record("bar", {"start": bar.start, "o": bar.open, "h": bar.high, "l": bar.low,
                                  "c": bar.close, "v": bar.volume},
                          market=bar.market.value, symbol=bar.symbol, ts=now)
        if key in self.positions:
            self.positions[key].last = bar.close

        if self.gate.check_halt(self.account()):
            self._exit_all("halt", now)
            return
        if key in self.pending:
            return
        if key in self.positions:
            self._manage_exit(key, bar, now)
            return
        self._maybe_enter(key, list(hist), now)

    def flatten_if_due(self, now: datetime) -> None:
        """봉이 오지 않아도 주기적으로 호출(마감 청산 보장)."""
        for key in list(self.positions):
            if key not in self.pending and self.gate.should_flatten(key[0], now):
                self._exit(key, "flatten", now)

    # ------------------------------------------------------------------ 청산
    def _manage_exit(self, key: Key, bar: Bar, now: datetime) -> None:
        p = self.positions[key]
        if self.gate.should_flatten(key[0], now):
            self._exit(key, "flatten", now)
        elif bar.low <= p.stop:
            self._exit(key, "stop", now)
        elif bar.high >= p.take_profit:
            self._exit(key, "take_profit", now)

    def _exit_all(self, reason: str, now: datetime) -> None:
        for key in list(self.positions):
            if key not in self.pending:
                self._exit(key, reason, now)

    def _exit(self, key: Key, reason: str, now: datetime) -> None:
        p = self.positions[key]
        req = OrderRequest(client_order_id=self._cid(key, "S", now), market=key[0], symbol=key[1],
                           side=Side.SELL, quantity=p.quantity, order_type=OrderType.MARKET)
        res = self._send(req, {"reason": reason}, now)
        if res.status is OrderStatus.FILLED:
            qty = res.filled_quantity
            proceeds = res.fill_price * qty * p.fx
            cost = p.avg_price * qty * p.fx
            fee = cost * Decimal(str(self.gate.limits.round_trip_cost_pct[key[0]])) / 100
            pnl = proceeds - cost - fee
            self.cash_krw += proceeds - fee
            self.gate.ledger.record_exit(self.gate.sessions[key[0]].trading_date(now), key[0], pnl)
            self.audit.record("exit", {"reason": reason, "pnl_krw": pnl, "qty": qty,
                                       "price": res.fill_price}, market=key[0].value, symbol=key[1], ts=now)
            if qty >= p.quantity:
                del self.positions[key]
            else:
                p.quantity -= qty
        elif res.status in (OrderStatus.ACCEPTED, OrderStatus.UNKNOWN):
            self.pending[key] = res

    # ------------------------------------------------------------------ 진입
    def _maybe_enter(self, key: Key, bars: list[Bar], now: datetime) -> None:
        market, symbol = key
        thesis = self.theses.active(market, symbol, now)
        if thesis is None or len(bars) < self.min_bars or self.gate.should_flatten(market, now):
            return

        ctx = DecisionContext(market=market, symbol=symbol, now=now, thesis=thesis, bars=bars,
                              indicators=compute_indicators(bars), fx_rate_krw=self.fx[market])
        decision = self.policy.decide(ctx)
        self.audit.record("decision", {"policy": self.policy.name, "reason": decision.reason,
                                       "signal": decision.signal, "detail": decision.detail},
                          market=market.value, symbol=symbol, ts=now)
        if decision.signal is None:
            return

        sig = decision.signal
        risk = self.gate.evaluate(sig, self.account(), now)
        self.audit.record("risk", {"approved": risk.approved, "quantity": risk.quantity,
                                   "reasons": risk.reasons}, market=market.value, symbol=symbol, ts=now)
        if not risk.approved:
            return

        req = OrderRequest(client_order_id=self._cid(key, "B", now), market=market, symbol=symbol,
                           side=Side.BUY, quantity=risk.quantity, order_type=OrderType.LIMIT,
                           limit_price=sig.entry_price)
        res = self._send(req, {"signal": sig}, now)
        if res.status is not OrderStatus.REJECTED:
            self.gate.ledger.record_entry(self.gate.sessions[market].trading_date(now), market)
        if res.status is OrderStatus.FILLED:
            fill = res.fill_price
            shift = fill - sig.entry_price  # 체결가 차이만큼 손절·익절도 평행 이동
            self.cash_krw -= fill * res.filled_quantity * sig.fx_rate_krw
            self.positions[key] = OpenPosition(res.filled_quantity, fill, sig.stop_price + shift,
                                               sig.take_profit_price + shift, sig.fx_rate_krw, fill)
        elif res.status in (OrderStatus.ACCEPTED, OrderStatus.UNKNOWN):
            self.pending[key] = res

    # ------------------------------------------------------------------ 공통
    def _send(self, req: OrderRequest, context: dict, now: datetime) -> OrderResult:
        try:
            res = self.broker.place_order(req)
        except DuplicateOrderError as e:
            res = OrderResult(client_order_id=req.client_order_id, status=OrderStatus.REJECTED,
                              message=str(e))
        self.audit.record("order", {"request": req, "result": res, **context},
                          market=req.market.value, symbol=req.symbol, ts=now)
        return res

    def _cid(self, key: Key, side: str, now: datetime) -> str:
        # 같은 분·같은 방향 주문은 같은 ID → 재시작 후 같은 봉을 다시 처리해도 중복 주문이 안 됨
        return f"{self.policy.name}-{key[0].value}-{key[1]}-{now:%Y%m%d%H%M}-{side}"


def restore_ledger(audit: AuditLog, gate: RiskGate) -> None:
    """감사 로그로 장부(누적·일일 손익, 거래 횟수, 연속 손절, 정지)를 복원한다."""
    led = gate.ledger
    for e in audit.events():
        if e["market"] is None:
            continue
        market = Market(e["market"])
        day = gate.sessions[market].trading_date(datetime.fromisoformat(e["ts"]))
        p = e["payload"]
        if e["kind"] == "exit":
            led.record_exit(day, market, Decimal(p["pnl_krw"]))
        elif (e["kind"] == "order" and p["request"]["side"] == Side.BUY.value
              and p["result"]["status"] != OrderStatus.REJECTED.value):
            led.record_entry(day, market)


def unreconciled_symbols(audit: AuditLog) -> list[str]:
    """체결 기준 순매수 수량이 남았거나 결과 불명 주문이 있는 종목. 있으면 시작 전 사람이 확인해야 한다."""
    net: dict[str, int] = {}
    unknown: set[str] = set()
    for e in audit.events("order"):
        res, req = e["payload"]["result"], e["payload"]["request"]
        key = f'{e["market"]}:{e["symbol"]}'
        if res["status"] == OrderStatus.FILLED.value:
            sign = 1 if req["side"] == Side.BUY.value else -1
            net[key] = net.get(key, 0) + sign * res["filled_quantity"]
        elif res["status"] in (OrderStatus.ACCEPTED.value, OrderStatus.UNKNOWN.value):
            unknown.add(key)
    for e in audit.events("pending_resolved"):
        unknown.discard(f'{e["market"]}:{e["symbol"]}')
    return sorted({k for k, q in net.items() if q != 0} | unknown)
