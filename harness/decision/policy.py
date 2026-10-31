"""진입 정책. 같은 인터페이스로 Jev 정책과 비교군(규칙 기반)을 나란히 돌린다."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from harness.market.bars import Bar
from harness.models import EntrySignal, Market
from harness.research.thesis import Thesis

from .jev import JevClient, JevError


class JevSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str
    timeout_sec: float = Field(gt=0)
    min_thesis_alignment: float = Field(ge=0, le=1)
    min_momentum_score: float = Field(ge=0, le=4)
    max_volatile_prob: float = Field(ge=0, le=1)


class BaselineSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_volume_ratio: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)


class ShadowSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool
    extra_symbols: dict[str, list[str]] = Field(default_factory=dict)  # 시장 → 섀도 전용 종목(매매 안 함)
    max_calls_per_day: int = Field(ge=0)
    monthly_budget_krw: float = Field(ge=0)
    usd_krw: float = Field(gt=0)
    usd_per_m_input_tokens: float = Field(gt=0)


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entry_order: str = Field(default="market", pattern="^(market|limit)$")
    pending_timeout_sec: float = Field(default=60, gt=0)
    fill_notices: bool = True


class OperationsSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    heartbeat_min: int = Field(default=60, ge=0)
    record_ticks: bool = True


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    min_bars: int = Field(ge=15)
    stop_atr_mult: float = Field(gt=0)
    reward_risk: float = Field(gt=0)
    jev: JevSettings
    baseline: BaselineSettings
    shadow: ShadowSettings | None = None
    paper_slippage_bps: float = Field(default=0, ge=0)
    execution: ExecutionSettings = ExecutionSettings()
    operations: OperationsSettings = OperationsSettings()

    @classmethod
    def from_yaml(cls, path: str | Path) -> StrategyConfig:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class DecisionContext:
    market: Market
    symbol: str
    now: datetime
    thesis: Thesis
    bars: Sequence[Bar]
    indicators: dict[str, float]
    fx_rate_krw: Decimal = Decimal(1)


@dataclass
class Decision:
    signal: EntrySignal | None
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)  # 감사 로그용(질문·답 원본 등)


class Policy(Protocol):
    name: str

    def decide(self, ctx: DecisionContext) -> Decision: ...


def _krx_tick(price: Decimal) -> Decimal:
    """KRX 호가단위(2023-01 개편 기준)."""
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                        (200000, 100), (500000, 500)):
        if price < limit:
            return Decimal(tick)
    return Decimal(1000)


def _round_to_tick(price: Decimal, market: Market, down: bool) -> Decimal:
    if market is Market.KR:
        tick = _krx_tick(price)
        q = (price / tick).to_integral_value(rounding="ROUND_FLOOR" if down else "ROUND_CEILING")
        return q * tick
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_signal(ctx: DecisionContext, cfg: StrategyConfig, confidence: float) -> EntrySignal | None:
    entry = ctx.bars[-1].close
    risk = Decimal(str(ctx.indicators["atr14"] * cfg.stop_atr_mult))
    if risk <= 0:
        return None
    stop = _round_to_tick(entry - risk, ctx.market, down=True)
    tp = _round_to_tick(entry + (entry - stop) * Decimal(str(cfg.reward_risk)), ctx.market, down=False)
    if not (0 < stop < entry < tp):
        return None
    return EntrySignal(market=ctx.market, symbol=ctx.symbol, entry_price=entry, stop_price=stop,
                       take_profit_price=tp, confidence=round(confidence, 4),
                       fx_rate_krw=ctx.fx_rate_krw, created_at=ctx.now)


# ---------------------------------------------------------------- 비교군
class BaselinePolicy:
    """AI 없이 규칙만: MA20 위 + 15분 수익 양수 + 거래량 증가."""

    name = "baseline"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def decide(self, ctx: DecisionContext) -> Decision:
        i, b = ctx.indicators, self.cfg.baseline
        ok = i["dist_ma20_pct"] > 0 and i["ret_15m_pct"] > 0 and i["volume_ratio"] >= b.min_volume_ratio
        if not ok:
            return Decision(None, "규칙 미충족", {"indicators": i})
        sig = build_signal(ctx, self.cfg, b.confidence)
        return Decision(sig, "규칙 충족" if sig else "손절폭 계산 불가", {"indicators": i})


# ---------------------------------------------------------------- Jev
QUESTIONS: dict[str, dict] = {
    "thesis_aligned": {
        "type": "noul",
        "instructions": "Is the recent intraday price action consistent with the long `thesis`?",
        "criteria": {"true": "Price and volume support the thesis direction now",
                     "false": "Price action contradicts or does not yet confirm the thesis"},
    },
    "momentum": {
        "type": "score",
        "instructions": "How strong is the upward intraday momentum in `recent_bars` and `indicators`?",
        "criteria": ["Strongly down", "Weakly down", "Flat", "Weakly up", "Strongly up"],
    },
    "regime": {
        "type": "choice",
        "instructions": "Which best describes the current intraday regime of this stock?",
        "criteria": {"trend": "Orderly directional move",
                     "range": "Sideways, mean-reverting",
                     "volatile": "Erratic large swings or gaps; unsafe to enter"},
    },
}


def build_state(ctx: DecisionContext, n_bars: int = 15) -> dict:
    return {
        "market": ctx.market.value,
        "symbol": ctx.symbol,
        "time": ctx.now.isoformat(),
        "thesis": {"direction": ctx.thesis.direction, "rationale": ctx.thesis.rationale},
        "indicators": ctx.indicators,
        "recent_bars": [
            {"t": b.start.strftime("%H:%M"), "o": float(b.open), "h": float(b.high),
             "l": float(b.low), "c": float(b.close), "v": b.volume}
            for b in ctx.bars[-n_bars:]
        ],
    }


class JevPolicy:
    name = "jev"

    def __init__(self, cfg: StrategyConfig, client: JevClient):
        self.cfg, self.client = cfg, client

    def decide(self, ctx: DecisionContext) -> Decision:
        state = build_state(ctx)
        try:
            data = self.client.ask(state, QUESTIONS)
        except JevError as e:
            return Decision(None, f"Jev 실패 → HOLD: {e}", {"state": state})

        a, s = data["answers"], self.cfg.jev
        p_align = float(a["thesis_aligned"]["noul"])
        momentum = float(a["momentum"]["score"])
        p_volatile = float(a["regime"]["probabilities"]["volatile"])
        detail = {"state": state, "model": data.get("model"), "answers": a, "usage": data.get("usage")}

        if p_align < s.min_thesis_alignment:
            return Decision(None, f"논지 정합 {p_align:.2f} 미달", detail)
        if momentum < s.min_momentum_score:
            return Decision(None, f"모멘텀 {momentum:.2f} 미달", detail)
        if p_volatile > s.max_volatile_prob:
            return Decision(None, f"급변 확률 {p_volatile:.2f} 초과", detail)

        # 결합 규칙 signal_v1: 신뢰도 = 정합 확률 x (1 - 급변 확률)
        sig = build_signal(ctx, self.cfg, p_align * (1 - p_volatile))
        return Decision(sig, "진입 제안" if sig else "손절폭 계산 불가", detail)
