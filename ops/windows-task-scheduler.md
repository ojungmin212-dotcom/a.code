# Windows — 작업 스케줄러 (평일 자동 실행)

PowerShell(관리자 아님)에서 저장소 폴더로 이동한 뒤 실행합니다. `python` 경로가 다르면 `(Get-Command python).Source` 로 확인.

```powershell
$repo = (Get-Location).Path
$py = (Get-Command python).Source
$days = "Monday","Tuesday","Wednesday","Thursday","Friday"

$a1 = New-ScheduledTaskAction -Execute $py -Argument "scripts\run_research.py --once" -WorkingDirectory $repo
$t1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 08:00
Register-ScheduledTask -TaskName "Harness-Research" -Action $a1 -Trigger $t1

$a2 = New-ScheduledTaskAction -Execute $py -Argument "scripts\run_mock.py data\theses.sqlite --send" -WorkingDirectory $repo
$t2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 08:50
$s2 = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -WakeToRun
Register-ScheduledTask -TaskName "Harness-Mock" -Action $a2 -Trigger $t2 -Settings $s2
```
- 확인: 작업 스케줄러 앱 → 작업 스케줄러 라이브러리 → Harness-*
- 긴급 정지: 저장소의 `data` 폴더에 `KILL` 파일 생성
- PC가 꺼져 있거나 절전이면 실행되지 않습니다(`-WakeToRun` 은 절전 해제를 시도).
