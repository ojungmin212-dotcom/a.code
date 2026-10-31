"""섀도 판단(F4): 주문과 무관하게 매 1분봉마다 Jev 판단을 기록해 보정 표본을 늘린다.

- 실제 판단(decision)이 일어난 봉은 건너뛴다(중복 표본 방지).
- 논지가 없는 종목은 '논지 없음' 문구로 묻고 has_thesis=False 로 표시한다(리포트에서 분리).
- Grok 비용 없음. Jev 입력 토큰 비용만 들며, 월 상한·일 호출 상한을 넘으면 섀도만 멈춘다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from harness.audit import AuditLog
from harness.research.thesis import Thesis

from .jev import JevError
from .policy import QUESTIONS, DecisionContext, JevPolicy, ShadowSettings, build_state

KST = ZoneInfo("Asia/Seoul")
NO_THESIS = "No research thesis today. Judge the intraday price action only."


class ShadowObserver:
    def __init__(self, policy: JevPolicy, audit: AuditLog, s: ShadowSettings):
        self.policy, self.audit, self.s = policy, audit, s
        self._day: str | None = None
        self._calls_today = 0
        self._month: str | None = None
        self._spent_month = 0.0
        self.stopped_reason: str | None = None

    def _roll(self, now: datetime) -> None:
        local = now.astimezone(KST)
        day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
        if month != self._month:
            self._month = month
            self._spent_month = sum(e["payload"]["krw"] for e in self.audit.events("jev_cost")
                                    if datetime.fromisoformat(e["ts"]).astimezone(KST).strftime("%Y-%m") == month)
        if day != self._day:
            self._day = day
            self._calls_today = sum(1 for e in self.audit.events("shadow")
                                    if datetime.fromisoformat(e["ts"]).astimezone(KST).strftime("%Y-%m-%d") == day)
            self.stopped_reason = None

    def observe(self, ctx: DecisionContext, has_thesis: bool, now: datetime) -> None:
        self._roll(now)
        if self._calls_today >= self.s.max_calls_per_day:
            self._stop(f"일 호출 상한 {self.s.max_calls_per_day}회", now)
            return
        if self._spent_month >= self.s.monthly_budget_krw:
            self._stop(f"월 예산 {self.s.monthly_budget_krw:,.0f}원", now)
            return
        state = build_state(ctx)
        try:
            data = self.policy.client.ask(state, QUESTIONS)
        except JevError as e:
            self.audit.record("shadow_error", {"error": str(e)[:300]},
                              market=ctx.market.value, symbol=ctx.symbol, ts=now)
            return
        self._calls_today += 1
        tokens = (data.get("usage") or {}).get("input_tokens") or 0
        krw = tokens / 1_000_000 * self.s.usd_per_m_input_tokens * self.s.usd_krw
        self._spent_month += krw
        self.audit.record("jev_cost", {"krw": krw, "input_tokens": tokens, "source": "shadow"}, ts=now)
        self.audit.record("shadow", {"policy": "jev", "has_thesis": has_thesis,
                                     "detail": {"state": state, "model": data.get("model"),
                                                "answers": data["answers"]}},
                          market=ctx.market.value, symbol=ctx.symbol, ts=now)

    def _stop(self, reason: str, now: datetime) -> None:
        if self.stopped_reason is None:
            self.stopped_reason = reason
            self.audit.record("shadow_stopped", {"reason": reason}, ts=now)

    @staticmethod
    def placeholder_thesis(ctx_market, symbol: str, now: datetime) -> Thesis:
        return Thesis(market=ctx_market, symbol=symbol, direction="LONG", rationale=NO_THESIS,
                      valid_from=now - timedelta(minutes=1), valid_until=now + timedelta(minutes=1),
                      source="shadow")
