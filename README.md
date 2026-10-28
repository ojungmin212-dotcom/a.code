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

## P3 구성 (국내 실거래 20만원)

| 모듈 | 내용 |
|---|---|
| `harness/promotion.py` | 승격 심사 — 모의 운영 6주·100회·비용 반영 손익>0·비교군 우위·Jev 보정 skill>0·미해소 없음 |
| `harness/engine.py` `reconcile()` | 브로커 잔고로 결과불명 주문 확정, 하네스 종목의 장부 불일치 시 정지 |
| `harness/runner.py` · `bootstrap.py` | 모의·실거래 공용 운영 루프와 조립, 킬 스위치(`data/KILL`) |
| `harness/notify.py` | 콘솔 + 텔레그램 알림(체결·청산·정지) |
| `config/risk.live.yaml` | 실거래 한도: 총손실 4만·일일 6천·1회 2천원, 1종목, 미국 금지 |

```bash
python scripts/promotion_check.py                 # 승격 심사만
python scripts/run_live.py data/theses.sqlite     # 심사 통과 + HARNESS_ALLOW_LIVE + 확인 문구 입력 시에만 시작
touch data/KILL                                   # 긴급 정지(신규 진입 중단 + 전량 청산)
python scripts/resume.py data/audit-live.sqlite "정지 원인과 조치 내용"
```

## P4 구성 (국내 + 미국, 50만원)

| 모듈 | 내용 |
|---|---|
| `harness/broker/nhplug_broker.py` | 미국 매수·매도·잔고 — **매도 경로 미설정 시 미국 주문 전체 비활성** |
| `harness/market/instruments.py` | 티커 → GIC 15자리(해외 실시간 구독키), NH 종목마스터 사용 |
| `harness/market/fx.py` | USD/KRW 수동 설정 + 신선도 검사(기본 3일) — 낡으면 미국 신규 진입 중단 |
| `harness/runner.py` | 한 프로세스가 국내·미국 정규장을 번갈아 운영(손실 장부 공유), 두 시장이면 상주 |
| `harness/promotion.py` `evaluate_p4` | 국내 실거래 4주·20회·슬리피지 중앙값 30bp 이하·정지 0회 + 미국 모의 4주·30회·손익>0 |

```bash
python scripts/capture_feed.py AAPL --us                          # 미국 장중: RC 푸시 필드 확인
python scripts/run_mock.py data/theses.sqlite --send --markets KR,US   # 미국 포함 모의 운영(상주)
python scripts/run_live.py data/theses.sqlite --p4                # P3·P4 심사 통과 시에만
```

## 알려진 한계
- 주문번호 응답 필드명 미확인 → 확인 전까지 모든 실제 전송은 `UNKNOWN` 처리
- 잔고 응답 파싱 미구현 (원본만 조회)
- 미국 주식 주문 미지원 (해외 매도 경로 미검증)
- 휴장일·조기폐장 미반영 (미국 추수감사절 다음날 등 조기폐장일에 마감 청산이 늦을 수 있음)
- 미국: 매도 경로·잔고 필드·RC 푸시 필드 미확인, 소수점 주문 미지원(25만원 한도로 1주가 비싼 종목은 거래 불가), 환율 수동 갱신
- 실시간 체결가 푸시의 가격·수량 필드명 미확인 → `config/feed.yaml` 이 비어 있으면 `run_mock.py` 가 시작을 거부
- 체결 확인은 잔고 대조 방식(5초 주기) — 잔고에는 체결가가 없어 매도 손익은 직전 시세로 추정. 체결통보(d2) 미연동
- 주문 취소 API 미구현 → 진입·청산 모두 시장가 주문 사용(지정가 미체결 잔류 위험 회피, 대신 슬리피지 감수)
- 봉 시각은 수신 시각 기준, 거래 없는 분은 봉이 생기지 않음
- 리플레이 청산은 봉 종가 체결로 근사 (갭·슬리피지 무시)
- Grok 요청·응답 형식과 모델명(grok-4.7)은 공식 문서를 직접 확인하지 못함 → `grok_ping.py` 로 확인 필요
- 리서치 비용: 1종목(삼성전자) 장 전 1회 추정 약 450원 → 월 약 9,900원으로 1만원 예산에 빠듯함. 추정 단가는 실측으로 교체 필요
- Jev 시장 장세 질문은 지수가 아닌 해당 종목 봉으로 판단 (지수 피드 미연동)
