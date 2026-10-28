"""모의투자 장중 운영 (국내). 기본은 dry_run — 주문은 --send 일 때만 모의 서버로 전송.

    python scripts/run_mock.py config/theses.yaml                    # 판단·로그만
    python scripts/run_mock.py config/theses.yaml --send             # 모의투자 주문 전송
    python scripts/run_mock.py config/theses.yaml --policy baseline  # 비교군
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.audit import AuditLog  # noqa: E402
from harness.broker.nhplug_broker import NHPlugBroker  # noqa: E402
from harness.decision import BaselinePolicy, JevClient, JevPolicy, StrategyConfig  # noqa: E402
from harness.engine import TradingEngine, restore_ledger, unreconciled_symbols  # noqa: E402
from harness.market.bars import BarAggregator  # noqa: E402
from harness.market.nh_feed import FeedFields, NHTickFeed  # noqa: E402
from harness.models import Market  # noqa: E402
from harness.research import ThesisStore  # noqa: E402
from harness.risk import SESSIONS, Ledger, RiskGate, RiskLimits  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("theses_yaml")
    p.add_argument("--send", action="store_true")
    p.add_argument("--policy", choices=["jev", "baseline"], default="jev")
    p.add_argument("--order-no-field", default=None, help="mock_smoke.py 로 확인한 주문번호 필드명")
    a = p.parse_args()

    feed_cfg = yaml.safe_load((ROOT / "config" / "feed.yaml").read_text(encoding="utf-8"))
    if not feed_cfg.get("price_field") or not feed_cfg.get("volume_field"):
        sys.exit("config/feed.yaml 의 price_field / volume_field 를 먼저 채우세요 (scripts/capture_feed.py).")

    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    theses = ThesisStore.from_yaml(a.theses_yaml)
    audit = AuditLog(ROOT / "data" / f"audit-{a.policy}.sqlite")
    broker = NHPlugBroker(os.environ.get("NHPLUG_DEFAULT_ACCOUNT", ""), dry_run=not a.send,
                          order_no_field=a.order_no_field, guard=audit.guard())
    policy = (JevPolicy(cfg, JevClient(cfg.jev.model, timeout_sec=cfg.jev.timeout_sec))
              if a.policy == "jev" else BaselinePolicy(cfg))
    leftover = unreconciled_symbols(audit)
    if leftover:
        sys.exit(f"이전 실행의 미청산/결과불명 주문이 있습니다: {leftover}. "
                 "브로커 화면으로 확인·정리한 뒤 감사 로그에 pending_resolved 를 남기고 다시 시작하세요.")
    gate = RiskGate(limits, Ledger())
    restore_ledger(audit, gate)
    engine = TradingEngine(gate=gate, broker=broker, policy=policy,
                           theses=theses, min_bars=cfg.min_bars, audit=audit)

    symbols = [s for m, s in theses.watchlist(datetime.now(timezone.utc)) if m is Market.KR]
    if not symbols:
        sys.exit("지금 유효한 국내 Thesis 가 없습니다.")
    fields = FeedFields(feed_cfg["price_field"], feed_cfg["volume_field"])
    agg = BarAggregator()
    feed = None
    print(f"정책={policy.name} 전송={'모의' if a.send else 'dry_run'} 종목={symbols}")

    while True:
        now = datetime.now(timezone.utc)
        kr = SESSIONS[Market.KR]
        if not kr.is_open(now):
            engine.flatten_if_due(now)
            if kr.minutes_to_close(now) < 0:
                print("장 마감 — 종료")
                return
            time.sleep(30)
            continue
        if feed is None or not feed.alive():
            feed = NHTickFeed(symbols, fields)
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


if __name__ == "__main__":
    main()
