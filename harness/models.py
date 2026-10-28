"""하네스 전 계층이 공유하는 데이터 모델.

금액은 모두 Decimal. 가격은 각 시장의 현지 통화, 원화 환산은 fx_rate_krw 로 한다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Market(StrEnum):
    KR = "KR"
    US = "US"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    DRY_RUN = "DRY_RUN"      # 전송하지 않음
    ACCEPTED = "ACCEPTED"    # 브로커 주문번호 확인됨
    FILLED = "FILLED"        # (모의 브로커) 즉시 체결
    REJECTED = "REJECTED"    # 서버에 도달하지 않았거나 명확히 거부됨
    UNKNOWN = "UNKNOWN"      # 전송됐을 수 있으나 결과 불명 → 재시도 금지, 잔고 대조 필요


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EntrySignal(_Frozen):
    """의사결정 계층(Jev)이 낸 신규 진입 제안. 롱 전용(현금 계좌, 공매도 없음)."""

    market: Market
    symbol: str = Field(min_length=1)
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    take_profit_price: Decimal = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    fx_rate_krw: Decimal = Field(default=Decimal(1), gt=0)  # KR=1, US=USD/KRW
    created_at: datetime

    @model_validator(mode="after")
    def _check_prices(self) -> EntrySignal:
        if not self.stop_price < self.entry_price < self.take_profit_price:
            raise ValueError("롱 진입은 stop < entry < take_profit 이어야 합니다")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 은 timezone-aware 여야 합니다")
        return self


class OrderRequest(_Frozen):
    client_order_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(min_length=1)
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None

    @model_validator(mode="after")
    def _check_price(self) -> OrderRequest:
        if self.order_type is OrderType.LIMIT and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("지정가 주문에는 양수 limit_price 가 필요합니다")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("시장가 주문에는 limit_price 를 넣지 않습니다")
        return self


class OrderResult(_Frozen):
    client_order_id: str
    status: OrderStatus
    broker_order_id: str | None = None
    filled_quantity: int = 0
    fill_price: Decimal | None = None
    message: str | None = None   # 브로커 rsp_msg 원문 등
    raw: dict | None = None


class Position(_Frozen):
    market: Market
    symbol: str
    quantity: int = Field(gt=0)
    avg_price: Decimal = Field(gt=0)
    last_price: Decimal = Field(gt=0)
    fx_rate_krw: Decimal = Field(default=Decimal(1), gt=0)

    @property
    def market_value_krw(self) -> Decimal:
        return self.last_price * self.quantity * self.fx_rate_krw

    @property
    def unrealized_pnl_krw(self) -> Decimal:
        return (self.last_price - self.avg_price) * self.quantity * self.fx_rate_krw


class AccountState(_Frozen):
    cash_krw: Decimal = Field(ge=0)
    positions: tuple[Position, ...] = ()

    @property
    def unrealized_pnl_krw(self) -> Decimal:
        return sum((p.unrealized_pnl_krw for p in self.positions), Decimal(0))

    def holds(self, market: Market, symbol: str) -> bool:
        return any(p.market == market and p.symbol == symbol for p in self.positions)
