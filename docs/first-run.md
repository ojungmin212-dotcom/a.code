# 첫 실행 런북 (H7) — 2026-11-02(월) 국내 장

목표: **① 실서버 필드·가정 확정(`verified_by_mock: true`) ② 모의 운영 1일차 완주.**
돈이 걸린 계좌가 아니라 **NH 모의투자 계좌**만 사용합니다.

> 원격 연결(Remote Control)을 해 두시면 아래 명령은 제가 실행합니다. 연결이 없으면 오박사님이 실행하고 결과를 보내 주세요.

---

## 전날까지 (11/1)

| # | 할 일 | 확인 |
|---|---|---|
| 0-1 | PC에 저장소 받기: `git clone https://github.com/ojungmin212-dotcom/a.code` → `cd a.code` | |
| 0-2 | 설치: `pip install -e ".[dev]"` | `python -m pytest -q` 전부 통과 |
| 0-3 | `cp .env.example .env` 후 입력: NH 앱키·시크릿·**모의 계좌번호**, `NHPLUG_BASE_URL=https://moapi.nhplug.com:8443`, TypeSafe 키, xAI 키, (선택) 텔레그램 | `chmod 600 .env` (macOS·Linux) |
| 0-4 | 자가 점검: `python scripts/doctor.py` | **❌ 0건** (⚠️ 는 허용) |
| 0-5 | (선택) 원격 연결: Claude 데스크톱 → 설정 → Claude Code → Remote Control 에서 `a.code` 폴더 추가 | |
| 0-6 | 나무 MTS/HTS 에서 모의계좌에 **삼성전자 보유가 0주**인지 확인 | 남아 있으면 미리 매도 |

## 당일 타임라인 (KST)

| 시각 | 명령 | 성공 기준 | 실패하면 |
|---|---|---|---|
| **08:30** | `python scripts/doctor.py` | ❌ 0건 | 해당 항목 수정 후 재실행. 해결 안 되면 **오늘 중단** |
| **08:40** | `python scripts/check_all.py` (주문 없음) | `모의투자 서버` ✅ · `계좌` ✅(acct_type=03) · `Jev` ✅ · `Grok` ✅ · 실시간은 장 전이라 ❌ 정상 | 아래 §실패 대응표 |
| **09:10** | `python scripts/check_all.py --send --apply` | 아래 8개 ✅ → `verified_by_mock: true` 로 자동 변경 | §실패 대응표 → 설정 수정 후 **09:40 까지 재시도 1회** |
| 09:15 | MTS 에서 모의계좌 삼성전자 **0주** 확인 | 0주 | 1주 남았으면 MTS 에서 매도 후 진행 |
| **09:20** | `python scripts/run_research.py --once` | `005930: 논지 생성` 또는 `점수 … 미달`(둘 다 정상) | `실패 → 논지 없음` 이면 Grok 오류 — 출력 보내 주세요. **매매는 시작하지 않아도 섀도 판단은 쌓입니다** |
| **09:25** | `python scripts/run_mock.py data/theses.sqlite --send` (창 하나 계속 켜 둠) | `시작 — 전송=모의 …` 알림 2줄(Jev·비교군) | 시작 거부 메시지를 그대로 보내 주세요 |
| 장중 | 텔레그램 💓(매시) · 진입/청산 알림 확인 | 🛑 없음 | §중단 기준 |
| **15:15** | (자동) 전량 청산 | 청산 알림 | 15:45 까지 청산 안 되면 MTS 에서 직접 정리 |
| **15:30~** | (자동) `📊 장 마감 요약 — 종료` | 프로세스 종료 | |
| **15:40** | `python scripts/report.py data/audit-jev.sqlite data/audit-baseline.sqlite` | 출력 확인 | |

### 09:10 점검의 8개 항목
`모의투자 서버` · `계좌` · `주문번호 필드` · `잔고 필드` · `원상복구 매도` · `실시간 필드` · **`체결수량 의미`** · (`체결통보 주문번호`, `Jev`, `Grok` 는 참고 — apply 조건 아님)

---

## 실패 대응표 (설정 수정은 동결 규칙상 허용: "실측 결과에 따른 설정·필드 수정")

