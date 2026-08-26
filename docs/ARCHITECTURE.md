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
src/engine/     감시 루프, 체결, 원장(store), orchestrator(watch 조립)
src/agents/     뇌·검증·확신도·밸류 — wiring + cycle_runner (+ pipeline shim)
src/datasources/ 시세·공시·매크로
scripts/watch.py  얇은 CLI → engine.orchestrator.run_from_args
config.example.yaml  공개용 기본값 (페이퍼)
```

`pipeline` 은 DAG가 아니다. `wiring`(브로커/LLM) + `cycle_runner` + `cycle.run_cycle` 의 호환 shim.
`watcher` config 키는 `disclosure`/`events` 별칭과 병합된다(운영 yaml 즉시 rename 불필요).

`config.yaml` · `.env` · `data/*.db` · `CONTEXT.md` 는 gitignore. 예시는 `config.example.yaml` · `.env.example`. 시드 유니버스(`data/base_universe_*.txt`)와 매크로 일정은 추적한다.

## 패키징 (Phase 0+) / CLI (Phase 3)

`pip install -e .` 후 `argus <cmd>` 가 정본 진입점이다.
watch 조립 로직은 `src/engine/orchestrator.py` 에 있고, `scripts/watch.py` 는 argparse 위임만 한다.
`scripts/run_*.bat` 는 `argus.exe` 우선·`python scripts\…` fallback. 작업 스케줄러 상주
(`register_watch.ps1`)는 무콘솔을 위해 `pythonw scripts\watch.py` stub 를 유지한다.

## data 경로 (Phase 2)

`src/paths.py` 가 논리 키를 resolve 한다. **존재 우선:** config 지정 →
`data/state|inbox|ledgers/…`(LAYOUT) → 레거시 `data/…`(CANONICAL).
컷오버 전에도 레거시만으로 운영 가능. 물리 이동은 장후
[`OPS_CUTOVER.md`](OPS_CUTOVER.md) + `argus doctor --migrate-data [--apply]`.
`data/llm_inbox` 는 이동 후에도 junction/별칭으로 유지(PokeTokenBarWin).

## research 경계 (Phase 4)

`research/` 는 lab only. 런타임(`src/`, 상주 watch)은 import 금지(Gate G0).
산출물은 `research/quant_review/data/`. 과거 `data/quant_review/` 잔여는
`argus doctor --migrate-research`. 인덱스·승격 금지: [`research/README.md`](../research/README.md).

## 의도적으로 안 나눈 것

대시보드 HTML은 `scripts/dashboard.py` 한 파일이다. 진입존 숫자는 엔진이 체결에 쓰고, 대시보드는 같은 필드를 표시만 한다. 공개 브리핑 페이지는 기본 꺼져 있다. broker/risk 구현 파일 물리 이동은 shim re-export(`src.engine.broker` 등) 후 Phase 후반에 한다.

## 테스트

`pytest` 는 `config.example.yaml` 만 읽는다 (`ARGUS_CONFIG`). 운영 라이브 설정을 건드리지 않는다.

키·LLM 예산·사용 순서: [USAGE.md](USAGE.md).
