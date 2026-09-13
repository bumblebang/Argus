# 풀·뇌 B안 — 설계 검토 및 작업 분해

> **상태:** 설계만 (미구현). 2026-09-13 실측 기반 재설계.
> **상위 SSOT:** `CONTEXT.md` § 풀·뇌 A/B 로드맵 — 이 문서가 B안 상세 SSOT.
> **A안 구현 맵:** `universe_roll.core_refresh` → `strategy_scores.json` → `serve_policy.select_scan_candidates` → `features.assemble` → `context.build_context` → `cycle.run_cycle`.

---

## 1. 현행 B안 명세 (CONTEXT 발췌)

| 항목 | 내용 |
|------|------|
| 목표 | 도시어 = 단일 진실 — 전략·진입 논거를 Athena 도시어에 흡수 |
| Athena | `recommended_strategy`, `strategy_rationale`, `horizon_hint` evidence 확장 |
| 뇌 scan | pad=`strategy_scores` 축소/폐지 — bullish 도시어 TTL 내 위주 |
| 뇌 역할 | 집행·사이징·SELL·타이밍 (재분석 최소화) |
| B2(선택) | dossier `track=swing\|value` |
| 선행 | A안 `brain_serve` n/context 안정 ≥1주 |

---

## 2. 실측 (2026-09-13(일) 16시, `data/state/bot.db`)

> 측정 엔진 = `scripts/pool_brain_report.py` (B0-5). 보유 집합은
> `store.get_open_positions()`(= `state NOT IN ('closed','armed')`) 기준.

### 2-1. 샷리스트 구성 — pad 가 신규 후보 공급의 42%

`select_scan_candidates` 를 실제 유니버스·보유·도시어로 재실행한 결과:

| 항목 | 값 |
|------|-----|
| 유니버스 | 216 (KR/US, core + day 태그) |
| shortlist | 40 = **must 23 + pad 17** (`scan_cap: 40`) |
| must 구성 | held 10 + armed 0 + (bullish ∩ 유니버스) 17 에서 중복 제거 |
| must 중 stub | **6** — 보유인데 유니버스(TV top100) 밖 → 피처 없는 `serve_stub` |
| pad 점수 유효 종목 | **0 / 216** → pad 는 점수순이 아니라 **유니버스순 폴백** |
| `strategy_scores` | asof 09-11 21:24, age **42.5h** > stale gate 36h → 로드 스킵 |

→ **"pad 축소/폐지"는 신규 후보 공급의 42%(17/40)를 끊는 변경이다.** 명세가
"축소/폐지"를 가볍게 적어둔 것과 실제 비중이 다르다. 게다가 그 17칸은 지금
점수 랭킹조차 아니다(유니버스순 폴백) — 즉 **pad 의 문제는 크기가 아니라 품질**이다.
주말엔 `strategy_scores` 가 항상 stale 이다(금요일 개장 전 배치 → 월요일 개장 전까지 36h 초과).

### 2-2. 도시어 절벽 — B안의 실제 급소

| 시점 | fresh 도시어 | fresh bullish |
|------|---|---|
| 지금 (일 16시) | 60 (KR 30 / US 30) | 19 (KR 10 / US 9) |
| 월 08:00 (KR 프리) | **0** | **0** |
| 월 09:00 (KR 개장) | **0** | **0** |

- fresh 도시어 나이: min 46.3h / p50 57.7h / max 58.3h, 적용 TTL 전량 60h.
- 도시어 생성은 **평일만** (09-11 금 60건 → 09-12 토·09-13 일 0건).
- Athena 창: KR 05:30~07:30 / US 17:00~21:50.

→ 금요일 배치가 일요일 18시경 전량 만료되고 월요일 05:30 배치까지 공백.
A안은 pad 가 메워주지만, **B안(pad 폐지)이면 그 구간 shortlist = 보유 10종, 신규 후보 0.**
일·월 US extra wake(00:30 / 02:30 / 04:30)는 전부 빈손이 된다. 연휴는 더 길다.

