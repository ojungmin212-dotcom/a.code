"""장중 운영 루프(모의·실거래, 국내·미국 공용).

국내(09:00–15:30 KST)와 미국(23:30–06:00 KST, 서머타임 시 1시간 앞당김) 정규장은 겹치지 않으므로
한 프로세스가 열린 시장 하나만 구독한다. 손실 한도 장부를 시장끼리 공유하기 위해서다.
국내만 운영하면 장 마감 후 종료, 두 시장을 운영하면 상주한다.
"""
from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from harness.broker.nhplug_broker import NHPlugBroker
from harness.engine import TradingEngine
from harness.market.bars import BarAggregator
from harness.market.nh_feed import FeedFields, NHTickFeed
from harness.models import Market
from harness.research.repo import ThesisRepo
from harness.risk import SESSIONS


@dataclass
class MarketSpec:
    market: Market
    symbols: list[str]
    fields: FeedFields
    tr_cd: str                                   # 국내 mc / 미국 RC
    keys: dict[str, str] = field(default_factory=dict)  # 종목 → 구독키(미국: GIC)


@dataclass
class LoopConfig:
    specs: list[MarketSpec]
    kill_file: Path               # 이 파일이 생기면 즉시 정지 + 전량 청산
    reconcile_every_sec: float = 5.0
    repo: ThesisRepo | None = None
    fx_source: Callable[[datetime], dict[Market, Decimal]] | None = None

    @property
    def symbols(self) -> list[str]:
        return [f"{s.market.value}:{x}" for s in self.specs for x in s.symbols]


def run_loop(engine: TradingEngine, broker: NHPlugBroker, cfg: LoopConfig) -> None:
    scope = {(s.market, x) for s in cfg.specs for x in s.symbols}
    agg = BarAggregator()
    feeds: dict[Market, NHTickFeed] = {}
    last_slow = last_reconcile = datetime.fromtimestamp(0, timezone.utc)

    while True:
        now = datetime.now(timezone.utc)
        if cfg.kill_file.exists() and not engine.gate.ledger.halted:
            engine.gate.ledger.halt(f"수동 킬 스위치({cfg.kill_file.name})")
            engine._halted_now(now)
        if engine.gate.ledger.halted:
            engine._exit_all("halt", now)

        if (now - last_slow).total_seconds() >= 60:
            if cfg.repo:
                engine.theses = cfg.repo.store()
            if cfg.fx_source:
                engine.set_fx(cfg.fx_source(now), now)
            last_slow = now
        if broker.balance_fields and (now - last_reconcile).total_seconds() >= cfg.reconcile_every_sec:
            try:
                engine.reconcile(broker.holdings(), now, scope)
            except Exception as e:  # 잔고 조회 실패는 기록만 하고 다음 주기에 재시도
                engine.audit.record("reconcile_error", {"error": str(e)}, ts=now)
            last_reconcile = now

        open_specs = [s for s in cfg.specs if SESSIONS[s.market].is_open(now)]
        if not open_specs:
            engine.flatten_if_due(now)
            all_done = all(SESSIONS[s.market].minutes_to_close(now) < 0 for s in cfg.specs)
            if all_done and not engine.positions and not engine.pending:
                engine.notify("장 마감 — 종료")
                return
            time.sleep(5)
            continue

        spec = open_specs[0]
        feed = feeds.get(spec.market)
        if feed is None or not feed.alive():
            feed = NHTickFeed(spec.symbols, spec.fields, market=spec.market, tr_cd=spec.tr_cd,
                              keys=spec.keys or None)
            feed.start()
            feeds[spec.market] = feed
        try:
            tick = feed.ticks.get(timeout=1.0)
        except queue.Empty:
            engine.flatten_if_due(now)
            continue
        bar = agg.add(tick)
        if bar is not None:
            engine.on_bar(bar, tick.ts)
        engine.flatten_if_due(now)
