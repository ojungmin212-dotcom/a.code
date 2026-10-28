"""Grok 연결·응답 형식 점검: 반대논리봇 1회(검색 도구 없음 → 비용 최소). XAI_API_KEY 필요.
응답 원문을 출력하므로 출력 텍스트·usage 필드 위치를 확인할 수 있다."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.research.bots import BEAR_SCHEMA, BEAR_SYS  # noqa: E402
from harness.research.grok import GrokClient  # noqa: E402
from harness.research.service import ResearchConfig  # noqa: E402

if __name__ == "__main__":
    cfg = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
    r = GrokClient(cfg.model, timeout_sec=60).ask(
        BEAR_SYS.format(now=datetime.now(timezone.utc).isoformat()),
        'Stock: 점검용. Opinions: {"sentiment": {"stance": "LONG", "confidence": 0.7}}',
        BEAR_SCHEMA, "bear")
    print("구조화 출력:", r.data)
    print("usage:", r.usage)
    print("원문:", json.dumps(r.raw, ensure_ascii=False)[:2000])
