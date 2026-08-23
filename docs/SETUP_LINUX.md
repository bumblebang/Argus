# Linux 상주

장은 코드가 KST로 계산한다. 문제는 **잠자기·로그아웃**이다. 서버나 절전 안 하는 머신에서 돌리는 것이 안전하다.

## 설치

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/bootstrap.py
```

`config.yaml` 의 `claude_command` 는 `"claude"` (PATH).

## systemd --user

1. `scripts/argus.watch.service.example` 을 `~/.config/systemd/user/argus-watch.service` 로 복사
2. `/REPLACE/WITH/ABS/PATH/TO/argus` 를 실제 경로로
3. 워치독은 `scripts/argus.watchdog.service.example` + `argus.watchdog.timer.example`

```bash
systemctl --user daemon-reload
systemctl --user enable --now argus-watch.service
systemctl --user enable --now argus-watchdog.timer
```

로그: `journalctl --user -u argus-watch -f` 및 `logs/watch.log`. 대시보드 `http://127.0.0.1:8787`.
무인 상주면 `.env` 에 `NTFY_TOPIC`.

내리기: `systemctl --user disable --now argus-watchdog.timer argus-watch.service`

이 유닛은 이 저장소에서 실기로 검증하지 않았다. `watch.py --dry --ticks 1` 이 된 뒤에만 enable 한다.

## 배치

Athena·market_state 는 crontab. `scripts/argus.crontab.example` 참고. 유니버스는 watch 가 굴린다.

## IP

`python scripts/doctor.py` 의 public ip 를 토스 허용 목록에 넣는다.
