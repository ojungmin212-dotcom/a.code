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

## P1 구성 (국내 빠른 루프)

| 모듈 | 내용 |
|---|---|
| `harness/market/` | 틱→1분봉 집계, 지표(수익률·MA20·VWAP·ATR·거래량비), 나무 실시간 체결가 피드 |
| `harness/research/thesis.py` | 매매 논지(사람이 YAML 작성, P2에서 Grok 이 대체) |
| `harness/decision/jev.py` | Jev(System One) 클라이언트 — 실패 시 HOLD, 재시도 없음 |
| `harness/decision/policy.py` | Jev 정책(원자 질문 3개 + 코드 결합) / 비교군 규칙 정책, KRX 호가단위 |
| `harness/engine.py` | 1분봉마다 청산 감시 → 진입 판단 → 리스크 게이트 → 주문, 재시작 시 장부 복원 |
| `harness/audit.py` | SQLite 감사 로그 + 재시작에도 유지되는 중복주문 방지 |
| `harness/replay.py` | CSV 1분봉 리플레이(배선 점검·정책 비교용, 성과 추정 아님) |

```bash
python scripts/jev_ping.py                                   # Jev 연결 점검
python scripts/capture_feed.py 005930                        # 장중: 실시간 푸시 필드명 확인 → config/feed.yaml
python scripts/run_replay.py data/bars.csv config/theses.yaml --jev
python scripts/run_mock.py config/theses.yaml                # 장중 운영(dry_run)
python scripts/run_mock.py config/theses.yaml --send --order-no-field <필드명>   # 모의투자 전송
python scripts/run_mock.py config/theses.yaml --policy baseline                  # 비교군 (별도 프로세스)
```

## P2 구성 (Grok 리서치 루프 + 평가)

| 모듈 | 내용 |
|---|---|
| `harness/research/grok.py` | xAI Responses API 클라이언트 (web_search·x_search 도구 + JSON 스키마 출력) |
| `harness/research/bots.py` | 감성(X·뉴스) / 펀더멘털 / 반대논리 봇 |
| `harness/research/aggregate.py` | 코드 결합: AVOID·반대논리 거부권, 가중 점수, 논지 유효시간(장 마감 넘지 않음) |
| `harness/research/budget.py` | 월 예산 상한 — 초과 시 리서치 중단 → 논지 없음 → 매매 없음 |
| `harness/research/repo.py` | 논지 공유 저장소 — Jev·비교군 프로세스가 같은 논지를 읽음 |
| `harness/evaluation.py` | 승률·손익비·최대낙폭, Jev 확률 보정(Brier) |

```bash
python scripts/grok_ping.py                       # Grok 연결·응답 형식 점검(검색 없음, 저비용)
python scripts/run_research.py                    # 리서치 상주(장 전 1회 + 선택적 장중 갱신)
python scripts/run_mock.py data/theses.sqlite                       # Jev 정책
python scripts/run_mock.py data/theses.sqlite --policy baseline     # 비교군
python scripts/report.py data/audit-jev.sqlite data/audit-baseline.sqlite
```

## 알려진 한계
- 주문번호 응답 필드명 미확인 → 확인 전까지 모든 실제 전송은 `UNKNOWN` 처리
- 잔고 응답 파싱 미구현 (원본만 조회)
- 미국 주식 주문 미지원 (해외 매도 경로 미검증)
- 휴장일·조기폐장 미반영
- 실시간 체결가 푸시의 가격·수량 필드명 미확인 → `config/feed.yaml` 이 비어 있으면 `run_mock.py` 가 시작을 거부
- 체결통보(d2) 미연동 → 실제 전송 주문은 전부 '대조 필요'로 묶이고 사람이 확인해야 다음 주문 가능
- 봉 시각은 수신 시각 기준, 거래 없는 분은 봉이 생기지 않음
- 리플레이 청산은 봉 종가 체결로 근사 (갭·슬리피지 무시)
- Grok 요청·응답 형식과 모델명(grok-4.7)은 공식 문서를 직접 확인하지 못함 → `grok_ping.py` 로 확인 필요
- 리서치 비용: 2종목 장 전 1회 추정 약 900원 → 월 1만원 예산으로는 약 11거래일분
- Jev 시장 장세 질문은 지수가 아닌 해당 종목 봉으로 판단 (지수 피드 미연동)
