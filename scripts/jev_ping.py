"""Jev 연결 점검: 모델 목록 조회 + 샘플 질문 1건.  TYPESAFE_API_KEY 필요."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.decision.jev import DEFAULT_BASE_URL, JevClient  # noqa: E402
from harness.decision.policy import QUESTIONS, StrategyConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    base = os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    r = requests.get(f"{base}/v1/models", timeout=5,
                     headers={"Authorization": f"Bearer {os.environ.get('TYPESAFE_API_KEY', '')}"})
    print("models:", r.status_code, r.text[:300])

    state = {"symbol": "005930", "thesis": {"direction": "LONG", "rationale": "점검용"},
             "indicators": {"ret_15m_pct": 0.4, "dist_vwap_pct": 0.2, "volume_ratio": 1.5},
             "recent_bars": []}
    data = JevClient(cfg.jev.model, timeout_sec=10).ask(state, QUESTIONS)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
