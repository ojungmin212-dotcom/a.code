"""모의투자 장중 운영 — 한 프로세스에서 Jev·비교군을 같은 시세로 동시에 운영(F9).

    python scripts/run_mock.py data/theses.sqlite --send       # Jev → NH 모의계좌, 비교군 → 메모리 모의체결
    python scripts/run_mock.py data/theses.sqlite              # dry_run (주문 전송 없음)
    python scripts/run_mock.py config/theses.yaml --policies baseline --broker-policy baseline

- 브로커(NH 모의계좌)로 주문하는 정책은 하나뿐이다(같은 계좌 잔고 혼선 방지). 나머지는 메모리 모의체결.
- Jev 레인은 섀도 판단(config/strategy.yaml shadow)을 함께 기록한다.
- 긴급 정지: data/KILL 파일 생성 → 모든 레인 신규 진입 중단 + 전량 청산.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.bootstrap import StartupBlocked, assemble  # noqa: E402
from harness.models import Market, OrderType  # noqa: E402
from harness.runner import run_loop  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("theses")
    p.add_argument("--send", action="store_true")
    p.add_argument("--policies", default="jev,baseline")
    p.add_argument("--broker-policy", default="jev")
    p.add_argument("--markets", default="KR", help="KR (미국은 D1 보류)")
    a = p.parse_args()
    markets = tuple(Market(m.strip()) for m in a.markets.split(","))
    policies = [x.strip() for x in a.policies.split(",") if x.strip()]
    try:
        asm = assemble(mode="mock", theses_path=a.theses, policies=policies,
                       broker_policy=a.broker_policy, risk_file="risk.yaml", send=a.send,
                       entry_order_type=OrderType.MARKET, markets=markets)
    except StartupBlocked as e:
        sys.exit(f"시작 거부: {e}")
    if not asm.broker_lane.broker.is_mock():
        sys.exit("run_mock.py 는 모의투자 서버 전용입니다 (NHPLUG_BASE_URL 확인)")
    for ln in asm.lanes:
        ln.engine.notify(f"시작 — 전송={'모의' if a.send and not ln.is_paper else ('메모리' if ln.is_paper else 'dry_run')}"
                         f" 섀도={'켜짐' if ln.engine.shadow else '꺼짐'} 종목={asm.loop.symbols}")
    run_loop(asm.lanes, asm.loop)


if __name__ == "__main__":
    main()
