"""봇 의견 → 논지. 결합은 코드로만 한다(LLM 심판 없음)."""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from harness.models import Market
from harness.risk.sessions import SESSIONS

from .bots import BearOpinion, BotOpinion
from .thesis import Thesis


class AggregateSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    weights: dict[str, float]
    min_score: float = Field(ge=0, le=1)
    avoid_veto_conf: float = Field(ge=0, le=1)
    bear_veto_prob: float = Field(ge=0, le=1)


def _value(o: BotOpinion) -> float:
    return {"LONG": o.confidence, "NEUTRAL": 0.0, "AVOID": -o.confidence}[o.stance]


def aggregate(market: Market, symbol: str, opinions: dict[str, BotOpinion], bear: BearOpinion,
              s: AggregateSettings, now: datetime, ttl_min: int) -> tuple[Thesis | None, str, float]:
    for name, o in opinions.items():
        if o.stance == "AVOID" and o.confidence >= s.avoid_veto_conf:
            return None, f"{name} AVOID 거부권", 0.0
    if bear.invalidation_prob >= s.bear_veto_prob:
        return None, f"반대논리 무효화 확률 {bear.invalidation_prob:.2f}", 0.0

    total_w = sum(s.weights[k] for k in opinions)
    score = sum(s.weights[k] * _value(o) for k, o in opinions.items()) / total_w
    score *= 1 - bear.invalidation_prob
    if score < s.min_score:
        return None, f"점수 {score:.2f} 미달", score

    close = SESSIONS[market].bounds(now)[1]
    until = min(now + timedelta(minutes=ttl_min), close)
    if until <= now:
        return None, "장 마감 이후", score
    reasons = [r for o in opinions.values() for r in o.reasons][:4]
    risks = bear.counterarguments[:2]
    rationale = " / ".join(reasons) + (" | 리스크: " + " / ".join(risks) if risks else "")
    thesis = Thesis(market=market, symbol=symbol, direction="LONG", rationale=rationale[:600],
                    valid_from=now, valid_until=until, source=f"grok(score={score:.2f})")
    return thesis, "논지 생성", score
