"""리스크 게이트 — 결정론적 코드만. LLM·Jev 판단은 여기서 쓰지 않는다.

진입(EntrySignal)만 심사한다. 청산은 손실을 줄이는 방향이므로 게이트를 거치지 않고
should_flatten() 과 손절/익절 감시가 처리한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR

from harness.models import AccountState, EntrySignal, Market

from .ledger import Ledger
from .limits import RiskLimits
from .sessions import SESSIONS, MarketSession


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)


class RiskGate:
    def __init__(self, limits: RiskLimits, ledger: Ledger,
                 sessions: dict[Market, MarketSession] | None = None):
        self.limits = limits
        self.ledger = ledger
        self.sessions = sessions or SESSIONS

    # ------------------------------------------------------------------ 정지 판정
    def check_halt(self, account: AccountState) -> bool:
        """총 손실 한도 도달 시 장부를 정지 상태로 만든다. 정지 중이면 True."""
        total = self.ledger.cumulative_realized_krw + account.unrealized_pnl_krw
        if total <= -self.limits.max_total_loss_krw:
            self.ledger.halt(f"총 손실 한도 도달: {total}원")
        return self.ledger.halted

    def should_flatten(self, market: Market, now: datetime) -> bool:
        """장 마감 전 전량 청산 구간이면 True. 장 밖이어도 True(보유 금지)."""
        s = self.sessions[market]
        return not s.is_open(now) or s.minutes_to_close(now) <= self.limits.flatten_before_close_min

    # ------------------------------------------------------------------ 진입 심사
    def evaluate(self, sig: EntrySignal, account: AccountState, now: datetime) -> RiskDecision:
        lim, led = self.limits, self.ledger
        session = self.sessions[sig.market]
        day = session.trading_date(now)

        def reject(reason: str) -> RiskDecision:
            return RiskDecision(False, 0, (reason,))

        if self.check_halt(account):
            return reject(f"정지 상태: {led.halt_reason}")

        if not session.is_open(now):
            return reject("정규장 시간이 아님")
        if session.minutes_since_open(now) < lim.no_entry_after_open_min:
            return reject("장 시작 직후 진입 금지 구간")
        if session.minutes_to_close(now) <= lim.no_entry_before_close_min:
            return reject("장 마감 직전 진입 금지 구간")

        # 미실현 손실까지 포함해 같은 거래일 손실을 본다(보수적).
        if led.daily_realized(day) + account.unrealized_pnl_krw <= -lim.max_daily_loss_krw:
            return reject("일일 손실 한도 도달")
        if led.consecutive_losses(day, sig.market) >= lim.max_consecutive_losses:
            return reject(f"{sig.market} 연속 손절 한도 도달")
        if led.trades(day, sig.market) >= lim.max_trades_per_day[sig.market]:
            return reject(f"{sig.market} 일일 거래 횟수 한도 도달")

        if sig.confidence < lim.min_confidence:
            return reject(f"신뢰도 미달: {sig.confidence:.2f} < {lim.min_confidence:.2f}")

        edge_pct = float((sig.take_profit_price - sig.entry_price) / sig.entry_price * 100)
        need_pct = lim.round_trip_cost_pct[sig.market] * lim.min_edge_cost_multiple
        if edge_pct < need_pct:
            return reject(f"기대수익 {edge_pct:.2f}% < 필요 {need_pct:.2f}% (비용 대비)")

        if account.holds(sig.market, sig.symbol):
            return reject("이미 보유 중인 종목")
        if len(account.positions) >= lim.max_open_positions:
            return reject("동시 보유 종목 한도 도달")

        return self._size(sig, account)

    def _size(self, sig: EntrySignal, account: AccountState) -> RiskDecision:
        lim = self.limits
        unit_cost_krw = sig.entry_price * sig.fx_rate_krw
        unit_risk_krw = (sig.entry_price - sig.stop_price) * sig.fx_rate_krw

        # 남은 일일·총 손실 여유보다 큰 손실을 1회 거래에 걸지 않는다.
        budget = min(lim.max_loss_per_trade_krw,
                     lim.max_total_loss_krw + self.ledger.cumulative_realized_krw
                     + account.unrealized_pnl_krw)

        caps = {
            "1회 손실 한도": budget / unit_risk_krw,
            "종목당 최대 금액": lim.max_position_krw / unit_cost_krw,
            "가용 현금": account.cash_krw / unit_cost_krw,
        }
        binding, raw_qty = min(caps.items(), key=lambda kv: kv[1])
        qty = int(raw_qty.to_integral_value(rounding=ROUND_FLOOR)) if raw_qty > 0 else 0
        if qty < 1:
            return RiskDecision(False, 0, (f"수량 0주 ({binding} 제약)",))
        return RiskDecision(True, qty, (f"수량 {qty}주 ({binding} 제약)",))
