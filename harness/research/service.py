"""리서치 1회 실행: 후보 종목마다 봇 3개 → 결합 → 논지 저장. 예산 초과·오류는 '논지 없음'으로 끝난다."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from harness.audit import AuditLog
from harness.models import Market

from .aggregate import AggregateSettings, aggregate
from .bots import ask_bear, ask_fundamental, ask_sentiment
from .budget import BudgetExceeded, BudgetGuard
from .grok import GrokClient, GrokError
from .repo import ThesisRepo


class UniverseItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    market: Market
    symbol: str
    name: str


class ResearchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str
    timeout_sec: float = Field(gt=0)
    universe: list[UniverseItem]
    thesis_ttl_min: int = Field(gt=0)
    refresh_min: int | None = Field(default=None, gt=0)
    monthly_budget_krw: float = Field(ge=0)
    usd_krw: float = Field(gt=0)
    est_cost_usd: dict[str, float]
    token_price_usd_per_m: dict[str, float]
    aggregate: AggregateSettings

    @classmethod
    def from_yaml(cls, path: str | Path) -> ResearchConfig:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def per_symbol_usd(self) -> float:
        return sum(self.est_cost_usd[k] for k in ("sentiment", "fundamental", "bear"))


class ResearchService:
    def __init__(self, cfg: ResearchConfig, client: GrokClient, repo: ThesisRepo, audit: AuditLog):
        self.cfg, self.client, self.repo, self.audit = cfg, client, repo, audit
        self.budget = BudgetGuard(audit, cfg.monthly_budget_krw, cfg.usd_krw)

    def run(self, now: datetime, market: Market | None = None) -> dict[str, str]:
        results = {}
        for item in self.cfg.universe:
            if market is None or item.market is market:
                results[item.symbol] = self._one(item, now)
        return results

    def _one(self, item: UniverseItem, now: datetime) -> str:
        m, sym = item.market.value, item.symbol
        try:
            # 종목 하나를 끝까지 평가할 예산이 없으면 시작하지 않는다(반쪽 결과로 비용만 쓰는 것 방지)
            self.budget.reserve(self.cfg.per_symbol_usd(), now)
            opinions = {}
            for name, fn in (("sentiment", ask_sentiment), ("fundamental", ask_fundamental)):
                op, res = fn(self.client, m, sym, item.name, now)
                self._charge(name, res.usage, now, sym)
                opinions[name] = op
                self.audit.record("research_opinion", {"bot": name, "opinion": op,
                                                       "citations": res.citations},
                                  market=None, symbol=sym, ts=now)
            bear, res = ask_bear(self.client, m, sym, item.name, now, opinions)
            self._charge("bear", res.usage, now, sym)
        except (GrokError, BudgetExceeded, ValueError) as e:
            self.repo.expire(m, sym, now)
            self.audit.record("research_failed", {"error": str(e)}, symbol=sym, ts=now)
            return f"실패 → 논지 없음: {e}"

        thesis, reason, score = aggregate(item.market, sym, opinions, bear, self.cfg.aggregate,
                                          now, self.cfg.thesis_ttl_min)
        self.repo.expire(m, sym, now)
        if thesis is not None:
            self.repo.add(thesis)
        self.audit.record("research_result", {"reason": reason, "score": score, "bear": bear,
                                              "thesis": thesis}, symbol=sym, ts=now)
        return reason

    def _charge(self, bot: str, usage: dict, now: datetime, symbol: str) -> None:
        self.budget.charge(bot, self.cfg.est_cost_usd[bot], usage, self.cfg.token_price_usd_per_m,
                           now, symbol=symbol)
