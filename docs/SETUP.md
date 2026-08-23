# 설치와 페이퍼

Python 3.11+ . 토스 Open API는 본인 앱에서 신청한다. 키를 다른 사람과 나누지 않는다.

## 키

| 변수 | 언제 |
|---|---|
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | 시세·감시·(라이브면) 주문 |
| `TOSS_ACCOUNT_NO` | 라이브 권고. 비우면 첫 계좌 |
| `DRY_RUN=true` | 페이퍼. 기본 |
| PATH의 `claude` 또는 `ANTHROPIC_API_KEY` | 뇌. 없으면 `--dry` 만 |
| `DART_API_KEY` | KR 공시·재무 |
| `FRED_API_KEY` / `ECOS_API_KEY` / `FINNHUB_API_KEY` | 매크로·미 뉴스 |
| `NTFY_TOPIC` | 폰 푸시. 무인 운영이면 사실상 필요 |

발급: 토스 앱 Open API, [DART](https://opendart.fss.or.kr), [FRED](https://fred.stlouisfed.org/docs/api/api_key.html), [Finnhub](https://finnhub.io), [한은 ECOS](https://ecos.bok.or.kr/api/), [ntfy.sh](https://ntfy.sh).

전체 키 표·하루 LLM 콜 감각·모델 조율: [USAGE.md](USAGE.md).

## 순서

1. `python scripts/bootstrap.py`
2. `.env` 채우기. `config.yaml` 의 `risk.capital` / `paper.cash` 를 본인 금액으로.
3. `python scripts/doctor.py` — 공인 IP를 토스 허용 목록에 넣는다.
4. `python scripts/watch.py --dry --ticks 1`
5. 상주는 OS 문서 ([Windows](SETUP_WINDOWS.md) · [macOS](SETUP_MAC.md) · [Linux](SETUP_LINUX.md)). 무인이면 `.env` 에 `NTFY_TOPIC`. 페이퍼로 하루 돌린 뒤 라이브는 [SETUP_LIVE.md](SETUP_LIVE.md).

클론 직후엔 config 의 정적 `universe` 소수만 쓴다. 상주 watch 가 `data/universe.yaml` 을 굴리기 시작하면 그 파일이 우선이다. `scripts/screen.py` 는 수동 재실행용이다.

테스트: 저장소 루트에서 `pip install -r requirements-dev.txt && python -m pytest`.
구조: [ARCHITECTURE.md](ARCHITECTURE.md).
