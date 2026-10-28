"""P3 실거래 (국내, 20만원 한도: config/risk.live.yaml).

시작 조건 — 하나라도 어긋나면 거부:
  1. 승격 심사 통과 (config/promotion.yaml, 모의 운영 감사 로그 기준)
  2. 브로커·시세 필드 확정 (config/broker_fields.yaml · feed.yaml) → 잔고 대조 활성
  3. 운영 서버 + HARNESS_ALLOW_LIVE=I_ACCEPT_REAL_MONEY
  4. 이전 실행의 미청산·결과불명 주문 없음
  5. 터미널에서 확인 문구 직접 입력

    python scripts/run_live.py data/theses.sqlite

긴급 정지: data/KILL 파일 생성 → 신규 진입 중단 + 전량 청산.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.audit import AuditLog  # noqa: E402
from harness.bootstrap import StartupBlocked, assemble, fields_ready  # noqa: E402
from harness.models import OrderType  # noqa: E402
from harness.promotion import PromotionCriteria, evaluate  # noqa: E402
from harness.runner import run_loop  # noqa: E402

CONFIRM = "실거래 시작"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    jev_db, base_db = ROOT / "data" / "audit-jev.sqlite", ROOT / "data" / "audit-baseline.sqlite"
    if not jev_db.exists() or not base_db.exists():
        sys.exit("모의 운영 감사 로그가 없습니다 (data/audit-jev.sqlite, audit-baseline.sqlite)")
    result = evaluate(AuditLog(jev_db), AuditLog(base_db),
                      PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml"),
                      datetime.now(timezone.utc), fields_ready())
    print(result.render())
    if not result.passed:
        sys.exit("승격 기준 미달 — 실거래를 시작하지 않습니다.")

    try:
        asm = assemble(mode="live", theses_path=sys.argv[1], policy_name="jev",
                       risk_file="risk.live.yaml", send=True, audit_name="audit-live.sqlite",
                       entry_order_type=OrderType.MARKET, allow_live=True)
    except StartupBlocked as e:
        sys.exit(f"시작 거부: {e}")
    # LiveTradingBlocked(운영 서버 아님/환경변수 없음)는 assemble 안에서 그대로 올라온다.
    if asm.broker.is_mock():
        sys.exit("실거래는 운영 서버(api.)에서만 실행합니다")
    if asm.broker.balance_fields is None:
        sys.exit("잔고 필드 미설정 — 체결 확인 없이 실거래할 수 없습니다")

    limits = asm.engine.gate.limits
    print(f"\n실거래 한도: 자본 {limits.initial_capital_krw:,}원 · 총손실 {limits.max_total_loss_krw:,}원 · "
          f"일일 {limits.max_daily_loss_krw:,}원 · 1회 {limits.max_loss_per_trade_krw:,}원 · 종목 {asm.loop.symbols}")
    if input(f"계속하려면 '{CONFIRM}' 을 입력: ").strip() != CONFIRM:
        sys.exit("취소")
    asm.engine.notify("실거래 시작")
    run_loop(asm.engine, asm.broker, asm.loop)


if __name__ == "__main__":
    main()
