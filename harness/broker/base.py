from __future__ import annotations

from typing import Protocol

from harness.models import OrderRequest, OrderResult


class DuplicateOrderError(RuntimeError):
    """같은 client_order_id 로 두 번 주문하려 할 때. 중복 체결 방지용."""


class Broker(Protocol):
    def place_order(self, req: OrderRequest) -> OrderResult: ...


class Guard(Protocol):
    def claim(self, client_order_id: str) -> None: ...


class IdempotencyGuard:
    """프로세스 내 client_order_id 중복 차단. 재시작 간 보존은 harness.audit.SqliteIdempotencyGuard."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, client_order_id: str) -> None:
        if client_order_id in self._seen:
            raise DuplicateOrderError(f"이미 제출된 client_order_id: {client_order_id}")
        self._seen.add(client_order_id)
