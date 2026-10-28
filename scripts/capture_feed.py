"""실시간 체결가 푸시 원문을 JSONL 로 저장 — 가격·수량 필드명 확인용 (장중에 실행).

    python scripts/capture_feed.py 005930 --n 20
    python scripts/capture_feed.py AAPL --us          # 미국(RC, 티커→GIC 자동 변환, 미국 장중)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import sys

from nhplug.realtime import subscribe, ws_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.market.instruments import gic_map  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="+")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--out", default="data/feed_capture.jsonl")
    p.add_argument("--us", action="store_true")
    a = p.parse_args()
    tr_cd = "RC" if a.us else "mc"
    keys = list(gic_map(a.symbols).values()) if a.us else a.symbols

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print("접속:", ws_url(tr_cd=tr_cd), "구독키:", keys)
    with out.open("a", encoding="utf-8") as f:
        def on_msg(m: dict) -> None:
            f.write(json.dumps({"received_at": datetime.now(timezone.utc).isoformat(), "msg": m},
                               ensure_ascii=False) + "\n")
            print(json.dumps(m.get("body"), ensure_ascii=False)[:400])
        n = subscribe(keys, on_msg, tr_cd=tr_cd, max_messages=a.n, timeout=30)
    print(f"\n{n}건 저장 → {out}. 본문에서 현재가·체결수량 필드명을 찾아 config/feed.yaml 에 넣으세요"
          + (" (us_price_field / us_volume_field)." if a.us else "."))


if __name__ == "__main__":
    main()
