"""실시간 체결가 푸시 원문을 JSONL 로 저장 — 가격·수량 필드명 확인용 (장중에 실행).

    python scripts/capture_feed.py 005930 --n 20
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from nhplug.realtime import subscribe, ws_url


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="+")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--out", default="data/feed_capture.jsonl")
    a = p.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print("접속:", ws_url(tr_cd="mc"))
    with out.open("a", encoding="utf-8") as f:
        def on_msg(m: dict) -> None:
            f.write(json.dumps({"received_at": datetime.now(timezone.utc).isoformat(), "msg": m},
                               ensure_ascii=False) + "\n")
            print(json.dumps(m.get("body"), ensure_ascii=False)[:400])
        n = subscribe(a.symbols, on_msg, tr_cd="mc", max_messages=a.n, timeout=30)
    print(f"\n{n}건 저장 → {out}. 본문에서 현재가·체결수량 필드명을 찾아 config/feed.yaml 에 넣으세요.")


if __name__ == "__main__":
    main()
