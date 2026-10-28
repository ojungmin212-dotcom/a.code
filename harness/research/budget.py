"""리서치 API 월 예산 통제. 비용은 감사 로그(research_cost)에 남겨 재시작에도 누적된다."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.audit import AuditLog

KST = ZoneInfo("Asia/Seoul")


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, audit: AuditLog, monthly_budget_krw: float, usd_krw: float):
        self.audit, self.budget, self.usd_krw = audit, monthly_budget_krw, usd_krw

    @staticmethod
    def _month(ts: datetime) -> str:
        return ts.astimezone(KST).strftime("%Y-%m")

    def spent_krw(self, now: datetime) -> float:
        m = self._month(now)
        return sum(e["payload"]["krw"] for e in self.audit.events("research_cost")
                   if self._month(datetime.fromisoformat(e["ts"])) == m)

    def reserve(self, est_usd: float, now: datetime) -> None:
        """호출 전 확인: 추정 비용을 더해도 예산 안이어야 한다."""
        if self.spent_krw(now) + est_usd * self.usd_krw > self.budget:
            raise BudgetExceeded(f"월 예산 {self.budget:,.0f}원 초과 예상 (사용 {self.spent_krw(now):,.0f}원)")

    def charge(self, bot: str, est_usd: float, usage: dict, token_price: dict[str, float],
               now: datetime, symbol: str | None = None) -> float:
        """실측 토큰 비용과 추정치 중 큰 값을 기록(검색 도구 과금이 usage 에 안 잡힐 수 있어 보수적으로)."""
        tok = ((usage.get("input_tokens") or 0) * token_price["input"]
               + (usage.get("output_tokens") or 0) * token_price["output"]) / 1_000_000
        usd = max(est_usd, tok)
        krw = usd * self.usd_krw
        self.audit.record("research_cost", {"bot": bot, "usd": usd, "krw": krw, "usage": usage},
                          symbol=symbol, ts=now)
        return krw
