from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.models import Market


class RiskLimits(BaseModel):
    """config/risk.yaml 과 1:1. 누락·오타 키는 로드 시점에 실패한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_capital_krw: Decimal = Field(gt=0)
    max_total_loss_krw: Decimal = Field(gt=0)
    max_daily_loss_krw: Decimal = Field(gt=0)
    max_loss_per_trade_krw: Decimal = Field(gt=0)
    max_open_positions: int = Field(ge=1)
    max_position_krw: Decimal = Field(gt=0)
    max_trades_per_day: dict[Market, int]
    max_consecutive_losses: int = Field(ge=1)
    min_confidence: float = Field(ge=0.0, le=1.0)
    min_edge_cost_multiple: float = Field(ge=0.0)
    round_trip_cost_pct: dict[Market, float]
    no_entry_after_open_min: int = Field(ge=0)
    no_entry_before_close_min: int = Field(ge=0)
    flatten_before_close_min: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> RiskLimits:
        if not (self.max_loss_per_trade_krw <= self.max_daily_loss_krw <= self.max_total_loss_krw):
            raise ValueError("1회 손실 <= 일일 손실 <= 총 손실 순서여야 합니다")
        if self.max_total_loss_krw >= self.initial_capital_krw:
            raise ValueError("총 손실 한도는 초기 자본보다 작아야 합니다")
        for name in ("max_trades_per_day", "round_trip_cost_pct"):
            missing = set(Market) - set(getattr(self, name))
            if missing:
                raise ValueError(f"{name} 에 시장 누락: {sorted(missing)}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> RiskLimits:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))
