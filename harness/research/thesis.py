"""매매 논지(Thesis). P1 은 사람이 YAML 로 작성, P2 에서 Grok 리서치봇이 생성한다."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.models import Market


class Thesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market: Market
    symbol: str = Field(min_length=1)
    direction: str = Field(pattern="^LONG$")  # 현금 계좌 롱 전용
    rationale: str = Field(min_length=1)
    valid_from: datetime
    valid_until: datetime
    source: str = "manual"

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("시각에 타임존을 넣어야 합니다 (예: 2026-10-29T09:00:00+09:00)")
        return v

    def active(self, now: datetime) -> bool:
        return self.valid_from <= now < self.valid_until


class ThesisStore:
    def __init__(self, theses: list[Thesis]):
        self._items = theses

    @classmethod
    def from_yaml(cls, path: str | Path) -> ThesisStore:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or []
        return cls([Thesis.model_validate(x) for x in raw])

    def active(self, market: Market, symbol: str, now: datetime) -> Thesis | None:
        hits = [t for t in self._items if t.market == market and t.symbol == symbol and t.active(now)]
        return max(hits, key=lambda t: t.valid_from) if hits else None

    def watchlist(self, now: datetime) -> list[tuple[Market, str]]:
        return sorted({(t.market, t.symbol) for t in self._items if t.active(now)})
