"""Grok + Jev 장중 매매 하네스."""
from pathlib import Path as _Path

# .env 를 모든 진입점에서 먼저 읽는다. (이전에는 nhplug 를 import 하는 경로에서만 로드돼,
# run_research.py 처럼 nhplug 를 쓰지 않는 스크립트는 XAI_API_KEY 를 못 읽었다.)
# 이미 설정된 환경변수가 우선한다(override=False).
try:
    from dotenv import load_dotenv as _load

    _load(_Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # python-dotenv 는 nhplug 의존성이라 보통 설치돼 있다
    pass
