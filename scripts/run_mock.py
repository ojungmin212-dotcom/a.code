"""모의투자 장중 운영 (국내). 기본은 dry_run — 주문은 --send 일 때만 모의 서버로 전송.

    python scripts/run_mock.py data/theses.sqlite --send                    # Jev 정책
    python scripts/run_mock.py data/theses.sqlite --send --policy baseline  # 비교군 (별도 프로세스)
    python scripts/run_mock.py config/theses.yaml                           # 사람이 쓴 논지, dry_run

긴급 정지: data/KILL 파일을 만들면 신규 진입 중단 + 전량 청산.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.bootstrap import StartupBlocked, assemble  # noqa: E402
from harness.models import OrderType  # noqa: E402
from harness.runner import run_loop  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("theses")
    p.add_argument("--send", action="store_true")
    p.add_argument("--policy", choices=["jev", "baseline"], default="jev")
    a = p.parse_args()
    try:
        asm = assemble(mode="mock", theses_path=a.theses, policy_name=a.policy, risk_file="risk.yaml",
                       send=a.send, audit_name=f"audit-{a.policy}.sqlite",
                       entry_order_type=OrderType.MARKET)
    except StartupBlocked as e:
        sys.exit(f"시작 거부: {e}")
    if not asm.broker.is_mock():
        sys.exit("run_mock.py 는 모의투자 서버 전용입니다 (NHPLUG_BASE_URL 확인)")
    asm.engine.notify(f"시작 — 전송={'모의' if a.send else 'dry_run'} 종목={asm.loop.symbols}")
    run_loop(asm.engine, asm.broker, asm.loop)


if __name__ == "__main__":
    main()
