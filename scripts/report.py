"""정책별 포워드 테스트 리포트.

    python scripts/report.py data/audit-jev.sqlite data/audit-baseline.sqlite
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.audit import AuditLog  # noqa: E402
from harness.evaluation import jev_calibration, trade_stats  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for db in sys.argv[1:]:
        a = AuditLog(db)
        s = trade_stats(a)
        pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "-"
        print(f"\n[{Path(db).name}] 청산 {s.trades}회 · 손익 {s.total_krw:,.0f}원 · 승률 {s.win_rate:.0%}"
              f" · 손익비 {pf} · 최대낙폭 {s.max_drawdown_krw:,.0f}원")
        c = jev_calibration(a)
        if c.n:
            print(f"  Jev 보정: 표본 {c.n} · Brier {c.brier:.3f} (기준 {c.base_brier:.3f}, skill {c.skill:+.3f})")
            for rng, n, hit in c.bins:
                print(f"    확률 {rng}: {n:4d}건 → 실제 상승 {hit:.0%}")
    print("\n⚠️ 표본 100회 미만 결과는 운과 구분할 수 없습니다. 지수(KOSPI) 대비 비교는 아직 미연동.")
