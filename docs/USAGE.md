# 사용법 · 키 · LLM 예산

클론 후 무엇을 넣고, 하루 평균 뇌가 얼마나 돌고, 어디서 줄이는지.

설치 스모크는 [SETUP.md](SETUP.md), 뇌 인증만 [AUTH.md](../AUTH.md), 돈의 경로는 [ARCHITECTURE.md](ARCHITECTURE.md).

## 하루 흐름 (권장)

1. `python scripts/bootstrap.py` → `.env` · `config.yaml` 생성
2. 아래 **키** 채우기. `risk.capital` / `paper.cash` 를 본인 금액으로
3. `python scripts/doctor.py` — 공인 IP를 토스 허용 목록에
4. `python scripts/watch.py --dry --ticks 1` — 배선만
5. 상주 등록 ([Windows](SETUP_WINDOWS.md) · [macOS](SETUP_MAC.md) · [Linux](SETUP_LINUX.md)). 무인이면 `NTFY_TOPIC`
6. 장전 배치(선택): `athena` · `build_market_state` · `value_scan`. 유니버스는 watch 가 굴린다(`screen.py` 는 수동 CLI)
7. 페이퍼로 며칠 본 뒤 라이브는 [SETUP_LIVE.md](SETUP_LIVE.md)

대시보드: watch 가 켠 `http://127.0.0.1:8787` (읽기전용).

수동 1사이클(페이퍼, live_client 없음):

```bash
python scripts/agent_cycle.py --dry   # MockLLM, 비용 0
python scripts/agent_cycle.py --cli   # 구독 CLI
```

## 발급해서 `.env` 에 넣을 것

