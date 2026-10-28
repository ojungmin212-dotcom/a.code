"""정지 해제(수동). 원인을 해결했다는 사유를 반드시 남긴다.

    python scripts/resume.py data/audit-live.sqlite "장부 불일치 원인: HTS 수동매매. 정리 완료"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.audit import AuditLog  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 3 or len(sys.argv[2].strip()) < 10:
        sys.exit(__doc__)
    AuditLog(sys.argv[1]).record("resume", {"note": sys.argv[2]})
    print("재개 기록 완료. ⚠️ 총 손실 한도 도달로 인한 정지는 손실이 그대로라 다시 정지됩니다.")
