# macOS — launchd (평일 자동 실행)

`~/Library/LaunchAgents/com.harness.mock.plist` (리서치는 Hour 8 / Minute 0, 스크립트만 바꿔 같은 형식으로 하나 더)
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.harness.mock</string>
  <key>WorkingDirectory</key><string>/Users/YOU/a.code</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>scripts/run_mock.py</string>
    <string>data/theses.sqlite</string><string>--send</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>50</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/YOU/a.code/data/mock.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/a.code/data/mock.err</string>
</dict></plist>
```
```bash
launchctl load ~/Library/LaunchAgents/com.harness.mock.plist
```
⚠️ 맥이 잠자기 상태면 실행되지 않습니다: 시스템 설정 → 배터리/에너지에서 잠자기 방지, 또는 `caffeinate`.
시간은 맥의 시스템 시간대 기준입니다(한국 시간으로 설정돼 있어야 함).
