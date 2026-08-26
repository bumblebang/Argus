# macOS 상주

장은 코드가 KST로 계산하므로 맥 타임존과 무관하다. 문제는 **잠자기**다. 덮개를 닫으면 데몬이 멈춘다.

## 설치

```bash
brew install python@3.12
cd argus
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
argus bootstrap
```

Claude Code를 설치한 뒤 터미널에서 `claude -p "ping"`. `config.yaml` 의 `claude_command` 는 `"claude"` (PATH).

## launchd

1. `scripts/argus.watch.plist.example` 을 복사해 `~/Library/LaunchAgents/local.argus.watch.plist`
2. hang 대비: `scripts/argus.watchdog.plist.example` → `local.argus.watchdog.plist` (5분 간격)
3. 두 파일의 `/REPLACE/WITH/ABS/PATH/TO/argus` 를 실제 경로로
4. `caffeinate -s` 가 python을 감싼다 (시스템 슬립 억제, 전원 연결 권장)

```bash
launchctl load ~/Library/LaunchAgents/local.argus.watch.plist
launchctl load ~/Library/LaunchAgents/local.argus.watchdog.plist
launchctl start local.argus.watch
```

로그: `logs/watch.log` 및 plist 의 StandardOut/Error. 대시보드 `http://127.0.0.1:8787`.
무인 상주면 `.env` 에 `NTFY_TOPIC`.

내리기:

```bash
launchctl unload ~/Library/LaunchAgents/local.argus.watchdog.plist
launchctl unload ~/Library/LaunchAgents/local.argus.watch.plist
```

KeepAlive 는 **크래시**만 살린다. 워치독은 하트비트가 5분 이상 멈추면
`launchctl kickstart -k gui/$UID/local.argus.watch` 로 hang 을 끊는다.

에너지 설정에서 “전원 어댑터 사용 시 잠자기 방지”를 켜 두는 것이 안전하다.

## 배치

Athena·market_state 는 crontab 또는 별도 LaunchAgent `StartCalendarInterval` 로 KST 시각을 맞춘다. 예: Athena KR 창 05:30. 유니버스는 watch 가 굴린다.

## IP

`argus doctor` 의 public ip 를 토스 허용 목록에 넣는다. 공유기 DHCP면 주소가 바뀐다.
