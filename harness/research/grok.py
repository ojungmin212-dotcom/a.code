"""xAI Grok Responses API 클라이언트 (서버측 검색 도구 + JSON 스키마 구조화 출력).

형식 근거(검색으로 확인, 공식 문서 직접 확인 못 함):
  POST https://api.x.ai/v1/responses
  {model, input:[{role,content}], tools:[{type:web_search},{type:x_search}],
   text:{format:{type:json_schema, name, schema, strict:true}}}
응답 텍스트 위치가 확인되지 않아 output_text(최상위) → output[].content[].output_text 순으로 찾는다.
실패는 전부 GrokError. 재시도하지 않는다(비용 중복 방지).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.x.ai"


class GrokError(RuntimeError):
    pass


@dataclass
class GrokResult:
    data: dict
    usage: dict
    citations: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _extract_text(data: dict) -> str | None:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]
    text = None
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str):
                text = c["text"]  # 마지막 메시지가 최종 답
    return text


def _extract_citations(data: dict) -> list[str]:
    urls: list[str] = []
    for u in data.get("citations") or []:
        if isinstance(u, str):
            urls.append(u)
        elif isinstance(u, dict) and isinstance(u.get("url"), str):
            urls.append(u["url"])
    for item in data.get("output") or []:
        for c in (item.get("content") or []) if isinstance(item, dict) else []:
            for a in (c.get("annotations") or []) if isinstance(c, dict) else []:
                if isinstance(a, dict) and isinstance(a.get("url"), str):
                    urls.append(a["url"])
    return list(dict.fromkeys(urls))


class GrokClient:
    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None,
                 timeout_sec: float = 120, post: Callable[..., Any] | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        self.base_url = (base_url or os.environ.get("XAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_sec = timeout_sec
        self._post = post or requests.post

    def ask(self, system: str, user: str, schema: dict, schema_name: str,
            tools: list[str] | None = None) -> GrokResult:
        if not self.api_key:
            raise GrokError("XAI_API_KEY 가 없습니다")
        body: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema,
                                "strict": True}},
        }
        if tools:
            body["tools"] = [{"type": t} for t in tools]
        try:
            r = self._post(f"{self.base_url}/v1/responses",
                           headers={"Authorization": f"Bearer {self.api_key}"},
                           json=body, timeout=self.timeout_sec)
        except requests.RequestException as e:
            raise GrokError(f"네트워크 오류: {e}") from e
        if not r.ok:
            raise GrokError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError as e:
            raise GrokError("JSON 아님") from e
        text = _extract_text(data)
        if text is None:
            raise GrokError("응답에서 출력 텍스트를 찾지 못함")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise GrokError(f"구조화 출력 파싱 실패: {text[:200]}") from e
        if not isinstance(parsed, dict):
            raise GrokError("구조화 출력이 객체가 아님")
        return GrokResult(parsed, data.get("usage") or {}, _extract_citations(data), data)
