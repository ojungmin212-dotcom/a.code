"""승격 심사만 실행 (실거래 시작 없음).  python scripts/promotion_check.py"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.audit import AuditLog  # noqa: E402
from harness.bootstrap import fields_ready  # noqa: E402
from harness.promotion import PromotionCriteria, evaluate  # noqa: E402

if __name__ == "__main__":
    r = evaluate(AuditLog(ROOT / "data" / "audit-jev.sqlite"),
                 AuditLog(ROOT / "data" / "audit-baseline.sqlite"),
                 PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml"),
                 datetime.now(timezone.utc), fields_ready())
    print(r.render())
