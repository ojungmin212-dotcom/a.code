"""연결·필드 일괄 점검(F5·H2). 결과를 data/check-report.json 과 화면에 남긴다. 로직은 harness/checks.py.

    python scripts/check_all.py                 # 조회만: 계좌·실시간·Jev·Grok (주문 없음)
    python scripts/check_all.py --send          # + 모의계좌 1주 시장가 매수 → 잔고·체결통보 확인 → 1주 매도(원상복구)
    python scripts/check_all.py --send --apply  # 주문번호·잔고·실시간 필드·체결수량 의미가 모두 확인되면 verified_by_mock: true

국내 장중(09:05~15:15)에 실행해야 주문·실시간 확인이 된다. 모의투자 서버에서만 동작한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.bootstrap import broker_fields, feed_fields, load_yaml  # noqa: E402
from harness.broker.nhplug_broker import NHPlugBroker  # noqa: E402
from harness.checks import Checker, Deps, apply_verified  # noqa: E402
from harness.models import Market  # noqa: E402

RAW = ROOT / "data" / "check-raw"


def ping(name: str) -> tuple[bool, str]:
    RAW.mkdir(parents=True, exist_ok=True)
    cmd = {"Jev": "jev_ping.py", "Grok": "grok_ping.py"}[name]
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / cmd)], capture_output=True, text=True,
                         timeout=120, check=False)
    (RAW / f"{name}.txt").write_text(out.stdout + out.stderr, encoding="utf-8")
    return out.returncode == 0, f"출력: data/check-raw/{name}.txt"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--symbol", default="005930")
    a = p.parse_args()
    order_fields, bfields = broker_fields()
    try:
        broker = NHPlugBroker(os.environ.get("NHPLUG_DEFAULT_ACCOUNT", ""), dry_run=not a.send,
                              order_no_field=order_fields, balance_fields=bfields)
    except Exception as e:
        sys.exit(f"❌ 브로커 설정: {e}")
    from nhplug.realtime import subscribe

    from harness.market.notices import NHNoticeFeed
    deps = Deps(broker=broker, feed_fields=feed_fields(Market.KR), subscribe=subscribe,
                clock=lambda: datetime.now(timezone.utc), sleep=time.sleep,
                notice_feed=NHNoticeFeed if a.send else None, run_ping=ping, raw_dir=RAW)
    rep = Checker(deps).run(a.send, a.symbol)

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "check-report.json").write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(), "results": rep.results, "facts": rep.facts},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n보고서: data/check-report.json · 원문: data/check-raw/ · 통과 "
          f"{sum(r['ok'] for r in rep.results)}/{len(rep.results)} · 확정 사실 {rep.facts}")
    if a.apply and rep.apply_ok:
        apply_verified(ROOT / "config")
        print("✅ config/broker_fields.yaml · feed.yaml 의 verified_by_mock 을 true 로 설정했습니다.")
    elif load_yaml("broker_fields.yaml").get("verified_by_mock") is not True:
        print("ℹ️  verified_by_mock 은 false 유지(승격 심사·실거래 잠금). 모두 통과 시 --send --apply 로 갱신.")


if __name__ == "__main__":
    main()
