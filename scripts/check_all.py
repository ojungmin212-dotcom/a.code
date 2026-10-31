"""연결·필드 일괄 점검(F5). 결과를 data/check-report.json 과 화면에 남긴다.

    python scripts/check_all.py                 # 조회만: 계좌·잔고·실시간·Jev·Grok (주문 없음)
    python scripts/check_all.py --send          # + 모의계좌 1주 시장가 매수 → 잔고 확인 → 1주 매도(원상복구)
    python scripts/check_all.py --send --apply  # 모든 필드 확인되면 verified_by_mock: true 로 설정 파일 갱신

국내 장중(09:10~15:20)에 실행해야 실시간·체결 확인이 된다. 모의투자 서버에서만 동작한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.bootstrap import broker_fields, feed_fields, load_yaml  # noqa: E402
from harness.broker.nhplug_broker import NHPlugBroker, server_message  # noqa: E402
from harness.models import Market, OrderRequest, OrderStatus, OrderType, Side  # noqa: E402

RAW = ROOT / "data" / "check-raw"
results: list[dict] = []


def step(name: str, ok: bool, detail: str, raw: object = None) -> bool:
    results.append({"name": name, "ok": ok, "detail": detail})
    print(f"{'✅' if ok else '❌'} {name}: {detail}", flush=True)
    if raw is not None:
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{len(results):02d}-{re.sub(r'[^0-9A-Za-z가-힣]+', '_', name)}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return ok


def check_balance_fields(broker: NHPlugBroker, symbol: str) -> tuple[bool, int]:
    data = broker.raw_kr_balance()
    f = broker.balance_fields
    rows = data.get(f.holdings_list) if isinstance(data, dict) else None
    rows = rows if isinstance(rows, list) else ([rows] if rows else [])
    hit = [r for r in rows if str(r.get(f.symbol_field, "")).strip() == symbol]
    if not hit:
        step("잔고 조회", isinstance(data, dict) and f.holdings_list in data,
             f"{symbol} 보유 없음 · {server_message(data)}", data)
        return False, 0
    missing = [k for k in (f.quantity_field, f.avg_price_field) if k not in hit[0]]
    qty = int(float(str(hit[0].get(f.quantity_field, 0)).replace(",", "") or 0))
    ok = step("잔고 필드", not missing, f"누락 {missing}" if missing else f"{symbol} {qty}주 · 필드 일치", data)
    return ok, qty


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--symbol", default="005930")
    a = p.parse_args()
    order_fields, bfields = broker_fields()

    # 1. 브로커 환경
    try:
        broker = NHPlugBroker(os.environ.get("NHPLUG_DEFAULT_ACCOUNT", ""), dry_run=not a.send,
                              order_no_field=order_fields, balance_fields=bfields)
        step("모의투자 서버", broker.is_mock(), "moapi 확인")
    except Exception as e:
        step("브로커 설정", False, str(e))
        return finish(a.apply)

    try:
        acc = broker.raw_accounts()
        rows = acc.get("Output_0") or []
        mine = [r for r in rows if str(r.get("acct_no")) == broker.account_no]
        step("계좌", bool(mine) and str(mine[0].get("acct_type")) == "03",
             f"acct_type={mine[0].get('acct_type') if mine else '없음'} (모의=03)", acc)
    except Exception as e:
        step("계좌", False, str(e))

    # 2. 주문 → 잔고 → 원상복구
    order_ok = bal_ok = False
    if a.send:
        cid = f"check-{uuid.uuid4().hex[:8]}"
        r = broker.place_order(OrderRequest(client_order_id=cid, market=Market.KR, symbol=a.symbol,
                                            side=Side.BUY, quantity=1, order_type=OrderType.MARKET))
        order_ok = step("주문번호 필드", r.status is OrderStatus.ACCEPTED,
                        f"{r.status} 주문번호={r.broker_order_id} · {r.message}", r.raw)
        time.sleep(5)
        try:
            bal_ok, qty = check_balance_fields(broker, a.symbol)
            if qty >= 1:
                s = broker.place_order(OrderRequest(client_order_id=cid + "-s", market=Market.KR,
                                                    symbol=a.symbol, side=Side.SELL, quantity=1,
                                                    order_type=OrderType.MARKET))
                step("원상복구 매도", s.status is OrderStatus.ACCEPTED, f"{s.status} · {s.message}", s.raw)
        except Exception as e:
            step("잔고 조회", False, str(e))
    else:
        print("ℹ️  --send 없이 실행: 주문번호·잔고 필드 확인은 건너뜀")

    # 3. 실시간 체결가
    feed_ok = False
    ff = feed_fields(Market.KR)
    try:
        from nhplug.realtime import subscribe
        msgs: list[dict] = []
        subscribe([a.symbol], msgs.append, tr_cd="mc", max_messages=5, timeout=20)
        bodies = [m.get("body") or {} for m in msgs]
        good = [b for b in bodies if ff and ff.price in b and ff.volume in b]
        feed_ok = step("실시간 필드", bool(good),
                       f"{len(msgs)}건 수신 · 필드 일치 {len(good)}건" if msgs else "0건(장 마감이면 정상)", msgs)
    except Exception as e:
        step("실시간", False, str(e))

    # 4. Jev / Grok
    RAW.mkdir(parents=True, exist_ok=True)
    for name, cmd in (("Jev", "jev_ping.py"), ("Grok", "grok_ping.py")):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / cmd)], capture_output=True,
                             text=True, timeout=120)
        (RAW / f"{name}.txt").write_text(out.stdout + out.stderr, encoding="utf-8")
        step(name, out.returncode == 0, f"출력: data/check-raw/{name}.txt")

    finish(a.apply and order_ok and bal_ok and feed_ok)


def finish(apply: bool) -> None:
    report = {"at": datetime.now(timezone.utc).isoformat(), "results": results}
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "check-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    print(f"\n보고서: data/check-report.json · 원문: data/check-raw/ · 통과 {sum(r['ok'] for r in results)}/{len(results)}")
    if apply:
        for name in ("broker_fields.yaml", "feed.yaml"):
            path = ROOT / "config" / name
            path.write_text(re.sub(r"(?m)^verified_by_mock: .*$", "verified_by_mock: true",
                                   path.read_text(encoding="utf-8")), encoding="utf-8")
        print("✅ config/broker_fields.yaml · feed.yaml 의 verified_by_mock 을 true 로 설정했습니다.")
    elif load_yaml("broker_fields.yaml").get("verified_by_mock") is not True:
        print("ℹ️  verified_by_mock 은 false 유지(승격 심사·실거래 잠금). 모두 통과 시 --send --apply 로 갱신.")


if __name__ == "__main__":
    main()
