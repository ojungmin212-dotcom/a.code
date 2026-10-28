"""CSV 1분봉으로 비교군(baseline)과 Jev 정책을 같은 조건에서 돌린다.

    python scripts/run_replay.py data/bars.csv config/theses.yaml            # baseline 만
    python scripts/run_replay.py data/bars.csv config/theses.yaml --jev      # Jev 도 (API 키 필요)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.decision import BaselinePolicy, JevClient, JevPolicy, StrategyConfig  # noqa: E402
from harness.replay import read_bars_csv, run_replay  # noqa: E402
from harness.research import ThesisStore  # noqa: E402
from harness.risk import RiskLimits  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("bars_csv")
    p.add_argument("theses_yaml")
    p.add_argument("--jev", action="store_true")
    a = p.parse_args()

    limits = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    theses = ThesisStore.from_yaml(a.theses_yaml)
    bars = list(read_bars_csv(a.bars_csv))

    policies = [BaselinePolicy(cfg)]
    if a.jev:
        policies.append(JevPolicy(cfg, JevClient(cfg.jev.model, timeout_sec=cfg.jev.timeout_sec)))
    print("⚠️ 리플레이는 배선 점검용입니다. 성과 추정으로 쓰지 마세요.\n")
    for pol in policies:
        r = run_replay(bars, pol, theses, limits, cfg.min_bars)
        print(f"{r.policy:9s} 청산 {r.trades:3d}회  실현손익(비용 추정 포함) {r.realized_krw:>10,.0f}원"
              f"  정지={r.halted}")


if __name__ == "__main__":
    main()
