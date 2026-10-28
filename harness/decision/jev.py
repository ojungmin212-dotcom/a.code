"""TypeSafe Jev (System One) HTTP 클라이언트.

형식 근거: POST /v1/systemone {state, model, questions} → {model, answers, usage}.
실패는 전부 JevError 로 올린다 — 호출자는 HOLD 로 처리한다. 재시도하지 않는다(장중 판단은 신선도가 우선).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.typesafe.ai"


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None,
                 timeout_sec: float = 3.0, post: Callable[..., Any] | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_sec = timeout_sec
        self._post = post or requests.post

    def ask(self, state: Any, questions: dict[str, dict]) -> dict:
        """answers 와 응답 모델 버전·사용량을 담은 원본 dict 를 돌려준다. 형식 검증 포함."""
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY 가 없습니다")
        try:
            r = self._post(f"{self.base_url}/v1/systemone",
                           headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"state": state, "model": self.model, "questions": questions},
                           timeout=self.timeout_sec)
        except requests.RequestException as e:
            raise JevError(f"네트워크 오류: {e}") from e
        if not r.ok:
            raise JevError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError as e:
            raise JevError("JSON 아님") from e
        _validate(data, questions)
        return data


def _validate(data: Any, questions: dict[str, dict]) -> None:
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise JevError("answers 없음")
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise JevError(f"{qid}: 답 누락 또는 타입 불일치")
        if q["type"] == "noul":
            v = a.get("noul")
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise JevError(f"{qid}: noul 값 이상")
        elif q["type"] == "choice":
            probs = a.get("probabilities")
            if not isinstance(probs, dict) or set(probs) != set(q["criteria"]):
                raise JevError(f"{qid}: choice 확률 이상")
        elif q["type"] == "score":
            if not isinstance(a.get("score"), (int, float)):
                raise JevError(f"{qid}: score 값 이상")
