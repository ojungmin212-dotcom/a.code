"""빠른 루프 엔진: 1분봉 마감마다 청산 감시 → 진입 판단 → 리스크 게이트 → 주문.

포지션·현금은 엔진 내부 장부로 관리한다(체결 결과 FILLED 만 반영).
브로커가 ACCEPTED·UNKNOWN 을 돌려주면 그 종목을 '대조 필요(pending)'로 묶고 추가 주문을 하지 않는다.
대조는 reconcile()(브로커 잔고와 비교) 또는 운영자 확인 후 resolve_pending() 으로 푼다.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from harness.audit import AuditLog
from harness.broker.base import Broker, DuplicateOrderError
from harness.decision.policy import DecisionContext, Policy
from harness.decision.shadow import ShadowObserver
from harness.market.bars import Bar
from harness.market.indicators import compute_indicators
from harness.models import (
    AccountState, EntrySignal, Market, OrderRequest, OrderResult, OrderStatus, OrderType, Position, Side,
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


@dataclass
class PendingOrder:
    result: OrderResult
    side: Side
    qty_before: int          # 주문 직전 엔진 장부상 보유 수량
    qty_ordered: int
    sent_at: datetime
    reason: str
    signal: EntrySignal | None = None


class TradingEngine:
    def __init__(self, *, gate: RiskGate, broker: Broker, policy: Policy, theses: ThesisStore,
                 min_bars: int, audit: AuditLog | None = None,
                 fx_rates: dict[Market, Decimal] | None = None, history: int = 120,
                 entry_order_type: OrderType = OrderType.LIMIT,
                 notify: Callable[[str], None] | None = None,
                 shadow: ShadowObserver | None = None):
        self.gate, self.broker, self.policy, self.theses = gate, broker, policy, theses
        self.min_bars = min_bars
        self.audit = audit or AuditLog()
        self.fx = fx_rates or {Market.KR: Decimal(1)}
        self.cash_krw = gate.limits.initial_capital_krw
        self.positions: dict[Key, OpenPosition] = {}
        self.pending: dict[Key, PendingOrder] = {}
        self.entry_order_type = entry_order_type
        self._last_exit_try: dict[Key, datetime] = {}
        self.shadow = shadow
        self._decided = False
        self.notify = notify or (lambda msg: None)
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
        po = self.pending.pop((market, symbol), None)
        self.audit.record("pending_resolved", {"note": note, "result": po.result if po else None},
                          market=market.value, symbol=symbol)

    def set_fx(self, rates: dict[Market, Decimal], now: datetime) -> None:
        """환율 갱신. 빠진 시장은 신규 진입 중단(보유 포지션은 마지막 환율로 관리)."""
        for m in set(self.fx) - set(rates) - {Market.KR}:
            self.audit.record("fx_unavailable", {"market": m.value}, ts=now)
        self.fx = {Market.KR: Decimal(1), **rates}

    # ------------------------------------------------------------------ 메인 루프
    def on_bar(self, bar: Bar, now: datetime) -> None:
        self._decided = False
        self._step(bar, now)
        if self.shadow is not None and not self._decided:
            self._observe_shadow((bar.market, bar.symbol), now)

    def _observe_shadow(self, key: Key, now: datetime) -> None:
        bars = list(self._bars[key])
        if len(bars) < self.min_bars:
            return
        market, symbol = key
        thesis = self.theses.active(market, symbol, now)
        ctx = DecisionContext(market=market, symbol=symbol, now=now,
                              thesis=thesis or ShadowObserver.placeholder_thesis(market, symbol, now),
                              bars=bars, indicators=compute_indicators(bars),
                              fx_rate_krw=self.fx.get(market, Decimal(1)))
        self.shadow.observe(ctx, thesis is not None, now)

    def _step(self, bar: Bar, now: datetime) -> None:
        key = (bar.market, bar.symbol)
        hist = self._bars.setdefault(key, deque(maxlen=self._history))
        hist.append(bar)
        self.audit.record("bar", {"start": bar.start, "o": bar.open, "h": bar.high, "l": bar.low,
                                  "c": bar.close, "v": bar.volume},
                          market=bar.market.value, symbol=bar.symbol, ts=now)
        if key in self.positions:
            self.positions[key].last = bar.close

        was_halted = self.gate.ledger.halted
        if self.gate.check_halt(self.account()):
            if not was_halted:
                self._halted_now(now)
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
        minute = now.replace(second=0, microsecond=0)
        if self._last_exit_try.get(key) == minute:
            return  # 같은 분에 이미 시도함(루프가 초 단위로 돌아도 주문·로그 폭주 방지)
        self._last_exit_try[key] = minute
        p = self.positions[key]
        req = OrderRequest(client_order_id=self._cid(key, "S", now), market=key[0], symbol=key[1],
                           side=Side.SELL, quantity=p.quantity, order_type=OrderType.MARKET)
        res = self._send(req, {"reason": reason}, now)
        if res.status is OrderStatus.FILLED:
            self._book_exit(key, res.filled_quantity, res.fill_price, reason, now)
        elif res.status in (OrderStatus.ACCEPTED, OrderStatus.UNKNOWN):
            self.pending[key] = PendingOrder(res, Side.SELL, p.quantity, p.quantity, now, reason)

    def _book_exit(self, key: Key, qty: int, price: Decimal, reason: str, now: datetime,
                   estimated: bool = False) -> None:
        p = self.positions[key]
        exit_fx = self.fx.get(key[0], p.fx)  # 미국: 매도 시점 환율로 환산(환차손익 포함)
        proceeds = price * qty * exit_fx
        cost = p.avg_price * qty * p.fx
        fee = cost * Decimal(str(self.gate.limits.round_trip_cost_pct[key[0]])) / 100
        pnl = proceeds - cost - fee
        self.cash_krw += proceeds - fee
        self.gate.ledger.record_exit(self.gate.sessions[key[0]].trading_date(now), key[0], pnl)
        self.audit.record("exit", {"reason": reason, "pnl_krw": pnl, "qty": qty, "price": price,
                                   "estimated_price": estimated},
                          market=key[0].value, symbol=key[1], ts=now)
        self.notify(f"청산 {key[1]} {qty}주 @{price}{'(추정)' if estimated else ''} "
                    f"손익 {pnl:,.0f}원 ({reason})")
        if qty >= p.quantity:
            del self.positions[key]
        else:
            p.quantity -= qty

    # ------------------------------------------------------------------ 진입
    def _maybe_enter(self, key: Key, bars: list[Bar], now: datetime) -> None:
        market, symbol = key
        thesis = self.theses.active(market, symbol, now)
        if thesis is None or len(bars) < self.min_bars or self.gate.should_flatten(market, now):
            return
        if market not in self.fx:
            return  # 환율 없음/낡음 → 이 시장 신규 진입 금지

        ctx = DecisionContext(market=market, symbol=symbol, now=now, thesis=thesis, bars=bars,
                              indicators=compute_indicators(bars), fx_rate_krw=self.fx[market])
        decision = self.policy.decide(ctx)
        self._decided = True
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
                           side=Side.BUY, quantity=risk.quantity, order_type=self.entry_order_type,
                           limit_price=sig.entry_price if self.entry_order_type is OrderType.LIMIT else None)
        res = self._send(req, {"signal": sig}, now)
        if res.status is not OrderStatus.REJECTED:
            self.gate.ledger.record_entry(self.gate.sessions[market].trading_date(now), market)
        if res.status is OrderStatus.FILLED:
            self._book_entry(key, sig, res.filled_quantity, res.fill_price, now)
        elif res.status in (OrderStatus.ACCEPTED, OrderStatus.UNKNOWN):
            self.pending[key] = PendingOrder(res, Side.BUY, 0, risk.quantity, now, "entry", sig)

    def _book_entry(self, key: Key, sig: EntrySignal, qty: int, fill: Decimal, now: datetime,
                    estimated: bool = False) -> None:
        shift = fill - sig.entry_price  # 체결가 차이만큼 손절·익절도 평행 이동
        self.cash_krw -= fill * qty * sig.fx_rate_krw
        self.positions[key] = OpenPosition(qty, fill, sig.stop_price + shift,
                                           sig.take_profit_price + shift, sig.fx_rate_krw, fill)
        self.audit.record("fill", {"side": Side.BUY.value, "qty": qty, "price": fill,
                                   "signal_price": sig.entry_price, "estimated_price": estimated},
                          market=key[0].value, symbol=key[1], ts=now)
        self.notify(f"진입 {key[1]} {qty}주 @{fill}")

    # ------------------------------------------------------------------ 잔고 대조
    def reconcile(self, holdings: dict[Key, tuple[int, Decimal]], now: datetime,
                  scope: set[Key], timeout_sec: float = 60) -> None:
        """브로커 잔고(종목 → (수량, 평균단가))로 결과불명 주문을 확정하고 장부 불일치를 잡는다.

        scope: 하네스가 매매하는 종목만. 계좌의 다른 보유 종목은 불일치로 보지 않는다.
        """
        for key, po in list(self.pending.items()):
            held, avg = holdings.get(key, (0, Decimal(0)))
            timed_out = now - po.sent_at >= timedelta(seconds=timeout_sec)
            if po.side is Side.BUY:
                filled = held - po.qty_before
                if filled >= po.qty_ordered or (timed_out and filled > 0):
                    self.pending.pop(key)
                    self._book_entry(key, po.signal, filled, avg, now)
                elif timed_out:
                    self.pending.pop(key)
                    self.audit.record("pending_resolved", {"note": "시간 초과 · 잔고 변화 없음 → 미체결"},
                                      market=key[0].value, symbol=key[1], ts=now)
            else:
                sold = po.qty_before - held
                if sold > 0 and (held == 0 or timed_out):
                    self.pending.pop(key)
                    # 잔고에는 체결가가 없다 → 직전 시세로 추정(보수적 판단은 리포트에서 별도 확인)
                    self._book_exit(key, sold, self.positions[key].last, po.reason, now, estimated=True)
                elif timed_out:
                    # 현금 계좌는 보유 수량 이상 매도가 불가능하므로 재주문해도 중복 매도 위험이 없다
                    self.pending.pop(key)
                    self.notify(f"⚠️ {key[1]} 매도 미확인 — 다음 봉에서 재주문")
                    self.audit.record("pending_resolved", {"note": "매도 시간 초과 → 재주문 대상"},
                                      market=key[0].value, symbol=key[1], ts=now)

        expected = {k: p.quantity for k, p in self.positions.items()}
        for key in (set(expected) | set(holdings)) & (scope | set(expected)):
            if key in self.pending:
                continue
            if expected.get(key, 0) != holdings.get(key, (0, Decimal(0)))[0]:
                reason = (f"장부 불일치 {key[1]}: 엔진 {expected.get(key, 0)}주 / "
                          f"브로커 {holdings.get(key, (0, 0))[0]}주")
                if not self.gate.ledger.halted:
                    self.gate.ledger.halt(reason)
                    self._halted_now(now)

    # ------------------------------------------------------------------ 공통
    def _halted_now(self, now: datetime) -> None:
        reason = self.gate.ledger.halt_reason
        self.audit.record("halt", {"reason": reason}, ts=now)
        self.notify(f"🛑 정지: {reason} — 신규 진입 중단, 재개는 scripts/resume.py 로 수동")

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
    halt_reason = None
    for e in audit.events():
        if e["kind"] == "halt":
            halt_reason = e["payload"]["reason"]
        elif e["kind"] == "resume":
            halt_reason = None
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
    if halt_reason:
        led.halt(halt_reason)


def unreconciled_symbols(audit: AuditLog) -> list[str]:
    """장부상 보유 수량이 남았거나 결과불명 주문이 해소되지 않은 종목. 있으면 시작 전 사람이 확인해야 한다."""
    net: dict[str, int] = {}
    unknown: set[str] = set()
    for e in audit.events():
        key = f'{e["market"]}:{e["symbol"]}'
        kind, p = e["kind"], e["payload"]
        if kind == "fill":
            net[key] = net.get(key, 0) + int(p["qty"])
            unknown.discard(key)
        elif kind == "exit":
            net[key] = net.get(key, 0) - int(p["qty"])
            unknown.discard(key)
        elif kind == "pending_resolved":
            unknown.discard(key)
        elif kind == "order" and p["result"]["status"] in (OrderStatus.ACCEPTED.value,
                                                            OrderStatus.UNKNOWN.value):
            unknown.add(key)
    return sorted({k for k, q in net.items() if q != 0} | unknown)
