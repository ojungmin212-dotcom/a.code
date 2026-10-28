"""기록된 1분봉으로 엔진 전체를 돌려보는 리플레이(배선 점검·정책 비교용).

⚠️ 성과 추정으로 쓰지 말 것: 청산은 봉 종가 체결로 근사하고(갭·슬리피지 무시),
LLM/Jev 는 과거 데이터에 대해 미래 정보를 알고 있을 수 있다(docs §6).
"""
from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from harness.audit import AuditLog
from harness.broker.paper import PaperBroker
from harness.decision.policy import Policy
from harness.engine import TradingEngine
from harness.market.bars import Bar
from harness.models import Market
from harness.research.thesis import ThesisStore
from harness.risk import Ledger, RiskGate, RiskLimits


def read_bars_csv(path: str | Path) -> Iterator[Bar]:
    """열: start(ISO8601, 타임존 포함), market, symbol, open, high, low, close, volume"""
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            yield Bar(Market(r["market"]), r["symbol"], datetime.fromisoformat(r["start"]),
                      Decimal(r["open"]), Decimal(r["high"]), Decimal(r["low"]),
                      Decimal(r["close"]), int(r["volume"]))


@dataclass
class ReplayResult:
    policy: str
    trades: int
    realized_krw: Decimal
    halted: bool
    audit: AuditLog


def run_replay(bars: Iterable[Bar], policy: Policy, theses: ThesisStore, limits: RiskLimits,
               min_bars: int, fx_rates: dict[Market, Decimal] | None = None) -> ReplayResult:
    fx = fx_rates or {Market.KR: Decimal(1), Market.US: Decimal(1400)}
    ledger = Ledger()
    gate = RiskGate(limits, ledger)
    broker = PaperBroker(limits.initial_capital_krw)
    for m, rate in fx.items():
        broker.set_fx(m, rate)
    audit = AuditLog()
    engine = TradingEngine(gate=gate, broker=broker, policy=policy, theses=theses,
                           min_bars=min_bars, audit=audit, fx_rates=fx)
    last_now = None
    for bar in sorted(bars, key=lambda b: b.start):
        now = bar.start + timedelta(minutes=1)  # 봉 마감 시각에 판단
        broker.set_price(bar.market, bar.symbol, bar.close)
        engine.on_bar(bar, now)
        last_now = now
    if last_now is not None:
        engine.flatten_if_due(last_now + timedelta(days=1))  # 남은 포지션 강제 청산
    return ReplayResult(policy.name, len(audit.events("exit")),
                        ledger.cumulative_realized_krw, ledger.halted, audit)
