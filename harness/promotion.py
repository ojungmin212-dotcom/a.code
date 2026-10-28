"""승격 심사: 모의 운영 감사 로그가 기준을 넘어야 실거래를 시작할 수 있다(코드로 강제)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from harness.audit import AuditLog
from harness.engine import unreconciled_symbols
from harness.evaluation import jev_calibration, slippage_bps, trade_stats


class P4Criteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_live_weeks: float = Field(ge=0)
    min_live_trades: int = Field(ge=0)
    max_median_slippage_bps: float
    allow_halts: int = Field(ge=0)
    min_us_mock_weeks: float = Field(ge=0)
    min_us_mock_trades: int = Field(ge=0)
    require_us_mock_positive_pnl: bool


class PromotionCriteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_weeks: float = Field(ge=0)
    min_trades: int = Field(ge=0)
    require_positive_pnl: bool
    require_beats_baseline: bool
    min_jev_skill: float
    min_calibration_samples: int = Field(ge=0)
    p4: P4Criteria

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


def _weeks_since_first_exit(a: AuditLog, now: datetime, market: str | None = None) -> float | None:
    exits = [e for e in a.events("exit") if market is None or e["market"] == market]
    return (now - datetime.fromisoformat(exits[0]["ts"])) / timedelta(weeks=1) if exits else None


def evaluate_p4(live: AuditLog, mock: AuditLog, c: P4Criteria, now: datetime,
                fields_ready: bool) -> PromotionResult:
    """live: 국내 실거래(P3) 감사 로그, mock: 미국을 포함해 돌린 모의 운영 감사 로그."""
    r = PromotionResult()
    r.add("국내·미국 필드 확정", fields_ready, "broker_fields.yaml(us 포함) · feed.yaml(us_ 포함)")

    w = _weeks_since_first_exit(live, now, "KR")
    r.add("국내 실거래 기간", w is not None and w >= c.min_live_weeks,
          f"{w:.1f}주" if w is not None else "실거래 체결 없음")
    ls = trade_stats(live, "KR")
    r.add("국내 실거래 횟수", ls.trades >= c.min_live_trades, f"{ls.trades}회 / 기준 {c.min_live_trades}회")

    slip = sorted(slippage_bps(live))
    med = slip[len(slip) // 2] if slip else None
    r.add("체결 슬리피지 중앙값", med is not None and med <= c.max_median_slippage_bps,
          f"{med:.1f}bp / 기준 {c.max_median_slippage_bps}bp" if med is not None else "체결 없음")

    halts = live.events("halt")
    r.add("실거래 정지 이력", len(halts) <= c.allow_halts,
          f"{len(halts)}회" + (f" (최근: {halts[-1]['payload']['reason']})" if halts else ""))

    uw = _weeks_since_first_exit(mock, now, "US")
    r.add("미국 모의 기간", uw is not None and uw >= c.min_us_mock_weeks,
          f"{uw:.1f}주" if uw is not None else "미국 모의 체결 없음")
    us = trade_stats(mock, "US")
    r.add("미국 모의 횟수", us.trades >= c.min_us_mock_trades, f"{us.trades}회 / 기준 {c.min_us_mock_trades}회")
    if c.require_us_mock_positive_pnl:
        r.add("미국 모의 손익 > 0", us.total_krw > 0, f"{us.total_krw:,.0f}원")

    left = unreconciled_symbols(live)
    r.add("실거래 미해소 없음", not left, ", ".join(left) or "없음")
    return r
