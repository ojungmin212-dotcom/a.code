"""Grok 리서치 루프(별도 프로세스). 논지를 data/theses.sqlite 에 쓰고 매매 프로세스들이 읽는다.

    python scripts/run_research.py --once     # 지금 1회
    python scripts/run_research.py            # 시장별 개장 60분 전 1회 + (refresh_min 설정 시) 장중 갱신
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.audit import AuditLog  # noqa: E402
from harness.models import Market  # noqa: E402
from harness.research.grok import GrokClient  # noqa: E402
from harness.research.repo import ThesisRepo  # noqa: E402
from harness.research.service import ResearchConfig, ResearchService  # noqa: E402
from harness.risk import SESSIONS  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--force", action="store_true", help="휴장일에도 실행(비용 발생)")
    a = p.parse_args()

    cfg = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
    audit = AuditLog(ROOT / "data" / "audit-research.sqlite")
    svc = ResearchService(cfg, GrokClient(cfg.model, timeout_sec=cfg.timeout_sec),
                          ThesisRepo(ROOT / "data" / "theses.sqlite"), audit)
    per_run = cfg.per_symbol_usd() * len(cfg.universe) * cfg.usd_krw
    print(f"1회 추정 비용 {per_run:,.0f}원 · 월 예산 {cfg.monthly_budget_krw:,.0f}원 "
          f"(사용 {svc.budget.spent_krw(datetime.now(timezone.utc)):,.0f}원)")
    if per_run * 22 > cfg.monthly_budget_krw:  # 장 전 1회 x 월 22거래일만 해도
        print(f"⚠️ 장 전 1회만 돌려도 월 {per_run * 22:,.0f}원 → 예산 소진 후 리서치가 멈추고 매매도 멈춥니다.")

    def run(market: Market | None = None) -> None:
        now = datetime.now(timezone.utc)
        for sym, r in svc.run(now, market).items():
            print(f"{now:%H:%M} {sym}: {r}")

    if a.once:
        if not a.force and not any(SESSIONS[u.market].is_trading_day(datetime.now(timezone.utc))
                                   for u in cfg.universe):
            print("휴장일 — 리서치를 건너뜁니다(비용 절약). 강제 실행은 --force")
            return
        run()
        return

    markets = sorted({u.market for u in cfg.universe})
    last: dict[Market, datetime] = {}
    while True:
        now = datetime.now(timezone.utc)
        for m in markets:
            s = SESSIONS[m]
            if s.done_for_day(now):
                continue
            prev = last.get(m)
            first_today = s.minutes_since_open(now) >= -60 and (
                prev is None or s.trading_date(prev) != s.trading_date(now))
            refresh = (cfg.refresh_min is not None and prev is not None and s.is_open(now)
                       and now - prev >= timedelta(minutes=cfg.refresh_min))
            if first_today or refresh:
                run(m)
                last[m] = now
        time.sleep(30)

if __name__ == "__main__":
    main()
