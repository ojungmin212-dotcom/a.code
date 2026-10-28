"""Grok 리서치 루프(별도 프로세스). 논지를 data/theses.sqlite 에 쓰고 매매 프로세스들이 읽는다.

    python scripts/run_research.py --once     # 지금 1회
    python scripts/run_research.py            # 장 전 08:00 1회 + (refresh_min 설정 시) 장중 갱신
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

    def run() -> None:
        now = datetime.now(timezone.utc)
        for sym, r in svc.run(now).items():
            print(f"{now:%H:%M} {sym}: {r}")

    if a.once:
        run()
        return

    kr = SESSIONS[Market.KR]
    last: datetime | None = None
    while True:
        now = datetime.now(timezone.utc)
        local = kr.local(now)
        if not kr.is_weekday(now) or kr.minutes_to_close(now) < 0:
            time.sleep(60)
            continue
        first_today = local.hour >= 8 and (last is None or kr.local(last).date() != local.date())
        refresh = (cfg.refresh_min is not None and last is not None and kr.is_open(now)
                   and now - last >= timedelta(minutes=cfg.refresh_min))
        if first_today or refresh:
            run()
            last = now
        time.sleep(30)

if __name__ == "__main__":
    main()
