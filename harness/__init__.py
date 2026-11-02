"""Grok + Jev 장중 매매 하네스."""
import sys as _sys
from pathlib import Path as _Path


def _force_utf8_output() -> None:
    """I1: 표준출력·오류를 UTF-8 로 고정한다.

    Windows 에서 출력을 파일로 돌리면(작업 스케줄러 로그 등) 기본 인코딩 cp949 가 알림 이모지(✅🛑💓)를
    쓰지 못해 UnicodeEncodeError 가 나고, 알림을 보내던 레인이 정지된다. 그래도 못 쓰는 문자는 '?' 로 대체한다.
    """
    for stream in (_sys.stdout, _sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 이미 쓰기가 시작된 스트림 등 — 출력 실패보다 계속 실행이 중요
            pass


_force_utf8_output()

# .env 를 모든 진입점에서 먼저 읽는다. (이전에는 nhplug 를 import 하는 경로에서만 로드돼,
# run_research.py 처럼 nhplug 를 쓰지 않는 스크립트는 XAI_API_KEY 를 못 읽었다.)
# 이미 설정된 환경변수가 우선한다(override=False).
try:
    from dotenv import load_dotenv as _load

    _load(_Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # python-dotenv 는 nhplug 의존성이라 보통 설치돼 있다
    pass
