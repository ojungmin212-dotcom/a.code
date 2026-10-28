"""메모리 내 모의 브로커. 테스트와 오프라인 리허설용 — 실제 체결 품질(슬리피지)은 반영하지 않는다."""
from __future__ import annotations

from decimal import Decimal

from harness.models import (
    AccountState, Market, OrderRequest, OrderResult, OrderStatus, OrderType, Position, Side,
)

from .base import IdempotencyGuard


class PaperBroker:
    def __init__(self, cash_krw: Decimal, fee_pct: dict[Market, Decimal] | None = None):
        self.cash_krw = cash_krw
        self.fee_pct = fee_pct or {Market.KR: Decimal("0.015"), Market.US: Decimal("0.09")}
        self._prices: dict[tuple[Market, str], Decimal] = {}
        self._fx: dict[Market, Decimal] = {Market.KR: Decimal(1), Market.US: Decimal(1400)}
        self._pos: dict[tuple[Market, str], tuple[int, Decimal]] = {}
        self._guard = IdempotencyGuard()

    def set_price(self, market: Market, symbol: str, price: Decimal) -> None:
        self._prices[(market, symbol)] = price

    def set_fx(self, market: Market, rate: Decimal) -> None:
        self._fx[market] = rate

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._guard.claim(req.client_order_id)
        key = (req.market, req.symbol)
        last = self._prices.get(key)
        if last is None:
            return self._reject(req, "시세 없음")

        if req.order_type is OrderType.LIMIT:
            crosses = last <= req.limit_price if req.side is Side.BUY else last >= req.limit_price
            if not crosses:
                return self._reject(req, "지정가 미도달(모의 브로커는 미체결 주문을 보관하지 않음)")
        price = last
        fx = self._fx[req.market]
        gross = price * req.quantity * fx
        fee = gross * self.fee_pct[req.market] / 100
        qty_held, avg = self._pos.get(key, (0, Decimal(0)))

        if req.side is Side.BUY:
            if gross + fee > self.cash_krw:
                return self._reject(req, "현금 부족")
            self.cash_krw -= gross + fee
            new_qty = qty_held + req.quantity
            self._pos[key] = (new_qty, (avg * qty_held + price * req.quantity) / new_qty)
        else:
            if req.quantity > qty_held:
                return self._reject(req, "보유 수량 부족(공매도 불가)")
            self.cash_krw += gross - fee
            left = qty_held - req.quantity
            if left:
                self._pos[key] = (left, avg)
            else:
                del self._pos[key]

        return OrderResult(client_order_id=req.client_order_id, status=OrderStatus.FILLED,
                           broker_order_id=f"paper-{req.client_order_id}",
                           filled_quantity=req.quantity, fill_price=price)

    def account_state(self) -> AccountState:
        positions = tuple(
            Position(market=m, symbol=s, quantity=q, avg_price=avg,
                     last_price=self._prices[(m, s)], fx_rate_krw=self._fx[m])
            for (m, s), (q, avg) in self._pos.items()
        )
        return AccountState(cash_krw=self.cash_krw, positions=positions)

    @staticmethod
    def _reject(req: OrderRequest, msg: str) -> OrderResult:
        return OrderResult(client_order_id=req.client_order_id, status=OrderStatus.REJECTED, message=msg)
