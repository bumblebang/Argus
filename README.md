# Argus (아르고스)

시황·종목 리서치·판단·검증·주문 한도를 코드가 감시하는 자율 투자 에이전트.
주문은 토스증권 Open API. **기본 설정은 페이퍼(실주문 없음)** 이다.

토스 Open API에는 모의투자 샌드박스가 없다. 라이브는 본인 실계좌다.
이 코드는 교육·도구이며 투자 권유가 아니다. 손실은 운영자 책임이다. 라이선스: [LICENSE](LICENSE).

구조(돈의 경로·상주 vs 배치): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 돌리려면 (여기까지면 기동)

| 단계 | 무엇 |
|---|---|
| 1 | Python 3.11+, 아래 스모크 |
| 2 | `.env` 에 `TOSS_CLIENT_ID` / `SECRET` (시세). `DRY_RUN=true` 가 기본 |
| 3 | (선택) 뇌: PATH의 `claude` 또는 `ANTHROPIC_API_KEY` |
| 4 | (상주 전 권고) `NTFY_TOPIC` — 무인 운영이면 사실상 필요 |

클론 직후 유니버스는 config 정적 소수다. 상주 watch 가 유니버스를 굴린다. 키 표·LLM 예산은 [USAGE.md](docs/USAGE.md). 상주 OS별·라이브는 아래 링크.

## 5분 스모크

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python scripts/bootstrap.py     # config.yaml · .env 가 없을 때만 생성
# .env 에 TOSS_CLIENT_ID / SECRET 을 채운다 (시세용)

python scripts/doctor.py
python scripts/watch.py --dry --ticks 1
```

대시보드: `http://127.0.0.1:8787` (watch 프로세스 안, 읽기전용).

```
시세 → 트리거 → 뇌(선택) → 검증 → 하드게이트 → 주문
```

사용법·API 키·LLM 예산: [docs/USAGE.md](docs/USAGE.md).
라이브 실주문은 [docs/SETUP_LIVE.md](docs/SETUP_LIVE.md) 체크리스트를 통과한 뒤에만.
상주: [Windows](docs/SETUP_WINDOWS.md) · [macOS](docs/SETUP_MAC.md) · [Linux](docs/SETUP_LINUX.md).
키·페이퍼: [docs/SETUP.md](docs/SETUP.md). 뇌 인증: [AUTH.md](AUTH.md). 기여: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

`python main.py` 는 안내만 하고 종료한다. 상주는 `scripts/watch.py`. 구 루프 지름길은 없다.

`.env` · `config.yaml` · `CONTEXT.md` · `data` 원장(json/db/토큰)은 gitignore다. 시드 유니버스·매크로 일정만 추적한다.

## 무엇이 도는가

```
scripts/watch.py          상주: 시세 폴링 → 트리거 → 뇌 → 검증 → 하드게이트 → 주문
scripts/athena.py         배치: 종목 딥리서치(도시에)
scripts/screen.py         수동 CLI: 유니버스 재스크린 (상주는 watch 가 굴림)
scripts/build_market_state.py  배치: 국면·매크로 파일
scripts/doctor.py         점검: 키·설정·공인 IP
scripts/agent_cycle.py    수동 1사이클 (기본 페이퍼, 실주문 없음)
```

판단(LLM)과 집행(돈)은 분리돼 있다. 모든 주문은 [`src/risk_gate.py`](src/risk_gate.py)를 통과한다.
`data/HALT` 파일이 있으면 신규 주문을 막는다.

데이터(`universe.yaml`, 히스토리, 도시에, `bot.db`)는 클론 직후 비어 있다. 각자 채운다.

테스트: `pip install -r requirements-dev.txt && python -m pytest`
