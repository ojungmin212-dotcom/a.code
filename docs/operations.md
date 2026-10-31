# 무인 운영 가이드 (F11)

| 시각(KST, 평일) | 작업 | 명령 |
|---|---|---|
| 08:00 | Grok 리서치 1회(휴장일 자동 건너뜀) | `run_research.py --once` |
| 08:50 | 장중 운영 시작 → 장 마감 후 요약 알림·자동 종료 | `run_mock.py data/theses.sqlite --send` |
| 매시간 | 생존 신호(💓) 텔레그램 알림 | 자동(`strategy.yaml operations.heartbeat_min`) |
| 주 1회 | 성과·보정·벤치마크 | `report.py data/audit-jev.sqlite data/audit-baseline.sqlite` |
| 시작 전 | 자가 점검 | `doctor.py` |

설정 방법: [Linux](../ops/linux-systemd.md) · [macOS](../ops/macos-launchd.md) · [Windows](../ops/windows-task-scheduler.md)

**알림을 받으려면** `.env` 에 `TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID` 를 넣으세요.
생존 신호가 1시간 넘게 오지 않으면 프로세스가 죽은 것입니다 — PC 상태와 `data/*.log` 를 확인하세요.

**저장되는 데이터(data/, git 제외)**
- `audit-*.sqlite` 판단·주문·체결·1분봉 · `theses.sqlite` 논지 · `ticks/` 틱 원본(F13) · `check-*` 점검 결과
