"""실거래. P3 = 국내 20만원(config/risk.live.yaml), P4 = 국내+미국 50만원(config/risk.yaml).

시작 조건 — 하나라도 어긋나면 거부:
  1. 승격 심사 통과 (config/promotion.yaml, 모의 운영 감사 로그 기준)
  2. 브로커·시세 필드 확정 (config/broker_fields.yaml · feed.yaml) → 잔고 대조 활성
  3. 운영 서버 + HARNESS_ALLOW_LIVE=I_ACCEPT_REAL_MONEY
  4. 이전 실행의 미청산·결과불명 주문 없음
  5. 터미널에서 확인 문구 직접 입력

    python scripts/run_live.py data/theses.sqlite              # P3 (국내)
    python scripts/run_live.py data/theses.sqlite --p4         # P4 (국내+미국, P4 승격 심사 추가)

긴급 정지: data/KILL 파일 생성 → 신규 진입 중단 + 전량 청산.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.audit import AuditLog  # noqa: E402
from harness.bootstrap import StartupBlocked, assemble, fields_ready, p4_enabled  # noqa: E402
from harness.models import Market, OrderType  # noqa: E402
from harness.promotion import PromotionCriteria, evaluate, evaluate_p4  # noqa: E402
from harness.runner import run_loop  # noqa: E402

CONFIRM = "실거래 시작"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--p4"]
    p4 = "--p4" in sys.argv[1:]
    if len(args) != 1:
        sys.exit(__doc__)
    markets = (Market.KR, Market.US) if p4 else (Market.KR,)
    if p4 and not p4_enabled():
        sys.exit("미국(P4)은 보류 중입니다(D1). config/promotion.yaml 의 p4_enabled 참고")
    criteria = PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml")
    now = datetime.now(timezone.utc)

    jev_db, base_db = ROOT / "data" / "audit-jev.sqlite", ROOT / "data" / "audit-baseline.sqlite"
    if not jev_db.exists() or not base_db.exists():
        sys.exit("모의 운영 감사 로그가 없습니다 (data/audit-jev.sqlite, audit-baseline.sqlite)")
    result = evaluate(AuditLog(jev_db), AuditLog(base_db), criteria, now, fields_ready((Market.KR,)))
    print("[P2→P3 심사]\n" + result.render())
    if not result.passed:
        sys.exit("승격 기준 미달 — 실거래를 시작하지 않습니다.")
    if p4:
        live_db = ROOT / "data" / "audit-live.sqlite"
        if not live_db.exists():
            sys.exit("국내 실거래(P3) 감사 로그가 없습니다")
        r4 = evaluate_p4(AuditLog(live_db), AuditLog(jev_db), criteria.p4, now, fields_ready(markets))
        print("\n[P3→P4 심사]\n" + r4.render())
        if not r4.passed:
            sys.exit("P4 승격 기준 미달 — 국내만 운영하려면 --p4 없이 실행하세요.")

    try:
        # Jev 만 실계좌로 주문, 비교군은 같은 시세로 메모리 모의체결(실거래 중에도 비교 지속)
        asm = assemble(mode="live", theses_path=args[0], policies=["jev", "baseline"],
                       broker_policy="jev", risk_file="risk.yaml" if p4 else "risk.live.yaml",
                       send=True, entry_order_type=OrderType.MARKET, markets=markets,
                       allow_live=True, audit_names={"jev": "audit-live.sqlite",
                                                     "baseline": "audit-live-baseline.sqlite"})
    except StartupBlocked as e:
        sys.exit(f"시작 거부: {e}")
    # LiveTradingBlocked(운영 서버 아님/환경변수 없음)는 assemble 안에서 그대로 올라온다.
    live = asm.broker_lane
    if live.broker.is_mock():
        sys.exit("실거래는 운영 서버(api.)에서만 실행합니다")
    if live.broker.balance_fields is None:
        sys.exit("잔고 필드 미설정 — 체결 확인 없이 실거래할 수 없습니다")

    limits = live.engine.gate.limits
    print(f"\n실거래 한도: 자본 {limits.initial_capital_krw:,}원 · 총손실 {limits.max_total_loss_krw:,}원 · "
          f"일일 {limits.max_daily_loss_krw:,}원 · 1회 {limits.max_loss_per_trade_krw:,}원 · 종목 {asm.loop.symbols}")
    if input(f"계속하려면 '{CONFIRM}' 을 입력: ").strip() != CONFIRM:
        sys.exit("취소")
    live.engine.notify("실거래 시작")
    run_loop(asm.lanes, asm.loop)


if __name__ == "__main__":
    main()