### 2-3. 보유 도시어 — any stance 는 90%, **bullish 는 50%**

| 항목 | 값 |
|------|-----|
| 보유 (open positions) | 10 (KR 5 / US 5) |
| 보유 중 fresh 도시어(any stance) | **9 / 10 (90%)** |
| 보유 중 fresh bullish | **5 / 10 (50%)** |
| 보유 중 유니버스(TV top100) 밖 | **6 / 10** → `serve_stub` |
| `entry_basis` | None 6 / value 3 / thesis 1 |

→ Athena 가 보유를 최우선으로 리서치하므로 커버리지 자체(90%)는 충분하다.
문제는 **bullish 가 절반**이라는 것 — B안이 퍼널을 bullish 도시어로 좁히면
보유 관리와 신규 진입이 같은 좁은 집합을 공유하게 된다.
`entry_basis` 도 10건 중 6건 미기록(→ `entry_basis.py:69` inference 폴백 = thesis)이라,
"뇌 역할 = SELL·타이밍" 재정의 전에 스탬프 누락 경로를 먼저 확인해야 한다.

### 2-4. 선행 결함 — `athena_queue` 쿨다운 붕괴 (P0)

```278:281:src/agents/athena_phase2.py
    try:
        rows = store.recent_events(
            ATHENA_QUEUE_KIND, time.time() - hours * 3600, limit=30)
```

`recent_events` 는 `WHERE kind=? AND ts>=? ORDER BY ts DESC LIMIT ?` — **심볼 필터가 없다.**
6h 창에 큐 이벤트가 30건을 넘으면 대상 심볼이 최신 30건에 들어올 확률이 사라져
`was_recently_queued` 가 항상 False → 루프 틱마다 재등록 → 이벤트가 늘수록 더 나빠지는 양성 피드백.

| 날짜 | `athena_queue` 건수 | distinct 심볼 |
|------|---|---|
| 09-07 | 240 | 53 |
| 09-08 | 136 | 77 |
| 09-09 | 90,439 | 103 |
| 09-10 | 24,619 | 98 |
| 09-11 | **207,613** | 113 |

2차 피해 — `_athena_queued(..., limit=80)` 도 같은 함수를 쓴다:

```245:247:src/agents/athena_phase2.py
        rows = store.recent_events(
            ATHENA_QUEUE_KIND, time.time() - since_hours * 3600, limit=80)
```

스팸 일자엔 "최근 80건"이 **직전 수십 초**를 덮는다 → Phase 2 재소환 우선순위가
"24h 내 트리거된 종목"이 아니라 "방금 갭난 소수 종목"으로 붕괴. 실측 결과:

- 7일간 큐 트리거 183종 중 **69종 미리서치**.
- 대신 **비보유 13종을 7일에 3~5회 반복 리서치** (`033790` 5회, `066970`·`095610`·`336260`·`001820` 4회) — 큐 경로가 `min_refresh_hours: 48` 을 우회.
- 7일 리서치 288건 / distinct 203 — 예산 30/run 의 상당 부분이 반복 대상에 잠김.

