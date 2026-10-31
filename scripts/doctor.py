"""자가 점검(F10·F16) — 네트워크 호출 없이 설정·보안·상태를 한 번에 확인한다.

    python scripts/doctor.py

❌ 가 하나라도 있으면 종료 코드 1. 연결·필드 실측은 scripts/check_all.py 가 맡는다.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import harness  # noqa: E402,F401  (.env 로드)
from harness.audit import AuditLog  # noqa: E402
from harness.bootstrap import fields_ready, load_yaml, p4_enabled  # noqa: E402
from harness.decision import StrategyConfig  # noqa: E402
from harness.engine import unreconciled_symbols  # noqa: E402
from harness.models import Market  # noqa: E402
from harness.promotion import PromotionCriteria  # noqa: E402
from harness.research.service import ResearchConfig  # noqa: E402
from harness.risk import SESSIONS, RiskLimits  # noqa: E402

fails = 0


def show(level: str, name: str, detail: str) -> None:
    global fails
    fails += level == "❌"
    print(f"{level} {name}: {detail}")


def main() -> int:
    now = datetime.now(timezone.utc)
    v = sys.version_info
    show("✅" if v >= (3, 12) else "❌", "Python", f"{v.major}.{v.minor}")
    for mod, need in (("nhplug", True), ("exchange_calendars", False), ("FinanceDataReader", False)):
        try:
            __import__(mod)
            show("✅", f"패키지 {mod}", "설치됨")
        except ImportError:
            show("❌" if need else "⚠️", f"패키지 {mod}", "미설치 — pip install -e .")

    # ---- 보안(F16)
    env = ROOT / ".env"
    if not env.exists():
        show("❌", ".env", "없음 — cp .env.example .env")
    else:
        if os.name == "posix":
            mode = env.stat().st_mode
            loose = mode & (stat.S_IRWXG | stat.S_IRWXO)
            show("⚠️" if loose else "✅", ".env 권한", f"{oct(mode & 0o777)}" + (" — chmod 600 .env 권장" if loose else ""))
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT,
                                 capture_output=True).returncode == 0
        show("❌" if tracked else "✅", ".env git 추적", "추적 중! git rm --cached .env" if tracked else "추적 안 됨")
    ignored = subprocess.run(["git", "check-ignore", "-q", "data/x"], cwd=ROOT).returncode == 0
    show("✅" if ignored else "❌", "data/ gitignore", "감사 로그·계좌 원문이 커밋되지 않음" if ignored else "data/ 가 무시되지 않음")
    cache = Path(os.environ.get("NHPLUG_TOKEN_CACHE_DIR") or Path.home() / ".nhplug")
    if os.name == "posix" and cache.exists():
        loose = cache.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        show("⚠️" if loose else "✅", "토큰 캐시 권한", f"{cache} {oct(cache.stat().st_mode & 0o777)}"
             + (f" — chmod 700 {cache} 권장(24시간 접근토큰 보관)" if loose else ""))
    for k, need in (("NHPLUG_APP_KEY", True), ("NHPLUG_APP_SECRET", True), ("NHPLUG_DEFAULT_ACCOUNT", True),
                    ("TYPESAFE_API_KEY", True), ("XAI_API_KEY", True), ("TELEGRAM_BOT_TOKEN", False)):
        ok = bool(os.environ.get(k))
        show("✅" if ok else ("❌" if need else "⚠️"), k, "설정됨" if ok else "없음")
    base = os.environ.get("NHPLUG_BASE_URL", "https://api.nhplug.com:8443(SDK 기본=운영!)")
    show("✅" if "moapi." in base else "⚠️", "NHPLUG_BASE_URL", base + ("" if "moapi." in base else " — 실거래 서버"))
    live = os.environ.get("HARNESS_ALLOW_LIVE")
    show("⚠️" if live else "✅", "HARNESS_ALLOW_LIVE", "설정됨(실거래 허용)" if live else "없음")

    # ---- 설정
    try:
        RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
        RiskLimits.from_yaml(ROOT / "config" / "risk.live.yaml")
        cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
        rcfg = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
        PromotionCriteria.from_yaml(ROOT / "config" / "promotion.yaml")
        show("✅", "설정 파일", f"검증 통과 · 진입 {cfg.execution.entry_order} · 섀도 {'켜짐' if cfg.shadow and cfg.shadow.enabled else '꺼짐'}")
    except Exception as e:
        show("❌", "설정 파일", str(e)[:200])
        return 1
    show("✅" if fields_ready() else "⚠️", "필드 실측 확인", "verified_by_mock=true" if fields_ready()
         else "미확인 — 모의 운영은 가능, 승격·실거래 잠금 (check_all.py --send --apply)")
    show("✅", "미국(P4)", "보류(D1)" if not p4_enabled() else "활성")

    # ---- 상태
    kr = SESSIONS[Market.KR]
    show("✅", "오늘 국내장", ("거래일 " + f"{kr.bounds(now)[0]:%H:%M}~{kr.bounds(now)[1]:%H:%M}")
         if kr.is_trading_day(now) else "휴장")
    if (ROOT / "data" / "KILL").exists():
        show("⚠️", "킬 스위치", "data/KILL 존재 — 시작하면 즉시 정지")
    for db in sorted((ROOT / "data").glob("audit-*.sqlite")):
        a = AuditLog(db)
        left = unreconciled_symbols(a)
        halted = [e for e in a.events() if e["kind"] in ("halt", "resume")]
        state = "정지 중" if halted and halted[-1]["kind"] == "halt" else "정상"
        show("❌" if left else ("⚠️" if state != "정상" else "✅"), db.name,
             f"미해소 {left}" if left else state)
        if db.name == "audit-research.sqlite":
            from harness.research.budget import BudgetGuard
            spent = BudgetGuard(a, rcfg.monthly_budget_krw, rcfg.usd_krw).spent_krw(now)
            show("⚠️" if spent > rcfg.monthly_budget_krw * 0.8 else "✅", "리서치 예산",
                 f"{spent:,.0f} / {rcfg.monthly_budget_krw:,.0f}원")
    free = shutil.disk_usage(ROOT).free / 2**30
    show("✅" if free > 1 else "⚠️", "디스크 여유", f"{free:.1f}GB (틱 기록은 하루 수 MB)")

    print(f"\n{'❌ ' + str(fails) + '건 해결 필요' if fails else '✅ 시작 가능'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
