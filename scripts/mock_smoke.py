"""모의투자 연결 점검. 기본은 조회 + 드라이런만 하고 주문은 보내지 않는다.

    python scripts/mock_smoke.py                    # 계좌·잔고 조회, 주문 payload 확인
    python scripts/mock_smoke.py --send 005930 1    # 모의투자로 1주 시장가 매수 전송

일괄 점검은 scripts/check_all.py 를 쓴다(필드 자동 판정·verified_by_mock 갱신).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.broker.nhplug_broker import NHPlugBroker  # noqa: E402
from harness.models import Market, OrderRequest, OrderType, Side  # noqa: E402


def show(title: str, data: object) -> None:
    print(f"\n=== {title}")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str)[:3000])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--send", nargs=2, metavar=("SYMBOL", "QTY"))
    args = p.parse_args()

    account = os.environ.get("NHPLUG_DEFAULT_ACCOUNT", "")
    broker = NHPlugBroker(account, dry_run=args.send is None)
    print("모의투자 서버:", broker.is_mock())

    show("계좌 목록 (acct_type 03 = 모의투자)", broker.raw_accounts())
    show("국내 잔고", broker.raw_kr_balance())

    symbol, qty = args.send or ("005930", "1")
    req = OrderRequest(client_order_id=uuid.uuid4().hex, market=Market.KR, symbol=symbol,
                       side=Side.BUY, quantity=int(qty), order_type=OrderType.MARKET)
    result = broker.place_order(req)
    show(f"주문 결과 ({result.status})", result.model_dump())


if __name__ == "__main__":
    main()
