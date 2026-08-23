# 구조

Argus는 **판단(LLM)** 과 **집행(돈)** 을 나눈 상주 봇이다. 클론한 뒤 각자의 키·자본·유니버스로 채운다.

## 프로세스가 둘이다

| 무엇이 | 언제 | 주문 |
|---|---|---|
| `scripts/watch.py` | 항상 (상주) | 라이브면 가능. 하드게이트 통과분. 유니버스 롤도 여기 |
| 배치 (`athena`, `build_market_state`, `value_scan` …) | 장전·주기 | 없음. `live_client` 미주입 |

Windows 작업 이름 **ArgusWatch** 가 상주다. `scripts/screen.py` 는 수동/디버그 CLI이고, 매매 유니버스(`data/universe.yaml`)는 watch 데몬이 굴린다. 장전 배치(Athena·국면)는 데몬이 아니다.

## 한 틱에서 돈이 나가는 길

```
시세·트리거 → (선택) 뇌 제안 → 검증 LLM → risk_gate → broker
                                              ↑
                                    data/HALT · 한도 · 세션 · 존
```

실주문은 동시에 세 가지가 참일 때만이다.

1. `broker.mode: live`
2. yaml `dry_run` 과 `.env DRY_RUN` 둘 다 false
3. **watch 프로세스**가 `live_client`를 주입

`python main.py` 와 `agent_cycle.py` 는 이 주입이 없다.

## 디렉터리

```
src/engine/     감시 루프, 체결, 원장(store)
src/agents/     뇌·검증·확신도·밸류
src/datasources/ 시세·공시·매크로
scripts/watch.py  상주 진입점 (대시보드 HTTP는 같은 프로세스)
config.example.yaml  공개용 기본값 (페이퍼)
```

`config.yaml` · `.env` · `data/*.db` · `CONTEXT.md` 는 gitignore. 예시는 `config.example.yaml` · `.env.example`. 시드 유니버스(`data/base_universe_*.txt`)와 매크로 일정은 추적한다.

## 의도적으로 안 나눈 것

대시보드 HTML은 `scripts/dashboard.py` 한 파일이다. 진입존 숫자는 엔진이 체결에 쓰고, 대시보드는 같은 필드를 표시만 한다. 패키징 `python -m argus` 는 아직 없다. 공개 브리핑 페이지는 기본 꺼져 있다.

## 테스트

`pytest` 는 `config.example.yaml` 만 읽는다 (`ARGUS_CONFIG`). 운영 라이브 설정을 건드리지 않는다.

키·LLM 예산·사용 순서: [USAGE.md](USAGE.md).
