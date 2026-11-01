# 이 저장소에서 작업하는 AI 에이전트 규칙

## 🔒 기능 동결 (X1, 2026-11-01 ~)
**첫 실서버 점검(`scripts/check_all.py --send --apply` 통과) + 모의 운영 2주 데이터가 쌓이기 전까지 새 기능을 추가하지 않는다.**

허용:
- 버그 수정, 실측 결과에 따른 설정·필드 수정
- 검증 강화: 테스트(성질 기반·시뮬레이션), CI, 린트·타입 오류 수정, 리뷰 지적 수정
- 문서

금지(동결 해제 전):
- 새 정책·새 봇·새 시장·새 외부 서비스 연동, 판단 기준값 튜닝
- `docs/review-v2.md` 2순위 이후 항목(G2·G3·G6·G10·G11·G12 등)

해제 조건: `config/broker_fields.yaml`·`feed.yaml` 의 `verified_by_mock: true` **그리고** 모의 운영 10거래일 이상.
해제는 사용자가 명시적으로 지시할 때만 한다.

## 절대 규칙 (설계서 §3 요약)
- LLM·Jev 는 제안만, 주문 수량·한도는 코드(`harness/risk/gate.py`)만 결정한다.
- `config/risk*.yaml` · `config/promotion.yaml` 의 한도·기준을 사용자 지시 없이 바꾸지 않는다.
- 실거래 잠금(승격 심사 · `HARNESS_ALLOW_LIVE` · `allow_live` · 확인 문구)을 우회하는 코드를 만들지 않는다.
- NH 응답 성공을 `rsp_cd` 비교로 판정하지 않는다(SDK 규약). 주문번호·잔고로 확인한다.

## 작업 전후 확인
```bash
ruff check harness scripts tests
pyright
python -m pytest -q --cov=harness --cov-fail-under=85
```
