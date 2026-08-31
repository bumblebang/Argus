# LIVE 관찰 체크리스트 (8템플릿 · strategy_stats · 배선)

> **목적:** 템플릿 고도화·배선 수정 **전에** 무엇을 얼마나 봤는지 기록한다.
> **판단 금지선:** 전략×시장 **거래수 n&lt;5** → `small_sample`, 승률 해석·코드 변경 보류.
> **갱신:** 2026-08-30

---

## 어디를 보나 (SSOT)

| 어디 | URL / 경로 | 용도 |
|------|------------|------|
| 대시보드 | `http://127.0.0.1:8787` | 일상 관측 |
| **오늘** 탭 | 대시보드 | 라이브 체결·차단·armed·시장심리 |
| **성과** 탭 | 대시보드 | 전략별 성과(store 청산)·거래별 실현손익·그림자 |
| **시스템** 탭 | 대시보드 | watch heartbeat·프로세스 |
| 결정 원장 | `data/ledgers/decisions.jsonl` | 뇌 proposal·strategy·thesis |
| DB | `data/bot.db` (`positions`·`events`) | 청산·exit_reason·strategy 컬럼 |
| 로그 | `logs/watch.log` | 발굴·갭스캔·체결·에러 |
| 스냅샷 | `python scripts/measurement_baseline.py` | strategy_stats·캘리브·그림자 한 번에 |

---

## A. 매일 (5분) — LIVE가 도는지

- [ ] **watch 살아 있음** — 시스템 탭 heartbeat / `watch.log` 최근 타임스탬프
- [ ] **오늘 탭 · 오늘 봇 거래** — `live_order` / `live_order_error` / `buy_blocked` 비율
- [ ] **armed·보유** — 진입대기 건수, `strategy`·`stop`·`target` null 아닌지
- [ ] **갭/close_scan** (KR 장중) — `gap_rebound_scan` wake 로그, 갭풀 후보 등장 여부

---

## B. 매주 — strategy_stats (1·2번)

**볼 곳:** 성과 탭 **「전략별 성과 (store 청산)」** 또는 `measurement_baseline.py` 출력.

| 항목 | 무엇을 보나 | 메모 |
|------|------------|------|
| 거래수 n | 전략×시장별 | **n&lt;5 → small_sample, 판단 보류** |
| 승률 | wins/n | n≥5부터 추세만 참고 |
| avg_ret_pct | 평균 수익률(원금대비) | 승률과 같이 |
| total_pnl | 실현손익 합 | 토스 앱과 대조(대시보드 히어로) |
| exit_reason | `strategy:*` / `stop_hit` / `brain` / `close_scan` | 청산이 템플릿 vs 뇌 vs 갭인지 |

**트랙 분리해서 적기 (섞으면 착시):**

| 트랙 | store `strategy` / meta | 구분 |
|------|-------------------------|------|
| brain 스윙/장투 | 8템플릿명 | discovery 후보 |
| value | `value` / `value_trade` | 밸류 워치리스트 |
| close_scan | horizon·meta `close_scan` / gap 풀 | 15:20 wake 전용 |
| day | `volatility_breakout` 등 + horizon=day | armed 템플릿 진입 |

**아직 하지 말 것:** n 한 자릿수 템플릿 “폐기/승격”, 파라미터 대량 튜닝.

---

## C. 매주 — 3번 배선 (discovery ↔ 템플릿)

**볼 곳:** `decisions.jsonl` 최근 BUY proposal + 후보 피처(뇌 입력에 실린 경우).

한 사이클당 종목마다 적을 것:

| 체크 | 필드 | “어긋남” 예 |
|------|------|-------------|
| 후보 풀 | `pool` / `source` / `momentum_20d` | discovery(거래대금↑)인데 20d -20% |
| 셋업 | thesis·base_rates `active_now` | `pullback_reversal` active인데 `momentum` 배정 |
| fit vs 배정 | `strategy_fit.best` vs `proposal.strategy` | fit 1위 `rsi_reversion`, 실제 `donchian_breakout` |
| horizon | `horizon` vs 전략 카탈로그 | swing인데 day 템플릿 |
| 트랙 겹침 | gap 풀 태그 + `strategy` | close_scan 종목에 day breakout |

**판단 문턱:** 같은 패턴이 **≥3건 / 2주** 반복될 때만 “배선 조정” 안건. 1~2건은 뇌 변동.

**조정 후보 (패턴 확인 후):**

- discovery 후보만 reversion 템플릿 → 발굴 필터 또는 스윙 화이트리스트
- strategy_fit 무시 → 프롬프트/가중치 (코드 작음)
- close_scan ↔ day 겹침 → 갭풀 태그 시 day 배정 차단 (일부 테스트已有)

---

## D. 월 1회 — 승률·운용 종합

- [ ] **성과 탭** — brain / value / close_scan **트랙별** n·승률·MDD(거래별 최대 낙폭 눈대)
- [ ] **캘리브** — `measurement_baseline` → `calibrated` 여부 (n≥20 전엔 사이징 잠금 정상)
- [ ] **그림자** — 성과 탭 shadow avg_ret (게이트 막은 BUY vs 실제)
- [ ] **운용 이상** — J1~J13 adopt 이후: 현금 음수·중복 주문·재대사 pnl 누락 **재발 없음** 확인

---

## E. “이제 손댈까?” 트리거 (AND)

| # | 조건 |
|---|------|
| 1 | 관심 트랙(brain 8템플릿)에서 **전략×시장 n≥5** (가능하면 ≥10) |
| 2 | **2주 이상** LIVE 중단·데이터 구멍 없음 |
| 3 | 3번 mismatch가 **반복**이거나, 특정 템플릿만 **구조적** underperform (같은 exit_reason·같은 풀) |
| 4 | quant 승격·메인 변경은 **별도** — `quant-thin-sample` 규칙 유지 |

---

## F. 관찰 로그 (복붙용)

```
날짜:
LIVE: OK / 이슈( )
오늘 체결: n=  차단: n=
전략별 n (brain만): 
  rsi_reversion KR n= wr=
  ...
3번 mismatch 이번 주: n=  예시 심볼:
다음 액션: 관찰 유지 / 배선 검토 / ( )
```

---

## 관련 문서

- `docs/JUDGMENT_BACKLOG.md` — 실행·측정 J1~J13 adopt (2026-08-27)
- `CONTEXT.md` — 운용 SSOT
- `.cursor/rules/quant-thin-sample.mdc` — quant 승격 금지 (Argus 템플릿과 별개)