| 실패 항목 | 원인 후보 | 조치 |
|---|---|---|
| 계좌 ❌ (acct_type≠03) | 모의 계좌번호가 아님 | `.env` 의 `NHPLUG_DEFAULT_ACCOUNT` 를 acct_type=03 계좌로 |
| 주문번호 필드 ❌ | 응답의 주문번호 키가 `mkt_orr_no` 가 아님 | `data/check-raw/*주문번호*.json` 의 `Output_0` 키를 `broker_fields.yaml order_no_fields` 앞에 추가 |
| 잔고 필드 ❌ (필드 누락) | 수량 키가 `itg_bnc_qty` 가 아님 | 원문에서 수량 키 확인(후보 `rsdl_qty`) → `quantity_field` 수정 |
| 잔고 필드 ❌ (30초 안에 안 보임) | 체결 지연·주문 거부 | MTS 에서 체결 여부 확인 후 재시도 |
| 원상복구 매도 ❌ | 매도 거부 | **MTS 에서 1주 직접 매도** (안 하면 운영 시작 시 장부 불일치로 정지) |
| 실시간 필드 ❌ (0건) | 장 마감·포트·채널 | 장중인데 0건이면 원문(`check-raw`) 보내 주세요 |
| 체결수량 의미 ❌ "누적거래량과 같음" | `movolume` 이 누적값 | `feed.yaml volume_field` 를 체결 1건 수량 필드로(원문에서 확인) |
| 체결수량 의미 "판정 불가" | 거래가 적어 푸시 부족 | 10분 뒤 재실행 |
| 체결통보 주문번호 ❌ 불일치 | 통보 번호 체계가 다름 | `strategy.yaml execution.fill_notices: false` (잔고 대조로 운영, 매도 손익은 추정치) |
| 체결통보 미수신 | 통보 채널 지연·미지원 | 위와 같음. 운영은 가능 |
| Jev ❌ | 키·가입 상태 | `data/check-raw/Jev.txt` 보내 주세요. Jev 없이는 비교군만 의미 있음 |
| Grok ❌ | 키·모델명·응답 형식 | `data/check-raw/Grok.txt` 보내 주세요(모델명 `grok-4.7` 미확인 상태) |

## 중단 기준 — 하나라도 해당하면 `data/KILL` 파일 생성(전량 청산·신규 진입 중단)

1. 🛑 **장부 불일치** 알림 → MTS 보유와 대조
2. 🛑 **레인 오류** 알림(코드 버그) → 알림 문구와 `data/audit-*.sqlite` 보내 주세요
3. 같은 종목 "결과불명" 주문이 30분 넘게 해소되지 않음
4. 💓 생존 신호가 70분 넘게 오지 않음 → 프로세스 확인
5. 모의계좌 잔고가 텔레그램 요약과 다름

정지 후 재시작: 원인 해결 → `python scripts/resolve_pending.py …`(필요 시) → `python scripts/resume.py data/audit-jev.sqlite "사유"` → `run_mock.py` 재실행.

## 보내 주실 것 (원격 연결이 없을 때)

| 파일 | 주의 |
|---|---|
| `data/check-report.json` | 키 없음. 공유해도 됨 |
| `report.py` 출력 화면 | |
| 텔레그램 알림(진입·청산·🛑) 캡처 | |
| `data/check-raw/` | ⚠️ **계좌번호·잔고 원문 포함** — 공개 장소에 올리지 마세요 |
| `.env` | ❌ **절대 보내지 마세요** |

## 성공 판정 (오늘의 완료 기준)

- [ ] `verified_by_mock: true` (2개 파일)
- [ ] 체결통보 주문번호 일치 여부 확정(일치/불일치 어느 쪽이든 확정이면 성공)
- [ ] 모의 운영이 15:30 까지 🛑 없이 완주하고 자동 종료
- [ ] 모의계좌 최종 보유 0주
- [ ] 섀도 판단 기록 수백 건(`report.py` 의 `Jev 보정[논지 없음(섀도)]` 표본)

이 다섯 개가 채워지면 **동결 해제 조건의 1/2(실측 확인)** 이 끝나고, 남은 조건은 모의 운영 10거래일입니다.
