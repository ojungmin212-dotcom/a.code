"""승격 심사만 실행 (실거래 시작 없음, P3·P4 모두).  python scripts/promotion_check.py"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.audit import AuditLog  # noqa: E402
from harness.bootstrap import fields_ready  # noqa: E402
from harness.models import Market  # noqa: E402
from harness.promotion import PromotionCriteria, evaluate, evaluate_p4  # noqa: E402

if __name__ == "__main__":
    c = PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml")
    now = datetime.now(timezone.utc)
    jev = AuditLog(ROOT / "data" / "audit-jev.sqlite")
    print("[P2→P3]\n" + evaluate(jev, AuditLog(ROOT / "data" / "audit-baseline.sqlite"), c, now,
                                  fields_ready()).render())
    print("\n[P3→P4]\n" + evaluate_p4(AuditLog(ROOT / "data" / "audit-live.sqlite"), jev, c.p4, now,
                                      fields_ready((Market.KR, Market.US))).render())
