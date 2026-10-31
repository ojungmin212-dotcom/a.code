"""장중 운영 루프(모의·실거래, 국내·미국 공용).

- 한 프로세스가 **시세 피드 하나를 여러 정책(레인)이 공유**한다(F9). NH 웹소켓은 앱키당 세션 2개가
  한도라 정책별 프로세스를 띄우면 한도를 다 쓴다.
- 브로커로 주문하는 레인은 하나뿐이다. 같은 계좌를 두 정책이 쓰면 잔고 대조에서 서로의 보유분이 섞인다.
  나머지 레인(비교군)은 같은 시세로 메모리 모의 체결(PaperBroker)한다.
- 국내(09:00–15:30 KST)와 미국(23:30–06:00 KST) 정규장은 겹치지 않으므로 열린 시장 하나만 구독한다.
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
from harness.broker.paper import PaperBroker
from harness.engine import TradingEngine
from harness.market.bars import BarAggregator
from harness.market.nh_feed import FeedFields, NHTickFeed
from harness.market.notices import NHNoticeFeed
from harness.market.recorder import TickRecorder
from harness.models import Market
from harness.research.repo import ThesisRepo
from harness.risk import SESSIONS


@dataclass
class MarketSpec:
    market: Market
    symbols: list[str]                           # 구독 종목(매매 대상 + 섀도 전용)
    fields: FeedFields
    tr_cd: str                                   # 국내 mc / 미국 RC
    keys: dict[str, str] = field(default_factory=dict)  # 종목 → 구독키(미국: GIC)
    tradable: list[str] = field(default_factory=list)   # 매매 대상(잔고 대조 범위)


@dataclass
class Lane:
    engine: TradingEngine
    broker: NHPlugBroker | PaperBroker

    @property
    def is_paper(self) -> bool:
        return isinstance(self.broker, PaperBroker)


@dataclass
class LoopConfig:
    specs: list[MarketSpec]
    kill_file: Path               # 이 파일이 생기면 모든 레인 정지 + 전량 청산
    reconcile_every_sec: float = 5.0
    repo: ThesisRepo | None = None
    fx_source: Callable[[datetime], dict[Market, Decimal]] | None = None
    pending_timeout_sec: float = 60
    fill_notices: bool = False
    tick_dir: Path | None = None
    heartbeat_min: int = 0

    @property
    def symbols(self) -> list[str]:
        return [f"{s.market.value}:{x}" for s in self.specs for x in s.symbols]


def summary(lanes: list[Lane], now: datetime, title: str) -> str:
    lines = [title]
    for ln in lanes:
        e = ln.engine
        days = {SESSIONS[m].trading_date(now) for m in SESSIONS}
        pnl = sum(e.gate.ledger.daily_realized(d) for d in days)
        today = now.strftime("%Y-%m-%d")
        shadow = sum(1 for x in e.audit.events("shadow") if x["ts"][:10] == today) if e.shadow else 0
        lines.append(f"- {e.policy.name}{'(paper)' if ln.is_paper else ''}: 보유 {len(e.positions)} · "
                     f"대기 {len(e.pending)} · 오늘 실현 {pnl:,.0f}원 · 누적 {e.gate.ledger.cumulative_realized_krw:,.0f}원"
                     + (f" · 섀도 {shadow}건" if e.shadow else "")
                     + (f" · 🛑 {e.gate.ledger.halt_reason}" if e.gate.ledger.halted else ""))
    return "\n".join(lines)


def run_loop(lanes: list[Lane], cfg: LoopConfig) -> None:
    if sum(not ln.is_paper for ln in lanes) > 1:
        raise ValueError("브로커로 주문하는 레인은 하나만 둘 수 있습니다(같은 계좌 잔고 혼선)")
    scope = {(s.market, x) for s in cfg.specs for x in (s.tradable or s.symbols)}
    agg = BarAggregator()
    feeds: dict[Market, NHTickFeed] = {}
    broker_lane = next((ln for ln in lanes if not ln.is_paper), None)
    notices: NHNoticeFeed | None = None
    recorder = TickRecorder(cfg.tick_dir) if cfg.tick_dir else None
    last_slow = last_reconcile = datetime.fromtimestamp(0, timezone.utc)
    last_beat = datetime.now(timezone.utc)

    while True:
        now = datetime.now(timezone.utc)
        if cfg.heartbeat_min and (now - last_beat).total_seconds() >= cfg.heartbeat_min * 60:
            lanes[0].engine.notify(summary(lanes, now, "💓 생존 신호"))
            last_beat = now
        if broker_lane and cfg.fill_notices and not broker_lane.broker.dry_run:
            if notices is None or not notices.alive():
                notices = NHNoticeFeed()
                notices.start()
            while not notices.notices.empty():
                n = notices.notices.get_nowait()
                broker_lane.engine.on_fill_notice(n.order_no, n.qty, n.price, n.rejected, now)
        for ln in lanes:
            e = ln.engine
            if cfg.kill_file.exists() and not e.gate.ledger.halted:
                e.gate.ledger.halt(f"수동 킬 스위치({cfg.kill_file.name})")
                e._halted_now(now)
            if e.gate.ledger.halted:
                e._exit_all("halt", now)

        if (now - last_slow).total_seconds() >= 60:
            store = cfg.repo.store() if cfg.repo else None
            rates = cfg.fx_source(now) if cfg.fx_source else None
            for ln in lanes:
                if store is not None:
                    ln.engine.theses = store
                if rates is not None:
                    ln.engine.set_fx(rates, now)
                    if ln.is_paper:
                        for m, r in rates.items():
                            ln.broker.set_fx(m, r)
            last_slow = now
        if (now - last_reconcile).total_seconds() >= cfg.reconcile_every_sec:
            for ln in lanes:
                if not ln.is_paper and ln.broker.balance_fields:
                    try:
                        ln.engine.reconcile(ln.broker.holdings(), now, scope, cfg.pending_timeout_sec)
                    except Exception as e:  # 잔고 조회 실패는 기록만 하고 다음 주기에 재시도
                        ln.engine.audit.record("reconcile_error", {"error": str(e)}, ts=now)
            last_reconcile = now

        open_specs = [s for s in cfg.specs if SESSIONS[s.market].is_open(now)]
        if not open_specs:
            for ln in lanes:
                ln.engine.flatten_if_due(now)
            all_done = all(SESSIONS[s.market].done_for_day(now) for s in cfg.specs)
            busy = any(ln.engine.positions or ln.engine.pending for ln in lanes)
            if all_done and not busy:
                lanes[0].engine.notify(summary(lanes, now, "📊 장 마감 요약 — 종료"))
                if recorder:
                    recorder.close()
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
            for ln in lanes:
                ln.engine.flatten_if_due(now)
            continue
        if recorder:
            recorder.write(tick)
        bar = agg.add(tick)
        for ln in lanes:
            if ln.is_paper:
                ln.broker.set_price(tick.market, tick.symbol, tick.price)
            if bar is not None:
                ln.engine.on_bar(bar, tick.ts)
            ln.engine.flatten_if_due(now)
