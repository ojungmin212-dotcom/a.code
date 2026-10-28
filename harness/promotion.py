"""승격 심사: 모의 운영 감사 로그가 기준을 넘어야 실거래를 시작할 수 있다(코드로 강제)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from harness.audit import AuditLog
from harness.engine import unreconciled_symbols
from harness.evaluation import jev_calibration, trade_stats


class PromotionCriteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_weeks: float = Field(ge=0)
    min_trades: int = Field(ge=0)
    require_positive_pnl: bool
    require_beats_baseline: bool
    min_jev_skill: float
    min_calibration_samples: int = Field(ge=0)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PromotionCriteria:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))


@dataclass
class PromotionResult:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    def render(self) -> str:
        lines = [f"{'✅' if ok else '❌'} {name}: {detail}" for name, ok, detail in self.checks]
        lines.append("→ 승격 " + ("가능" if self.passed else "불가"))
        return "\n".join(lines)


def evaluate(jev: AuditLog, baseline: AuditLog, c: PromotionCriteria, now: datetime,
             fields_ready: bool) -> PromotionResult:
    r = PromotionResult()
    r.add("브로커·시세 필드 확정", fields_ready, "config/broker_fields.yaml · feed.yaml")

    exits = jev.events("exit")
    if exits:
        weeks = (now - datetime.fromisoformat(exits[0]["ts"])) / timedelta(weeks=1)
        r.add("모의 운영 기간", weeks >= c.min_weeks, f"{weeks:.1f}주 / 기준 {c.min_weeks}주")
    else:
        r.add("모의 운영 기간", False, "체결된 모의 거래 없음")

    js, bs = trade_stats(jev), trade_stats(baseline)
    r.add("거래 횟수", js.trades >= c.min_trades, f"{js.trades}회 / 기준 {c.min_trades}회")
    if c.require_positive_pnl:
        r.add("비용 반영 손익 > 0", js.total_krw > 0, f"{js.total_krw:,.0f}원")
    if c.require_beats_baseline:
        r.add("비교군 대비 우위", bs.trades > 0 and js.total_krw > bs.total_krw,
              f"Jev {js.total_krw:,.0f}원 vs 비교군 {bs.total_krw:,.0f}원 ({bs.trades}회)")

    cal = jev_calibration(jev)
    ok = (cal.n >= c.min_calibration_samples and cal.skill is not None
          and cal.skill > c.min_jev_skill)
    skill = f"{cal.skill:+.3f}" if cal.skill is not None else "-"
    r.add("Jev 확률 보정", ok, f"skill {skill}, 표본 {cal.n} / 기준 {c.min_calibration_samples}")

    left = unreconciled_symbols(jev)
    r.add("미해소 주문·미청산 없음", not left, ", ".join(left) or "없음")
    return r
