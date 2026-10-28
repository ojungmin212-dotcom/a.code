"""장중 운영 루프(모의·실거래 공용). scripts/run_mock.py · run_live.py 가 조립해서 호출한다."""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness.broker.nhplug_broker import NHPlugBroker
from harness.engine import TradingEngine
from harness.market.bars import BarAggregator
from harness.market.nh_feed import FeedFields, NHTickFeed
from harness.models import Market
from harness.research.repo import ThesisRepo
from harness.risk import SESSIONS


@dataclass
class LoopConfig:
    symbols: list[str]
    fields: FeedFields
    kill_file: Path               # 이 파일이 생기면 즉시 정지 + 전량 청산
    reconcile_every_sec: float = 5.0
    repo: ThesisRepo | None = None


def run_loop(engine: TradingEngine, broker: NHPlugBroker, cfg: LoopConfig) -> None:
    kr = SESSIONS[Market.KR]
    scope = {(Market.KR, s) for s in cfg.symbols}
    agg, feed = BarAggregator(), None
    last_reload = last_reconcile = datetime.now(timezone.utc)

    while True:
        now = datetime.now(timezone.utc)
        if cfg.kill_file.exists() and not engine.gate.ledger.halted:
            engine.gate.ledger.halt(f"수동 킬 스위치({cfg.kill_file.name})")
            engine._halted_now(now)
        if engine.gate.ledger.halted:
            engine._exit_all("halt", now)

        if cfg.repo and (now - last_reload).total_seconds() >= 60:
            engine.theses = cfg.repo.store()
            last_reload = now
        if broker.balance_fields and (now - last_reconcile).total_seconds() >= cfg.reconcile_every_sec:
            try:
                engine.reconcile(broker.holdings(), now, scope)
            except Exception as e:  # 잔고 조회 실패는 기록만 하고 다음 주기에 재시도
                engine.audit.record("reconcile_error", {"error": str(e)}, ts=now)
            last_reconcile = now

        if not kr.is_open(now):
            engine.flatten_if_due(now)
            if kr.minutes_to_close(now) < 0 and not engine.positions and not engine.pending:
                engine.notify("장 마감 — 종료")
                return
            time.sleep(5)
            continue
        if feed is None or not feed.alive():
            feed = NHTickFeed(cfg.symbols, cfg.fields)
            feed.start()
        try:
            tick = feed.ticks.get(timeout=1.0)
        except queue.Empty:
            engine.flatten_if_due(now)
            continue
        bar = agg.add(tick)
        if bar is not None:
            engine.on_bar(bar, tick.ts)
        engine.flatten_if_due(now)
