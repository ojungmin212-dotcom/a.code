"""브로커 화면으로 확인한 결과불명 주문을 해소 처리 (감사 로그에 기록).

    python scripts/resolve_pending.py data/audit-jev.sqlite KR 005930 "HTS 확인: 미체결 취소됨"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.audit import AuditLog  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 5 or not sys.argv[4].strip():
        sys.exit(__doc__)
    db, market, symbol, note = sys.argv[1:]
    AuditLog(db).record("pending_resolved", {"note": note}, market=market, symbol=symbol)
    print("기록 완료. ⚠️ 실제로 체결된 수량이 있었다면 해당 포지션을 HTS 에서 직접 정리하세요.")