3차 피해 — `events` 총 683,730행 중 `athena_queue` 390,809행(payload 34MB) = **57%**.
`bot.db` 비대화 백로그의 주범 2번(1번은 틱 snapshots, #55 처리).

### 2-5. A안 안정성 (선행조건 판정 재료)

`brain_serve` 이벤트, 21일:

| 구간 | 관측 |
|------|------|
| `scan_cap: 40` 정착 | 09-03 이후 `n_items` p50 = 40 일관 |
| `context_bytes` | scan tier 175~191KB, focus tier 44~55KB |
| `cycle_error` (14d) | 0 |
| `wiring_mismatch` (14d) | 0 |
| `strategy_scores_stale` 플래그 | 기록된 이벤트 전부 False |
| reason 분포 | extra 78 / wake_triggers 66 / periodic 20 / athena_done 11 |

단, 09-02~11 매일 15:20·19:50 갭 각성이 `KeyError: 'market'` 로 사망했고(#53), **실전 확인은
09-14(월) 대기**. 즉 "A안 ≥1주 안정"은 scan 퍼널에 대해선 충족, **갭 경로는 미검증.**

---

## 3. 설계 개선 결정

### D1. B안의 1순위를 "pad 폐지" → "pad 품질 교체" 로 바꾼다

pad 는 40칸 중 17칸(42%)이다 — 폐지는 신규 후보 공급을 절반 가까이 끊는
큰 변경이고, 절벽 구간(§2-2)에서는 **유일한** 공급원이다. 반면 그 17칸은 지금
점수 랭킹이 아니라 유니버스순 폴백이다(§2-1). 즉 고칠 것은 크기가 아니라 소스다.
**pad 는 유지하고 랭킹 소스를 도시어 기반으로 교체**한다(B3-1).

### D2. "단일 진실"의 범위를 명문화 — 논거·전략 라벨까지, 가격 레벨은 코드 권위 유지

현행은 `combine_stop_target` 이 **코드 손절/목표를 권위**로 두고 도시어 레벨은
밴드(`MIN_STOP_PCT`~`MAX_STOP_PCT` 등) 안에서만 채택한다. `sanitize()` 가
bullish→neutral 강등을 하는 이유(LLM 레벨 신뢰 불가)와 동일한 근거다.
"단일 진실"을 문자 그대로 밀면 이 가드를 풀어야 하므로 **B안 스코프에서 제외**한다.

→ B안이 흡수하는 것: `recommended_strategy`, `strategy_rationale`, `horizon_hint`.
→ B안이 건드리지 않는 것: `combine_stop_target` 밴드, `sanitize` 하드가드, `resolve_strategy` 우선순위(B3 까지 보류).

### D3. 선행조건을 수치 게이트로 못 박는다

"`brain_serve` n/context 안정 ≥1주"는 판정식이 없어 측정 불가였다. 아래로 대체:

| 게이트 | 기준 |
|--------|------|
| G1 | 최근 7 거래일 `cycle_error` = 0 · `brain_serve` 매 거래일 ≥3건 |
| G2 | scan tier `n_items` = `scan_cap` ± 2 가 7 거래일 연속 |
| G3 | `context_bytes` p95 < 220KB (LLM 입력 폭주 없음) |
| G4 | `athena_queue` 일 건수 < 1,000 (= B0 픽스 검증) |
| G5 | 갭 각성(15:20 / 19:50) `KeyError` 0 · 사이클 진행 — **09-14(월) 이후** |
| G6 | 보유 도시어 커버리지(any stance) ≥ 80% 가 3 거래일 연속 |

G1~G5 통과 전 B1 착수 금지. G6 는 B3 게이트.
판정 엔진 = `python scripts/pool_brain_report.py` (`--strict` 로 exit code 판정).

**2026-09-13 1회차:** G1·G2·G3·G6 PASS / **G4 FAIL**(207,613/일 — B0 픽스 대상) ·
**G5 FAIL**(관측창에 갭 각성 0건 — #53 이후 첫 거래일이 09-14(월)).

### D4. 절벽 방어를 B안 필수 항목으로 승격

세 선택지 — **(a) 권장**:

- **(a) pad 폴백 유지 + `dossier_stale_fallback` 명시**: fresh bullish < N 이면 pad 로 채운다(현행 동작을 의도로 문서화 + 이벤트 기록).
- (b) TTL 60h → 84h 연장: 주말은 덮지만 연휴 불가, 낡은 레벨로 진입할 위험.
- (c) 주말 Athena 배치 신설: LLM 예산·클코 세션 한도와 충돌(운영 리스크 항목 ①).

---

## 4. 작업 분해

### B0 — 선행 픽스 (B안과 무관, P0) — **구현 완료**

브랜치 `fix/athena-queue-cooldown`.

| # | 작업 | 구현 |
|---|------|------|
| B0-1 | 쿨다운을 심볼 스코프 조회로 | `Store.recent_events(..., symbol=)` keyword-only 추가 + `idx_events_kind_symbol(kind, symbol, ts)` · `was_recently_queued` 교체 |
| B0-2 | 큐 조회를 심볼 집계로 | `Store.recent_events_by_symbol` 신설(창 안 종목별 최신 1건, 종목 수 기준 limit) · `_athena_queued(max_symbols=200)` |
| B0-3 | 이벤트 보존 정책 | `Store.prune_events(kinds, older_than_sec=7d)` + `loop._maybe_prune_events` 타이머(`watch.events_*`, 기본 7일·1h 주기) + `scripts/prune_events.py`(일회성·`--stats`·`--vacuum`). 대상 = `Store.PRUNABLE_EVENT_KINDS` = `athena_queue`·`athena_scan` (원장·귀속 kind 제외) |
| B0-4 | 큐 티어 예산 상한 | `_queued_for_refresh` — 보유 제외 + `athena.queue_max_per_run`(기본 10) 상한 |
| B0-5 | 게이트 측정 엔진 | `scripts/pool_brain_report.py` — G1~G6 PASS/FAIL + brain_serve 일별·큐 일별·must/pad 분할·도시어 절벽 |

**B0-4 가 B0-1·B0-2 와 같은 PR 에 있어야 하는 이유:** 쿨다운을 고치면 큐가 실제
트리거 전량(7일 183종 ≈ 26종/일)을 담게 된다. `select_symbols` 는 큐를 미커버보다
먼저 보므로, 상한이 없으면 **고친 큐가 30/run 예산을 다 먹어 미커버 발굴이 굶는다** —
B안이 기대는 도시어 커버리지가 오히려 나빠진다.

**철회한 설계:** 큐 경로에 `min_refresh_hours` 하한을 걸어 "비보유 종목 반복
리서치(7일에 3~5회)"를 막으려 했으나, 큐가 min_refresh 를 우회하는 것은
`test_select_symbols_queue_bypasses_min_refresh` 가 명시하는 **의도된 설계**다
(새 재료엔 즉시 반응). 반복 리서치의 원인도 결국 B0-2 의 조회 버그였다 —
큐가 임의의 소수만 반복해 담았기 때문. 상한만 두고 하한은 넣지 않았다.
**남은 관찰거리:** 픽스 후에도 같은 비보유 종목이 매일 재리서치되는지
(`pool_brain_report.py` + `dossier_report.py`). 지속되면 PM 결정 사항.

**남은 잠재 결함(이번 스코프 밖):** `_disclosure_queued`·`_earnings_result_queued`
(`athena.py:294`, `:330`)도 같은 "kind 전체 행 limit=50" 형태다. 현재 볼륨이
낮아(14일 disclosure 120건 · earnings_result 22건) 증상은 없지만 구조는 동일하다.
route 가 여러 값(queue/wake)이라 종목별 최신 1건으로 바꾸면 의미가 달라져서
같이 고치지 않았다.

### B1 — 도시어 증거 확장 (관측 전용, 집행 미연결)

| # | 작업 | 파일 | 완료 기준 |
|---|------|------|-----------|
| B1-1 | `DossierOutput` 에 `recommended_strategy`·`strategy_rationale`·`horizon_hint` 추가 | `src/agents/schemas.py:57-74` | 3필드 모두 optional. `recommended_strategy` 는 `strategies.REGISTRY` 키만 허용 |
| B1-2 | 코드 하드가드 — REGISTRY 밖 이름은 drop + `sanitize_notes` 기록 | `src/agents/athena.py:212-260` (`sanitize`) | REGISTRY 밖/None 이면 필드 제거, stance 강등은 **하지 않음** |
| B1-3 | `evidence` JSON 에 영속 | `src/agents/athena.py:559-563` | 기존 `evidence` envelope 에 3필드 추가, 하위호환(없으면 None) |
| B1-4 | Athena 프롬프트에 전략 선택 지침 | `ATHENA_SYSTEM` (`athena.py:35-100`) | 8전략 이름·성격을 주입하고 "확신 없으면 비워라" 명시 |
| B1-5 | `_dossier_brief` 에 노출 (뇌 컨텍스트 = **참고 정보**) | `src/agents/cycle_runner.py:307-324` | `candidates[].dossier.recommended_strategy` 로 뇌에 보이되 결정 프롬프트는 "참고" 어조 |
| B1-6 | 리포트에 합치도 집계 | `src/eval/dossier_quality.py` · `scripts/dossier_report.py` | `recommended_strategy` 채움률 · vs `strategy_scores.best` 합치율 · vs 실제 `resolve_strategy` 합치율 |

**B1 은 `resolve_strategy`·`serve_policy` 를 건드리지 않는다.** 집행 영향 0, 되돌리기 = 필드 무시.

### B2 — 합치도·성과 판정 (코드 변경 최소, n 쌓기)

| # | 작업 | 완료 기준 |
|---|------|-----------|
| B2-1 | 도시어 전략 라벨의 outcome 귀속 | `attribution.py` 에 `dossier_strategy` 버킷 — `meta.dossier_id` 로 조인 |
| B2-2 | 승격 판정 | 전략×시장 **n ≥ 5**(`quant-thin-sample` 준용) 이고 도시어 전략이 `strategy_scores.best` 대비 비손해일 때만 B3 진입 |
| B2-3 | 기각 조건 명시 | 합치율 < 40% 또는 채움률 < 50% 면 B안 **철회**하고 A안 유지 |

### B3 — 퍼널 전환 (조건부, G6 + B2 통과 후)

| # | 작업 | 내용 |
|---|------|------|
| B3-1 | pad 소스 교체 | pad 랭킹을 `strategy_scores` → 도시어 기반(conviction·rr·존 근접)으로. **pad 자체는 유지**(D1) |
| B3-2 | `dossier_stale_fallback` | fresh bullish < N(예 8) 이면 pad 를 `strategy_scores`/유니버스순으로 폴백 + `brain_serve` 에 `fallback_reason` 기록 (D4-a) |
| B3-3 | `resolve_strategy` 우선순위 삽입 | `proposal.strategy` > **도시어 `recommended_strategy`** > config 폴백. `wiring.py:250-271`. config `strategy_source: dossier` 토글로 롤백 가능 |
| B3-4 | 보유 stub 해소 | 보유인데 유니버스 밖인 11종 — `core_refresh._retained_symbols` 가 보유를 유지하는지 재확인, 아니면 피처 보강 경로 추가 |

### 스코프 밖 (변경 없음)

`value_trade` / `value_scan` 로직 · gap / `close_scan` · Toss TV 통일 ·
`combine_stop_target` 밴드 · `sanitize` 레벨 하드가드 · `require_dossier` 게이트.

---

## 5. 리스크 / 반대 의견

- **B안 전체가 "도시어 품질이 뇌 판단보다 낫다"는 미검증 가설에 서 있다.** 현재 fresh
  도시어 60건 중 bullish 19(32%), neutral 39 — 즉 Athena 는 대부분 관망을 낸다.
  이 분포에서 퍼널을 도시어에 위임하면 신규 진입이 구조적으로 줄어든다. B2 합치도
  통계 없이 B3 를 하면 "유입 축소"를 "선별 개선"으로 착각할 수 있다.
- **B1 은 LLM 출력 필드 3개 추가 = Athena 토큰·실패율 증가.** `sonnet` 모델로 30/run
  이므로 파싱 실패 시 재시도 1회 비용이 붙는다. 채움률을 B1-6 에서 반드시 본다.
- **B0 픽스만으로 B안 목표의 상당 부분이 달성될 가능성.** 큐가 정상화되면 트리거된
  69종이 리서치되고 bullish 풀이 19 → 더 커진다. B0 후 2주 관측에서 커버리지가
  충분해지면 B1 이 불필요할 수도 있다 — **B0 결과를 먼저 보는 쪽을 권장.**
