"""손익·거래 횟수·연속 손절·정지 상태 장부.

거래일 키는 시장 현지 날짜. 일일 손실 한도는 같은 날짜의 국내+미국 실현손익 합산에 적용한다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from harness.models import Market


@dataclass
class Ledger:
    cumulative_realized_krw: Decimal = Decimal(0)
    halted: bool = False
    halt_reason: str | None = None
    _daily_realized: dict[date, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    _trades: dict[tuple[date, Market], int] = field(default_factory=lambda: defaultdict(int))
    _consec_losses: dict[tuple[date, Market], int] = field(default_factory=lambda: defaultdict(int))

    def daily_realized(self, day: date) -> Decimal:
        return self._daily_realized[day]

    def trades(self, day: date, market: Market) -> int:
        return self._trades[(day, market)]

    def consecutive_losses(self, day: date, market: Market) -> int:
        return self._consec_losses[(day, market)]

    def record_entry(self, day: date, market: Market) -> None:
        """진입 주문이 전송된 시점에 호출(체결 여부와 무관하게 횟수에 포함)."""
        self._trades[(day, market)] += 1

    def record_exit(self, day: date, market: Market, realized_pnl_krw: Decimal) -> None:
        self.cumulative_realized_krw += realized_pnl_krw
        self._daily_realized[day] += realized_pnl_krw
        key = (day, market)
        self._consec_losses[key] = self._consec_losses[key] + 1 if realized_pnl_krw < 0 else 0

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted, self.halt_reason = True, reason

    def resume(self, operator_note: str) -> None:
        """수동 재개 전용. 자동 경로에서 호출하지 않는다."""
        if not operator_note.strip():
            raise ValueError("재개 사유를 남겨야 합니다")
        self.halted, self.halt_reason = False, None
