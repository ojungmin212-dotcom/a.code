"""Grok 리서치봇 3종. 각 봇은 JSON 스키마로만 답한다.

- sentiment  : 뉴스·X 게시물 기반 당일 심리 (web_search + x_search)
- fundamental: 공시·실적·밸류에이션 (web_search)
- bear       : 위 두 의견을 반박 — 논지를 무효화할 위험 (도구 없음, 추가 검색 비용 없음)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .grok import GrokClient


class BotOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stance: Literal["LONG", "NEUTRAL", "AVOID"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    risks: list[str]

    @field_validator("reasons", "risks")
    @classmethod
    def _trim(cls, v: list[str]) -> list[str]:
        return [x[:200] for x in v[:5]]


class BearOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invalidation_prob: float = Field(ge=0, le=1)
    counterarguments: list[str]

    @field_validator("counterarguments")
    @classmethod
    def _trim(cls, v: list[str]) -> list[str]:
        return [x[:200] for x in v[:5]]


def _schema(model: type[BaseModel]) -> dict:
    s = model.model_json_schema()
    s["additionalProperties"] = False
    s["required"] = list(s["properties"])
    return s


OPINION_SCHEMA = _schema(BotOpinion)
BEAR_SCHEMA = _schema(BearOpinion)

_COMMON = (
    "You are an equity research assistant for an intraday long-only trading system. "
    "Current time: {now}. Use only information published before this time. "
    "If evidence is weak or stale, answer NEUTRAL with low confidence — do not guess. "
    "Write reasons and risks in Korean, each under 120 characters."
)

SENTIMENT_SYS = _COMMON + (
    " Task: judge today's news and X (Twitter) sentiment for the stock. "
    "Discount promotional, anonymous or coordinated-looking posts."
)
FUNDAMENTAL_SYS = _COMMON + (
    " Task: judge recent disclosures, earnings, guidance and valuation news that could drive "
    "an intraday move today."
)
BEAR_SYS = (
    "You are a skeptical risk reviewer. Current time: {now}. Given two research opinions about "
    "an intraday LONG idea, estimate the probability that the idea is invalid today "
    "(already priced in, contradicted by evidence, or based on weak sources). "
    "Write counterarguments in Korean."
)


def _target(market: str, symbol: str, name: str) -> str:
    return f"Stock: {name} ({market}:{symbol})."


def ask_sentiment(client: GrokClient, market: str, symbol: str, name: str, now: datetime):
    r = client.ask(SENTIMENT_SYS.format(now=now.isoformat()), _target(market, symbol, name),
                   OPINION_SCHEMA, "opinion", tools=["web_search", "x_search"])
    return BotOpinion.model_validate(r.data), r


def ask_fundamental(client: GrokClient, market: str, symbol: str, name: str, now: datetime):
    r = client.ask(FUNDAMENTAL_SYS.format(now=now.isoformat()), _target(market, symbol, name),
                   OPINION_SCHEMA, "opinion", tools=["web_search"])
    return BotOpinion.model_validate(r.data), r


def ask_bear(client: GrokClient, market: str, symbol: str, name: str, now: datetime,
             opinions: dict[str, BotOpinion]):
    payload = json.dumps({k: v.model_dump() for k, v in opinions.items()}, ensure_ascii=False)
    r = client.ask(BEAR_SYS.format(now=now.isoformat()),
                   _target(market, symbol, name) + "\nOpinions:\n" + payload, BEAR_SCHEMA, "bear")
    return BearOpinion.model_validate(r.data), r
