# K1 — NH 공식 MCP 서버(nhplug-mcp) 연결

Claude 와 대화로 **시세·잔고·체결내역을 조회**합니다. 하네스가 정지했거나 "결과불명 주문"이 생겼을 때
계좌 상태를 바로 확인하는 용도입니다. **주문 기능은 꺼 둡니다**(`NHPLUG_ENABLE_TRADING=false`) —
주문은 하네스만 합니다.

- 저장소: https://github.com/PLUG-OpenAPI/nhplug-mcp (NH투자증권 공식)
- 사전 요건: Node.js 18 이상, Git, NH PLUG 앱키·시크릿

## Claude Code (이 저장소 폴더에서)
저장소 루트의 `.mcp.json` 이 이미 설정돼 있습니다. 키는 파일에 적지 않고 **환경변수**로 넘깁니다.

```bash
# macOS / Linux
export NHPLUG_APP_KEY=... NHPLUG_APP_SECRET=... NHPLUG_DEFAULT_ACCOUNT=...
claude            # 처음 실행 시 프로젝트 MCP 서버 사용 승인
```
```powershell
# Windows PowerShell
$env:NHPLUG_APP_KEY="..."; $env:NHPLUG_APP_SECRET="..."; $env:NHPLUG_DEFAULT_ACCOUNT="..."
claude
```

## Claude Desktop
설정 → 개발자 → Edit Config 에서 `claude_desktop_config.json` 의 `mcpServers` 에 추가:
```json
"nhplug": {
  "command": "npx",
  "args": ["-y", "github:PLUG-OpenAPI/nhplug-mcp#413a14581bfbfec46b67ad78d8f59e4dd654dd2c"],
  "env": {
    "NHPLUG_APP_KEY": "발급받은_APP_KEY",
    "NHPLUG_APP_SECRET": "발급받은_APP_SECRET",
    "NHPLUG_BASE_URL": "https://moapi.nhplug.com:8443",
    "NHPLUG_ENABLE_TRADING": "false"
  }
}
```
저장 후 Claude Desktop 을 완전히 종료(트레이 포함)했다가 다시 실행합니다.
처음 한 번은 터미널에서 `npx -y github:PLUG-OpenAPI/nhplug-mcp` 를 실행해 두면(약 1분 빌드) 시작이 빨라집니다.

## 이렇게 물어보면 됩니다
- "모의계좌 잔고 보여줘"
- "오늘 005930 체결내역 조회해줘"
- "삼성전자 현재가"

## 주의
- **버전 고정:** `#413a14581bfbfec46b67ad78d8f59e4dd654dd2c`(2026-09-28 커밋)로 고정했습니다. 고정하지 않으면 npx 가 캐시를 비울 때마다
  GitHub 최신 코드를 앱키와 함께 실행합니다(공급망 위험). 업데이트는 변경 내역을 확인한 뒤 해시를 바꾸세요.
- `NHPLUG_BASE_URL` 을 운영(`api.`)으로 바꾸면 **실계좌**를 조회합니다(주문은 꺼져 있어도 조회는 됨).
- 토큰 재발급은 NH 보안 알림을 만들 수 있습니다. SDK·MCP 모두 `~/.nhplug/` 에 토큰을 캐시해 재사용합니다.
