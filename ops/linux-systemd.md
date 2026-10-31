# Linux — systemd 사용자 타이머 (평일 자동 실행)

`~/a.code` 에 저장소가 있다고 가정합니다. 휴장일에는 스크립트가 스스로 건너뜁니다.

`~/.config/systemd/user/harness-research.service`
```ini
[Unit]
Description=Harness Grok research (once)
[Service]
Type=oneshot
WorkingDirectory=%h/a.code
ExecStart=/usr/bin/python3 scripts/run_research.py --once
```
`~/.config/systemd/user/harness-research.timer`
```ini
[Timer]
OnCalendar=Mon..Fri 08:00 Asia/Seoul
Persistent=false
[Install]
WantedBy=timers.target
```
`~/.config/systemd/user/harness-mock.service`
```ini
[Unit]
Description=Harness mock trading (KR session)
[Service]
Type=simple
WorkingDirectory=%h/a.code
ExecStart=/usr/bin/python3 scripts/run_mock.py data/theses.sqlite --send
Restart=on-failure
RestartSec=30
```
`~/.config/systemd/user/harness-mock.timer`
```ini
[Timer]
OnCalendar=Mon..Fri 08:50 Asia/Seoul
[Install]
WantedBy=timers.target
```
```bash
systemctl --user daemon-reload
systemctl --user enable --now harness-research.timer harness-mock.timer
loginctl enable-linger $USER     # 로그아웃 상태에서도 실행
journalctl --user -u harness-mock -f
```
`Restart=on-failure` 는 비정상 종료 시 30초 뒤 재시작합니다. 재시작 시 감사 로그로 장부를 복원하고,
결과불명 주문이 있으면 시작을 거부하므로(텔레그램으로 알림) 중복 주문은 생기지 않습니다.
