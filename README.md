# a.code — Grok + Jev 장중 매매 하네스

설계: [docs/harness-design.md](docs/harness-design.md)

## P0 구성 (현재)

| 모듈 | 내용 |
|---|---|
| `harness/models.py` | 진입 신호·주문·포지션 스키마 (pydantic, Decimal) |
| `harness/risk/` | 리스크 게이트(결정론적), 손익 장부, 장 시간(서머타임 반영) |
| `harness/broker/paper.py` | 메모리 모의 브로커 |
| `harness/broker/nhplug_broker.py` | 나무 PLUG 어댑터 — **모의투자 서버 전용, dry_run 기본** |
| `config/risk.yaml` | 리스크 한도 (50만원 / 최대 손실 10만원) |
| `scripts/mock_smoke.py` | 모의투자 연결 점검 |

```bash
pip install -e ".[dev]"
pytest
cp .env.example .env   # 앱키·모의계좌 입력
python scripts/mock_smoke.py              # 조회 + 드라이런
python scripts/mock_smoke.py --send 005930 1   # 모의투자 1주 매수 → 응답에서 주문번호 필드명 확인
```

## 알려진 한계 (P1에서 해결)
- 주문번호 응답 필드명 미확인 → 확인 전까지 모든 실제 전송은 `UNKNOWN` 처리
- 잔고 응답 파싱 미구현 (원본만 조회)
- 미국 주식 주문 미지원 (해외 매도 경로 미검증)
- 휴장일·조기폐장 미반영
- 중복 주문 방지가 프로세스 메모리 기준 (재시작 시 초기화)