| 변수 | 필수? | 용도 | 발급 |
|---|---|---|---|
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | **시세·감시** | 시세·계좌·(라이브면) 주문 | 토스증권 앱 Open API |
| `TOSS_ACCOUNT_NO` | 라이브 권고 | 계좌 seq. 비우면 첫 계좌 | `doctor` 출력으로 확인 후 고정 |
| `DRY_RUN` | 기본 `true` | `false` 만 실주문 가능(다른 조건과 AND) | — |
| PATH의 `claude` **또는** `ANTHROPIC_API_KEY` | 뇌 쓰려면 | 판단 LLM. 없으면 `--dry` / Mock | [AUTH.md](../AUTH.md) · [console.anthropic.com](https://console.anthropic.com) |
| `DART_API_KEY` | KR 공시·재무·Athena | 오픈다트 | [opendart.fss.or.kr](https://opendart.fss.or.kr) |
| `FRED_API_KEY` | 미 매크로 | FRED | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) |
| `ECOS_API_KEY` | 한은 매크로 | ECOS | [ecos.bok.or.kr](https://ecos.bok.or.kr/api/) |
| `FINNHUB_API_KEY` | 미 뉴스·실적 캘린더 | Finnhub | [finnhub.io](https://finnhub.io) |
| `NTFY_TOPIC` | 무인 운영 권고 | 폰 푸시(공개 서버, 추측 어려운 문자열) | [ntfy.sh](https://ntfy.sh) + 폰 앱 구독 |
| `KRX_API_KEY` | 선택 | VKOSPI·풋콜 → fear 보조입력 | [openapi.krx.co.kr](https://openapi.krx.co.kr) |
| `KRX_USER` / `KRX_PASS` | 선택·고급 | KRX 웹 로그인 수급·공매도 등. 기본 경로 아님 | KRX 정보데이터 회원 |

키 없어도 **기동은** 된다. 없는 배선만 비고, 해당 소스·리서치가 약해진다.  
토스 키만 있으면 시세 폴링·대시보드·페이퍼 전략 루프까지는 간다. 뇌 없는 상주는 `watch.brain_backend: dry` 또는 `--dry`.

자격증명은 `config.yaml` 에 두지 않는다.

## LLM 이 도는 곳 (토큰/구독이 나가는 곳)

시세·캔들·스크리너·대시보드는 LLM이 **아니다**. 비용은 아래 콜뿐이다.

| 경로 | 언제 | 기본 모델 (example) | 콜 구조 |
|---|---|---|---|
| **뇌** (watch) | `brain_interval_sec`·트리거·세션 | 결정 `opus` + 검증 `opus` | 각성 1회 ≈ **결정 1 + 검증 1** |
| **Athena** | 장전 창(배치) | `sonnet` | 종목당 1, `max_per_run` 상한 |
| **value_scan** | 배치 | `sonnet` | 실행당 ≤ `max_per_run`(주말 `weekend_max_per_run`) |
| **value_trade** | 창 시각(배치) | `fable` | 실행당 ≤ `max_per_run` |
| **public_brief** | 공개 페이지 켠 경우 | `sonnet` | TTL로 **하루 ~1콜**. HTML 렌더는 LLM 0 |

CLI(`brain_backend: cli`)는 Anthropic **구독 세션/주간 한도**를 봇과 개발이 **같은 풀**로 나눈다.  
API 키(`ANTHROPIC_API_KEY`)는 종량제 토큰 과금. 둘 다 쓰면 백엔드 선택에 따라 갈린다.

출력 상한은 `agents.max_tokens`(기본 8000). 입력은 유니버스·도сье·국면에 따라 커진다. **콜당 정확한 토큰 수는 프롬프트·보유 수에 따라 달라서 고정표가 없다.** 아래는 **콜 횟수** 기준의 운영 감각이다.

### 기본 설정의 하루 감각 (KR 장, example 기본)

가정: `brain_interval_sec: 3600`, `brain_sessions` KR 프리+정규, Athena 종료 훅 1회 + 08:00·15:30 extra, US 뇌 끔.

| 구분 | 대략 | 비고 |
|---|---|---|
| 뇌 각성(정기+훅) | **하루 ~10–12회** | Athena 직후 1 + 08:00부터 1h + 15:30. 이벤트는 +α |
| 뇌 LLM 콜 | **~16–20** (결정+검증) | 예전 30분·전세션 대비 대략 **반 이하** |
| Athena | ≤ `max_per_run` **30** sonnet | 창 05:30–07:30, 종료 시 뇌 훅 |
| value_scan | 평일 **2×15≈30** · 토 **1×16** | 07:40·15:45 / 토 10:00 |
| value_trade | ≤ **2** /회 | 모델 `fable` |

**저비용으로 시작할 때**: 뇌만 켜고 Athena·value_* · `public_page` 는 끄거나 `max_per_run` 을 한 자릿수로.  
**구독이 자주 끊길 때**: 장중 Cursor/Claude 개발을 같은 계정으로 몰지 말 것. 한도 나면 판단만 멈추고 루프·시세는 산다(대시보드 뇌 실패 카드).

## 모델·사용량 조율 (`config.yaml`)

복사본은 `config.example.yaml`. 바꾼 뒤 watch/배치 재기동.

### 빈도 (가장 효과 큼)

| 키 | 기본 | 줄이는 방향 |
|---|---|---|
| `watch.brain_interval_sec` | 0 | 정각은 extra_wakes. 0=상대 주기 끔 |
| `watch.brain_sessions` | KR/US premarket+regular | |
| `watch.extra_wakes` | KR 08/09/11/13, US 17/22:30/… | KST 벽시계. US는 DST ±1h 수동 |
| `watch.extra_wake_grace_sec` | 120 | 재기동 직후 extra 억제 |
| Athena 종료 훅 | `data/brain_wake_request.json` | 배치 직후 뇌 1회(시장 개장 전에도 소비) |
| `athena.max_per_run` | 30 | 5–10 |
| `value_scan.max_per_run` | 12 | 0에 가깝게 또는 `enabled: false` |
| `value_trade.max_per_run` | 2 | `enabled: false` 로 슬리브 끔 |
| `public_page.enabled` | false | 기본 꺼 둠 |

### 모델 티어

| 키 | 기본 | 역할 |
|---|---|---|
| `watch.brain_backend` | `cli` | `cli` 구독 / `live` API / `dry` Mock |
| `agents.claude_model` | `opus` | CLI 결정 |
| `agents.claude_val_model` | `opus` | CLI 검증. `sonnet` 이면 검증만 싸게 |
| `agents.claude_fallback_model` | `sonnet` | 주모델 실패 시 1단 폴백(세션 한도엔 무력한 경우 많음) |
| `agents.model` / `val_model` | opus급 API id | API 백엔드용 |
| `agents.thinking` | true | false 면 사고 토큰 감소(품질 트레이드오프) |
| `agents.max_tokens` | 8000 | 출력 상한 |
| `athena.model` / `value_scan.model` | sonnet | 배치 리서치 |
| `value_trade.model` | fable | 밸류 집행 판단 |

절약 예시: 결정 `sonnet`, 검증 `sonnet`, Athena `max_per_run: 8`, `brain_interval_sec: 3600`.

### 비상 폴백 (구독 한도)

`agents.cursor_bridge.enabled: true` 후 [cursor_brain_fallback.md](cursor_brain_fallback.md).  
한도일 때만 opus→sonnet→inbox. `--auto` 는 관망/거부로 **토큰 ≈0**.

## 최소 구성 체크리스트

- [ ] 토스 ID/SECRET + `DRY_RUN=true`
- [ ] `doctor` PASS, IP 등록
- [ ] 뇌: `claude -p "hello"` 또는 API 키 + `check_cli` / `check_auth`
- [ ] (권고) `DART_API_KEY`
- [ ] 상주 전 `NTFY_TOPIC` (무인 운영이면 사실상 필요)
- [ ] `watch --dry --ticks 1` 후 상주
- [ ] 구독 쓸 때: 장중 같은 계정으로 무거운 에이전트 개발 자제

라이브는 체크리스트를 페이퍼로 통과한 뒤에만.
