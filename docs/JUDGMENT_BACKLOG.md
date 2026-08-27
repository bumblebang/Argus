# 종합 판단 백로그 (미채택)

> 목적: 이슈를 **조사·재현으로 확인한 뒤** 기록하고, 한꺼번에 채택/기각을 판단한다.
> 상태: `open` = 확인됨·미판단 · `unverified` = 주장만 · `decide`/`adopt`/`defer`/`reject`.
> 규칙:
> 1. **작성 전** 코드 추적 + (가능하면) 최소 재현. 추측만으로 올리지 않는다.
> 2. 여기 있다고 구현 착수하지 않는다. 종합 판단 세션에서만 승격.
> 3. 실운영 DB 증거는 있으면 `ops_evidence`, 없으면 `ops: unknown` 명시.

**최종 갱신:** 2026-08-27 (J12·J13 재현/감사 확정)

---

## 판단 대기 목록

| ID | 제목 | 확인 | 심각도(초안) | 상태 |
|----|------|------|-------------|------|
| J1 | 라이브 게이트↔원장 비원자 + inflight 예약 부재 | **재현됨** (현금 음수까지) | P0 | open |
| J2 | 미체결 미추적 → 동일 조건 재평가 시 중복 발주 | **재현됨** (존 진입·청산) | P0 | open |
| J3 | 재대사 흡수 체결 → realized_pnl 미반영 | **재현됨** (계정·store pnl) | P0 | open |
| J4 | min_lot 면제가 종목비중·주문상한 무력화 + 누적 | **재현됨** (cap0→1주, 반복매수) | P1 | open |
| J5 | 경로 컷오버가 싱글턴 락을 무력화(이중 기동) | **재현됨** (migrate/rollback) | P0 | open |
| J6 | LLM `proposal.market`이 자본·한도·live 판정 기준을 바꿈 | **재현됨** (과대사이징·청산스킵) | P0 | open |
| J7 | Athena invalidation→손절 덮어쓰기 + RR이 LLM 레벨에 종속 | **재현됨** (sanitize·overwrite·RR) | P1 | open |
| J8 | 파라미터 클램프가 스키마 키만·NaN 통과 → 손절 경로 사망 | **재현됨** (passthrough·NaN) | P0 | open |
| J9 | 캘리브레이션 입증이 노이즈로 사이징 평평 잠금 해제 | **재현됨** (비단조·동점·slice) | P1 | open |
| J10 | 게이트 사후분석이 실승률을 깎고 반사실과 나란히 비교 | **재현됨** (~43pp 왜곡) | P1 | open |
| J11 | 그림자 채점 `has_open_since` 생존편향 + 비용 0 | **재현됨** (보유취소·손절채점) | P1 | open |
| J12 | 널 게이트≠라이브(stance) → `delta_vs_gated` 오염 | **재현됨** (존재만 vs bullish) | P1 | open |
| J13 | 사전등록이 미집행(킬/pass/배선 없음) | **감사확정** (코드 전수) | P1 | open |

---

## J1 — 라이브 게이트↔원장 비원자 + inflight 예약 부재

### 확인 결과: CONFIRMED

폴링 창에 **다른 종목** BUY가 들어오면 게이트가 진행 중 주문을 모르고 승인한다. 재현에서 두 건 모두 place·체결 후 **원장 현금이 음수**가 되었다.

### 재현 (2026-08-26, 로컬)

조건: `cash=150_000`, 각 BUY notional `100×1000=100_000`. 스레드 A place 후 `get_order`에서 대기하는 동안 스레드 B(이종 심볼) `execute`.

| 결과 | 값 |
|------|-----|
| place 횟수 | 2 (AAA, BBB) |
| 둘 다 ok | True |
| 최종 cash | **-50_000** |
| 보유 | AAA 100, BBB 100 |

→ 매수여력 게이트가 예약 없이 두 번 통과한 직접 증거. 총익스포저·섹터도 같은 `_invested`/`buying_power` 스냅샷 경로라 **동일 구멍**(별도 수치 재현은 동형).

### 코드 경로

- `broker.execute`: 락1 `gate.check`→`place`→`_mark_inflight` → **락 해제·폴링** → 락2 `apply_fill`
- `_inflight`: **동일 심볼만** 거부 (`_reject_inflight`)
- `risk_gate.check` BUY: `buying_power` / `_invested`(+섹터) / `open_count` — 예약 입력 없음

### 완화되어 보이는 것 (구멍 아님)

- 동일 심볼 inflight: 폴링 **중**만 막음. J1은 이종 심볼.
- `reconcile` defer: 이중 apply 방지. 한도 예약 아님.

### ops_evidence

워크스페이스에 `data/bot.db` 없음 → **실운영 발생 빈도 unknown**. 코드+재현만으로 결함 확정.

### 후보·트레이드오프·판단 체크

- 후보: BUY 예약(권장) / 락 연장(비권장) / 전역 직렬
- 트레이드오프: 예약 해제 누락→과차단; 미체결 구간 보수적 거절
- [ ] 채택/보류/기각 · 범위(BUY만?) · 테스트: 위 재현이 거절로 바뀌는지

---

## J2 — 미체결 미추적 → 재평가 시 중복 발주

### 확인 결과: CONFIRMED

체결 0이면 원장·ARMED(또는 보유 qty) 무변, **working order 레지스트리/취소 없음**. 다음 평가가 같은 조건이면 **다시 place**.

### 재현 (2026-08-26, 로컬)

**존 진입** (`EntryExecutor._evaluate_zone`, 가격 존 안, `get_order`→PENDING):

| | 1회차 | 2회차 |
|--|------|------|
| place | O1 | O2 |
| executed | False (미체결) | False |
| armed | 유지 1건 | 유지 1건 |
| ledger qty | 0 | 0 |

`CONFIRMED_J2_ZONE`: places≥2, armed=1, qty=0.

**청산** (`ExitExecutor`, 보유 10주, SELL PENDING 두 번):

| | 결과 |
|--|------|
| place | 2회 |
| ledger qty | 10 유지 |
| execute ok | False, False |

`CONFIRMED_J2_EXIT`: places≥2, qty=10.

부가 확인: `broker.py`에 `cancel_order` 호출 없음. `toss_client.cancel_order`만 존재.

### 코드 경로

- `_finish_live` filled=0 → 이벤트만, `apply_fill` 없음, `finally`에서 `_clear_inflight`
- `EntryExecutor`: `if not res` → armed 유지. 존 모드: `low≤price≤high`면 매 호출마다 execute
- `loop.py`: 매 틱 `get_armed` → `entry_executor.evaluate`
- `ExitExecutor` / `StrategyRunner`: 미체결 시 보유 유지 → 다음 트리거/신호에 재 execute

### 경로별 발화 조건 (과장 없이)

| 경로 | 재발주 조건 | 확인 |
|------|------------|------|
| 존 진입 | 가격이 존 안인 채로 재평가 | **재현** |
| 손절 등 ExitExecutor | 보유 남고 트리거 재발화(가격이 손절 아래 유지 등) | **재현**(연속 2회 호출) |
| 전략 진입 `decide==BUY` | 신호가 틱마다 BUY | 코드 동일 구조, **별도 재현 안 함**(논리는 존과 동형) |
| 전략 청산 SELL | 신호가 틱마다 SELL | 코드 동일 구조, 별도 재현 안 함 |

### 위험 구간 (설정과 교차)

- `trading_sessions.KR`: `regular, premarket, aftermarket` — 시간외 거래 **켜짐**
- `limit_slippage_pct: 0.01` — 최우선호가 1% 안만 리밋
- `max_spread_pct_extended: 0.02` — 스프레드 과대면 **스킵**(완화). 스프레드가 좁고 깊이만 없으면 **미체결+재발주**는 그대로
- `entry_zone_guard: true` — 스윙/장투가 존 모드로 武装 → 존 경로가 실사용 설정

→ “시간외·갭이 위험”은 **설정상 그럴 수 있는 구간**으로 확인. 실틱 폭주 로그는 DB 없어 unknown.

### ops_evidence

`data/bot.db` 없음 → 실운영 `live_order_pending` 연속 여부 **unknown**.

### 후보·트레이드오프

1. Working order 레지스트리 + 미체결 동안 재발주 금지(+만료 취소)
2. 폴링 종료 filled=0 시 `cancel_order`
3. 존/진입 원샷·쿨다운(증상 완화, orphan은 남음)
4. J1 예약과 working notional 통합

### J1 관계

- J2만: 동일 조건 폭주↓, 이종 심볼 한도 우회(J1) 남음
- J1만: 여력 예약 가능, **토스 orphan working**은 남을 수 있음
- 종합 시 working 수명주기 + 게이트 예약을 한 설계로 보는 것이 자연스러움

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 정책: 대기 vs 즉시 취소 vs 쿨다운
- [ ] 전략 진입/청산도 같은 테스트로 고정할지

---

## J3 — 재대사가 흡수한 체결은 realized_pnl에 안 잡힘

### 확인 결과: CONFIRMED

`realized_pnl` / `realized_pnl_today` 갱신은 `PaperAccount.apply_fill`의 **SELL 분기뿐**.  
주기 재대사(`apply_reconcile_from_live`)는 `cash`·`positions`만 덮어쓰고 이 경로를 우회한다.  
폴링 창 밖에서 체결된 매도(미체결 종료 후 실체결 → 재대사 흡수)는 **계정 실현손익이 영구히 0 가산**.

### 재현 (2026-08-27, 로컬)

1. BUY 10@1000 → 원장 qty=10, `realized_pnl={}`
2. 실계좌에서 전량 매도된 것처럼 재대사: `cash=1_002_000`, `items=[]`
3. 결과:

| 항목 | 재대사 후 | apply_fill SELL@1200 이었으면 |
|------|-----------|------------------------------|
| positions qty | 0 | 0 |
| cash | 1_002_000 (API 덮어씀) | 일치 가능 |
| `realized_pnl` | **{}** | KR=2000 |
| journal SELL | **없음** | 있음 |
| store close | state=closed, **exit_price=None, pnl=None** | pnl 확정 |
| `get_closed_positions()` (pnl NOT NULL) | **0건** | 1건 |

부가: `_ledger_already_has_fill`로 `apply_fill` 스킵 시에도 실현손익 미가산 재현.

### 코드 근거

- `paper_account.apply_fill` SELL만 `realized_pnl` / `realized_pnl_today` += pnl, journal append
- `broker_sync.apply_reconcile_from_live`: `account.cash[m]=…`, `account.positions=dict(live_pos)` — **pnl/journal 손대지 않음**
- 유령 청산: `exit_px=_last_sell_price(account)` ← journal SELL 없으면 None → `store.close_position` pnl NULL
- `sync_open_qty` 부분매도 store 귀속은 journal SELL 가격이 있을 때만(테스트가 journal을 **미리** 넣어 증명). 재대사 단독으로는 journal을 안 씀
- `broker._finish_live`: 재대사가 이미 qty 반영 시 apply_fill **스킵** → 같은 구멍

### 영향 (무엇이 깨지고 무엇이 괜찮은지)

| 소비자 | 영향 |
|--------|------|
| 일손실 게이트 (`daily_realized_pnl`) | 매도 손실/이익 누락 → **한도 느슨/왜곡** |
| DD 브레이커 (`realized_pnl`+미실현) | 실현분 누락 |
| report/dashboard paper `realized_pnl` | 과소 |
| attribution (`get_closed_positions`) | pnl NULL 행 제외 → **누락** |
| 원장 cash·qty | API로 맞춤 → **포지션/현금 자체는 대체로 OK** |

“영원히”: 계정 `realized_pnl`을 보정하는 다른 writer 없음(로드/저장만). 재대사만으로는 회복 안 됨.

### J2와의 관계

J2(미체결 후 working 방치) → 폴링 밖 체결 → 재대사 흡수 → **J3 발화**.  
J2를 고쳐도 부분체결·재시작·수동매도 고아는 재대사 경로로 J3가 남을 수 있음.

### 후보 (미채택)

1. 재대사 시 qty 감소/소멸 감지 + 토스 체결/주문 API로 실체결가·수수료 조회 후 journal+realized+store pnl
2. `_ledger_already_has_fill` 스킵 시에도 체결 스냅샷으로 realized만 보강
3. 게이트 일손실을 실계좌 당일 손익 API로(원장과 이원화 — 신중)
4. 체결가 없는 avg 추정 가산은 **비권장**(숫자만 채우고 틀림)

### 트레이드오프

- 정확한 가산 = API·복잡도↑
- cash는 이미 맞아서 “리포트만”으로 보이지만 **리스크 게이트가 이 숫자를 씀**

### 반박 / 고려

- 기동 `sync_from_live`도 pnl 미초기화(과거 실현 모름 — 의도 가능). J3 핵심은 **봇 주문의 폴링 밖 체결**이 재대사로만 흡수될 때.
- 실계좌 스냅샷 대시보드와 paper realized가 어긋날 수 있음.

### ops_evidence

워크스페이스 `bot.db` 없음 → 실운영 빈도 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 체결가 출처(주문 API vs 추정 금지)
- [ ] J2 설계와 한 패키지로 묶을지

---

## J4 — min_lot 면제가 종목비중·주문상한을 무력화하고 누적된다

### 확인 결과: CONFIRMED

`allow_min_lot: true`(운영 `config.yaml`·example 기본). qty==`min_lot_qty`(1) BUY는 게이트에서 **주문상한·종목비중 면제**(현금·gross·보유수·킬스위치는 유지 — 코드 주석과 일치).

사이징 구멍: `size_buy`가 `notional_cap`으로 budget을 0까지 깎은 뒤, min_qty 부활 조건을 **캡/budget이 아니라 분모 전액(`base`)** 과 비교한다 → headroom=0인데도 1주 복원.

면제 자체는 고단가 floor=0 보완용 **의도된 기능**. 제거보다 **절대 상한 + 1회성 제약**이 맞다는 방향(사용자 제안, 미채택).

### 재현 (2026-08-27, 로컬)

```text
size_buy(357k, w=0.12, notional_cap=0)           → 0
size_buy(..., min_qty=1, notional_cap=0)         → 1   # CONFIRMED_RESURRECT
```

게이트: 이미 1주@357k 보유 + 추가 1주 BUY  
- `allow_min_lot=True` → 승인  
- `False` → 거부(재현에선 주문상한 357k>200k)

누적(브로커 paper, headroom=0·min_qty=1 반복 3회):  
seed 1주 → **4주**. `CONFIRMED_ACCUM`.

### 코드 근거

- `risk.size_buy` (핵심):
  - `budget = min(base*pct, notional_cap)` → qty floor
  - 그다음 `if min_qty>0 and qty<min_qty and price*min_qty <= base: qty=min_qty`  
    ← **`base`(equity/capital)이지 notional_cap/budget 아님**
- `risk_gate.check`: `min_lot`이면 주문상한·종목비중 스킵
- 호출측: `cycle.py` / `execution.py`가 `headroom = equity*hard_cap - cur_notional`을 `notional_cap`으로 전달 — 이미 한도 초과면 headroom=0
- `min_lot_adjust`: 확신도 문턱 이상이고 1주 못 사면 weight를 1주분으로 올리고 `min_qty=1`
- 게이트 코드 기본은 `allow_min_lot=False`이나 **운영 config는 true**

### 영향

| 한도 | min_lot 1주 |
|------|-------------|
| 주문 절대캡 | 면제 |
| 종목 비중 | 면제 → **같은 종목 1주씩 피라미딩 가능** |
| 현금 | 유지 (현금 바닥까지) |
| 총익스포저·보유수·킬스위치 | 유지 |

고단가(예: 35만)면 1주만으로도 목표비중(20%)을 이미 넘긴 뒤에도 추가 1주가 통과한다.

### 후보 (미채택) — 사용자 방향 포함

1. **절대 상한**: min_lot여도 `max_order_notional` 또는 `price ≤ X` / notional ≤ 고정원 상한
2. **1회성**: 종목당(또는 armed당) min_lot 시범 1회만 — 이미 보유·이미 시범이면 면제·부활 금지
3. `size_buy` 부활 조건을 `price*min_qty <= min(base, budget|notional_cap)`로 고쳐 headroom=0이면 0 유지
4. 게이트에서 종목비중만은 면제 해제(주문상한만 면제) — 면제 축소

1+2가 “없애기보다 절대 상한·1회성”에 가깝다. 3은 사이징 정합 최소 패치.

### 트레이드오프

- 1회성·절대캡 → 초고단가 시범 기회↓ (의도된 보수화)
- 면제 제거 → floor=0 구멍 재발(기존 테스트 `test_min_lot_*`가 보호 중)
- `size_buy`만 고치면 게이트 면제로 **수동/다른 경로 qty=1**은 남을 수 있음 → 게이트·사이징 함께 보는 편이 안전

### 반박 / 고려

- 현금·gross가 최종 안전망이라 “무한”은 아님. 다만 **종목비중 하드한도의 의미는 깨짐**.
- 확신도 `min_lot_conviction` 미달이면 사이징이 안 올림 — 뇌/존 경로의 누적은 확신도 OK일 때.

### ops_evidence

실DB 없음 → 운영 누적 빈도 unknown. 코드+재현으로 결함 확정.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 절대캡 수치·1회성 키(심볼 vs armed_id)
- [ ] size_buy 부활 조건 수정 포함 여부

---

## J5 — 경로 컷오버가 싱글턴 락을 무력화해 이중 기동을 허용

### 확인 결과: CONFIRMED

락은 `<pidfile>.lock`에 걸리지만, `pidfile` 자체는 `paths.resolve("watch_pid")`가 **파일 존재(비어 있지 않음)** 로 LAYOUT vs 레거시를 고른다.  
`MIGRATE_MOVES`는 `watch.pid`만 옮기고 **`.lock`은 목록에 없음**. 커널 락은 원자적이지만 **어느 inode/경로에 락을 걸지가 FS 상태에 따라 바뀜** → 서로 다른 `.lock`을 쥐면 두 프로세스가 동시에 통과.

문서는 장중 `--apply` 금지를 체크리스트로만 적고, `doctor`/`paths_migrate`는 **장중·실행 중 검사를 하지 않음**. 롤백(state→legacy로 pid 되돌림)도 반대 방향으로 동일 상태를 만든다.

### 재현 (2026-08-27, 로컬)

**컷오버 중 A가 레거시 락 보유:**

1. A: `resolve` → `data/watch.pid`, 락=`watch.pid.lock` acquire
2. `apply_moves`: pid `moved` → `data/state/watch.pid`; **legacy `.lock` 잔존**, state `.lock` 없음
3. B: `resolve` → `data/state/watch.pid`, 락=`state/watch.pid.lock` **acquire 성공**
4. `CONFIRMED_DUAL_AFTER_MIGRATE_WHILE_A_HOLDS` True (서로 다른 lock 파일)

**롤백 방향:**

1. A: state pid + `state/watch.pid.lock` 보유
2. pid만 legacy로 `shutil.move`
3. B: `resolve` → legacy → `data/watch.pid.lock` acquire 성공  
4. `CONFIRMED_DUAL_AFTER_ROLLBACK` True

부가: `MIGRATE_MOVES`에 `.lock` 없음; `run_migrate`/`paths_migrate`에 `is_open`/장중 가드 없음.

### 코드 근거

- `singleton.SingleInstance`: `lockfile = pidfile.name + ".lock"` (pidfile과 같은 디렉터리)
- `paths.resolve`: 존재 우선 LAYOUT → configured → LEGACY; 없으면 LAYOUT 쓰기 기본
- `paths.MIGRATE_MOVES`: `data/watch.pid` → `data/state/watch.pid`만 (`.lock` 없음)
- `watch`/`orchestrator`: `SingleInstance(_paths.resolve("watch_pid"))`
- `docs/OPS_CUTOVER.md`: “장중·뇌 사이클 중 파일 이동 금지” / “하지 말 것: 장중 --apply” — **문서만**
- `doctor.run_migrate`: apply 시 시장·프로세스 검사 없음

### 발화 조건 (과장 없이)

| 상황 | 이중 기동? |
|------|-----------|
| watch **정지 후** migrate (문서대로) | 보통 아님 — orphan `.lock`은 잠금 해제 상태 |
| watch **실행 중** migrate/--apply | **됨** (재현) |
| 실행 중 롤백(pid만 반대 이동) | **됨** (재현) |
| 부분 컷오버로 pid가 한쪽에만 있음 + 단일 기동 | 정상 단일 락 |

즉 “컷오버 API가 항상 이중 기동”이 아니라, **resolve 가변 + lock 미이동**이 체크리스트 위반·실수 시 싱글턴을 깨뜨린다. 코드가 그 실수를 막지 않음.

### 영향

- watch 이중 → 토큰 경합·주문 이중·뇌 동시 스폰 (singleton 도입 사유와 정면 충돌)
- heartbeat/pid 관측도 resolve가 가리키는 쪽만 보여 오탐/미탐 가능

### 후보 (미채택)

1. **락 경로 고정**: 논리 키 `watch_lock`을 LAYOUT에만 두거나, 레거시·신 **양쪽 `.lock`을 모두** acquire(AND)
2. migrate 시 `watch.pid.lock`도 이동/삭제 계획에 포함 + **적용 전** pid 생존·락 보유 검사로 거부
3. `doctor --migrate-data --apply`에 장중/`SingleInstance` try-lock 가드
4. resolve를 “존재 우선” 대신 watch_pid는 **항상 LAYOUT**(쓰기)로 고정 — 레거시는 읽기 전용 관측만

### 트레이드오프

- 양쪽 lock AND → 컷오버 중 기동 자체가 막힐 수 있음(오히려 안전)
- lock 파일 이동은 Windows에서 잠긴 파일 move 실패 가능 → **실행 중 migrate 거부가 선행**돼야 함
- 문서만 강화는 재발 방지 부족(이미 체크리스트 있음)

### 반박 / 고려

- 문서 준수 시 실측 빈도는 낮을 수 있음. 그래도 싱글턴이 **운영 규율에 의존**하면 설계 목표가 깨진 것.
- heartbeat 등 다른 dual-resolve 키도 비슷한 관측 혼선 가능(본 J는 락 무력화가 핵심).

### ops_evidence

실운영 이중 pythonw 로그는 워크스페이스에 없음 → 빈도 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 고정 락 vs 양쪽 AND vs migrate 가드 우선순위
- [ ] 롤백 절차에 lock 정리 명시 여부

---

## J6 — LLM `proposal.market`이 자본 풀·한도·live 판정 기준을 바꿈

### 확인 결과: CONFIRMED

`Proposal.market`은 `Literal["KR","US"]` 스키마 검증만 하고, **후보/유니버스의 실제 시장과 코드 대조가 없다**.  
그 값이 `size_buy`/`sizing_base_amount`/`buying_power`/`_cap`/`_exposure_base`/`Order.market`/`live_markets`/`arm_candidate`까지 흘러간다.  
악의적 LLM이 아니라 **틀린 라벨만**으로 한쪽은 과대 사이징, 한쪽은 청산 불능(조용히 스킵).

### 재현 (2026-08-27, 로컬)

| 시나리오 | 결과 |
|----------|------|
| `Proposal(005930, market=US)` | 스키마 **통과** |
| US가 종목(가격 200) + `size_buy("KR", …)` vs `"US"` (자본 KR=1e7, US=0) | KR 라벨 **qty=10000**, US 라벨 **qty=0** |
| `Order(AAPL, KR, BUY, 1000@200)` 게이트 | **승인** (KR 여력·한도) |
| `Order(005930, US, BUY, 1@70000)` 게이트 (US cash=0) | 매수여력 부족으로 거부 |
| 보유 005930 + `Order(..., US, SELL)` / `live_markets=["KR"]` | place 없이 스킵, qty 유지 |
| `ExitExecutor` + store `market=US` | 동일 스킵 (`live_markets 밖`) |

`ValidationAgent.review`에 후보 market 하드체크 없음.  
`CycleRunner._universe_item`은 심볼만 찾고 루프 변수 `_market`을 **버리고** `return it` — 교정 소스도 안 씀.

### 코드 경로

- `cycle.py`: `equity = risk.sizing_base_amount(broker, p.market)` → `Order(p.symbol, p.market, …)`
- `cycle_runner.arm_candidate(sym, proposal.market, …)`
- `loop` 청산: `executor(sym, pos.get("market", market), …)` — store에 박힌 LLM 라벨
- `broker._prepare_live_order`: `order.market not in live_markets` → 스킵(BUY/SELL 공통)
- `apply_fill`: `symbol_market[symbol] = market` — 잘못된 라벨이 원장에 고착

### 운영 config와의 교차 (과장 없이)

현재 `capital.US: 0` + KR-only `live_markets`이면:

| 오라벨 | 신규 BUY | 청산 SELL |
|--------|----------|-----------|
| 미장 종목 → `market=KR` | **과대주문 가능** (KR 원 자본 ÷ 달러가) | live KR이라 주문 시도(심볼/호가는 별 문제) |
| 한국 종목 → `market=US` | 여력 0으로 거절되기 쉬움 | **live_markets 밖 스킵 → 청산 불능** |

즉 “양방향 대칭 과대”는 현 config에선 아니고, **과대는 US→KR 라벨**, **청산불능은 KR보유+US라벨(또는 store/주문 market=US)** 이 실측 위험.

### 후보 (미채택)

1. **코드 권위 market**: 유니버스/심볼규칙(KR 6자리 등)/`symbol_market`으로 `proposal.market` 덮어쓰기 또는 불일치 시 거부
2. 검증 사전거부에 candidate.market 불일치 하드룰
3. live 청산은 `live_markets`가 아니라 **심볼의 실제 거래소/원장 market**만 사용
4. LLM에 market 필드를 빼고 코드가 채움

### 트레이드오프

- 심볼 휴리스틱(6자리=KR)은 예외 티커에 깨질 수 있음 → 유니버스 맵이 더 안전
- market을 코드 전용으로 하면 스키마/프롬프트/브릿지 판사 프롬프트 동기화 필요

### 반박 / 고려

- 컨텍스트 후보에 올바른 market이 실려 있어 LLM이 보통 복사한다 — 그래도 **하드 게이트가 아니라 신뢰**다.
- US 자본 0은 BUY 오라벨 한쪽을 완화하지만 SELL 스킵·US→KR 과대는 막지 않음.

### ops_evidence

실DB 없음 → 빈도 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 덮어쓰기 vs 거부와 유니버스 부재 심볼 정책
- [ ] Exit/armed/store market 일괄 교정 범위

---

## J7 — Athena invalidation이 손절을 덮어쓰고, RR·확신도는 LLM 레벨에 종속

### 확인 결과: CONFIRMED (수식 방향은 주장과 반대 → 교정)

맞음:
- `sanitize()`는 **양수·순서**(`inv < lo <= hi < target`)만 본다. 현재가 대비 위치·밴드 폭·변동성 대비 타당성 **없음**.
- 도시에 `invalidation`이 있으면 코드 `entry_stop_target` 손절을 **무조건 덮어씀**.
- `compute_rr` 주석은 “LLM 산수 대신 코드가 계산”이지만, 분자·분모 레벨은 **LLM이 고른 값** → 객관 RR이 아님.
- 그 RR이 `score_buy`에서 확신도 ±가산 (`≥2` +0.08, `≥1.5` +0.04, `<1.5` −0.10).

**틀린 주장(교정):** “무효화가를 **내릴수록** RR이 커진다”  
공식 `RR=(target−mid)/(mid−invalidation)`에서 inv↓ → 분모↑ → **RR↓**. 재현: inv 990→5.0, 950→2.33, 100→0.19.

**실제 인센티브 정렬:**
| LLM 조작 | RR·확신도 | 손절(덮어쓰기 후) |
|----------|-----------|-------------------|
| inv를 진입에 **가깝게 올림** | RR↑ → 가산 | **얇은 손절**(쉽게 털림) |
| target **허상 상향** | RR↑ → 가산 | 손절과 무관하게 가산 |
| inv를 **멀리 내림**(넓은 손절) | RR↓ → **감점** | 보호는 느슨(위험) |

“손절 제거(넓은 손절)와 RR 가산이 같은 방향”은 **성립하지 않음**.  
대신 **얇은 손절+허상 목표가 = RR 가산**이 같은 손잡이고, **넓은 손절은 RR 패널티와 공존**(사이징은 깎여도 포지션 손절은 위험하게 넓음).

### 재현 (2026-08-27)

- `sanitize(inv=100, lo=1000, hi=1050, tgt=1200)` → bullish 유지, notes=[]
- `sanitize(inv=990, tgt=5000)` → bullish, rr≈113 (현재가·폭 무검사)
- `entry_stop_target(1025, swing, 5%)` → 973.75 → dossier inv=100으로 덮으면 **100**
- `score_buy`: inv=950 rr=2.33 → 0.56; inv=100 rr=0.19 → 0.38 (가산이 아니라 감점)

### 코드 근거

- `athena.sanitize`: None/≤0, 순서만 → 아니면 neutral
- `athena.compute_rr`: mid=(lo+hi)/2, risk=mid−inv
- `conviction.score_buy`: bullish면 `_rr_of`로 W_RR_*
- `cycle_runner` (orphan/open 승격): `stop, target = entry_stop_target(...); if d.invalidation: stop = d.invalidation`
- 존 진입: `plan_fn`이 stop=`zone["invalidation"]`

### 후보 (미채택)

1. sanitize에 현재가 대비 inv 거리 상·하한, 존 폭 ATR/% 캡, target 캡
2. 손절 = `max(code_stop, invalidation)` 또는 `min` 정책 명시(넓은 LLM 손절 거부)
3. RR 확신도 가산을 **코드 손절·코드 목표** 기준으로만 계산(도시에 레벨과 분리)
4. 도시에 RR을 사이징에 쓰지 않음(게이트·저널만)

### 트레이드오프

- inv를 코드 손절보다 타이트하게만 허용하면 Athena “무효화 논리” 표현력↓
- RR 가산 제거 시 사이징 신호가 단순해짐

### 반박 / 고려

- 최초 주장의 inv↓→RR↑는 수식과 반대. 다만 **sanitize 부재 + 덮어쓰기 + LLM 레벨→RR→확신도** 구조 문제는 그대로다.
- 넓은 손절은 RR 감점을 받지만 **여전히 포지션에 기록**될 수 있어 “감점되면 안전”이 아님.

### ops_evidence

실도시에 분포 미조회 → 빈도 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 손절 결합 규칙(max/min/거부)
- [ ] RR 가산 유지 여부·기준 가격

---

## J8 — 파라미터 클램프가 스키마 키만 먹고 NaN은 통과 → 손절 경로 사망

### 확인 결과: CONFIRMED

`Strategy.validate` 마지막이 스키마 외 키를 **그대로 통과**(주석에도 명시).  
`stop_loss_pct`를 `ParamSpec`으로 가진 전략은 **8개 중 2개**(volatility_breakout, bollinger_breakout). 나머지 6개에 같은 키를 실으면 클램프 없이 `entry_stop_target`→손절가가 됨.

NaN은 (1) 스키마 외 통과뿐 아니라 (2) **스키마 키 `ParamSpec.coerce`도** `float('nan')` 후 `nan < min`/`nan > max`가 모두 False라 경계로 안 잘림.  
Pydantic v2 `Proposal.params: dict[str, float]`는 NaN/Inf 허용(재현).

NaN 손절가: `position_triggers`의 `if stop:`는 NaN이 truthy라 들어가지만 `price <= stop`이 항상 False → **stop_hit 무발화**.  
`thesis_watch.check_price`의 `price < lim`도 False → **가격 무효화 무발화**.

### 재현 (2026-08-27)

| 검사 | 결과 |
|------|------|
| PARAMS에 `stop_loss_pct` | 2개만 / 6개 없음 |
| `validate_params('rsi_reversion', {stop_loss_pct: 0.99})` | **0.99 통과**, viol=[] |
| `validate_params('volatility_breakout', {stop_loss_pct: nan})` | **nan 통과**, viol에 stop 관련 없음 |
| `entry_stop_target(10000, swing, {0.99})` | stop=100 (사실상 전액 손절) |
| `entry_stop_target(..., {nan})` | stop=nan |
| `position_triggers(..., stop_price=nan, price=1)` | **[]** |
| `check_price(1, {price:nan})` | **None** |
| `Proposal(..., params={stop_loss_pct: nan})` | 수용 |
| `params={x: inf}` | 수용 |

### 코드 근거

```text
base.Strategy.validate:
  for spec in PARAMS: coerce → out[name]
  for k,v in params: out.setdefault(k, v)   # 스키마 외 통과

ParamSpec.coerce:
  v=float(value)  # nan OK
  if v < min / v > max  # nan 비교 False → 그대로 반환

wiring.entry_stop_target:
  stop = entry * (1 - stop_loss_pct)  # nan → nan; 0.99 → 1% 잔존

triggers.position_triggers:
  if stop:                 # nan truthy
    if price <= stop:      # 항상 False
```

### 영향

- 스윙/포지션 전략(rsi·macd·ma 등)에 뇌가 `stop_loss_pct`를 실으면 **하드 가드 우회** (0.99 등)
- NaN 손절 → 빠른손 `stop_hit` + thesis 가격무효화 **동시 불능** (전략 SELL·시간 손절 등은 별개)

### 후보 (미채택)

1. `coerce`에서 `math.isfinite` 강제, 비정상→default + viol
2. 스키마 외 키 **드롭** 또는 allowlist(`candle_interval` 등만)
3. `stop_loss_pct`/`target_profit_pct`를 전 전략 공통 PARAMS(또는 entry_stop_target 전 별도 클램프)
4. `position_triggers`/`check_price`에서 non-finite stop 무시+에러 이벤트
5. Proposal `params`에 `field_validator`로 finite-only

### 트레이드오프

- 스키마 외 전면 드롭 시 `candle_interval` 등 의도적 통과 키를 allowlist로 빼야 함
- 공통 stop_loss PARAMS는 전략 decide()와 손절 경로 의미가 달라 문서화 필요

### 반박 / 고려

- “클램프가 스키마만”은 주석상 **의도**일 수 있으나, `entry_stop_target`이 스키마 밖 `stop_loss_pct`를 읽는 한 하드 가드 주장은 거짓.
- NaN은 스키마 유무와 무관하게 coerce 구멍.

### ops_evidence

실저널 params 분포 미조회 → 빈도 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] finite 강제 vs 스키마 외 드롭 vs 공통 stop 스펙 우선순위

---

## J9 — 캘리브레이션 “입증”이 노이즈로 사이징 평평 잠금을 푼다

### 확인 결과: CONFIRMED

`sizing_enabled`는 config가 켜져 있어도 `conviction_calibration(...).calibrated`가 True일 때만 확신도 사이징을 켠다.  
입증 조건은 사실상:

```text
n >= 20
and len(rates) >= 2
and rates[-1] >= rates[0]
```

여기서 `rates` = bin별 `n >= 5`인 구간의 `hit_rate`만 BINS 순으로 모은 리스트.

- **중간 bin 단조성 없음** (V자여도 양 끝만 보면 통과)
- **통계 검정 없음**
- **Brier는 계산만** 하고 `calibrated`에 미사용 (`EVAL_PROTOCOL`도 Brier mis-spec 언급)
- **동점 허용** (`>=`)
- 표본은 `get_closed_positions` **행 단위** — `parent_id`로 안 묶음. 같은 store의 `attribution`/`lessons`는 묶음 → 부분매도 시 **거래 1건이 표본 여러 건**

### 재현 (2026-08-27)

| 시나리오 | 결과 |
|----------|------|
| rates=[0.3, 0.0, 0.6] (중간 붕괴) | `calibrated=True`, `sizing_enabled=True` |
| 저·고 bin hit 둘 다 0.5 (동점) | `calibrated=True` |
| 부분매도 3 slice, parent_id 동일 | cal `n=3`, attribution group **1** |
| `brier` 존재 | 플래그와 무관하게 True 가능 |

### 코드 근거

- `calibration.py` 84–93행: 위 조건만으로 `calibrated=True`
- Brier 79–81 계산 후 return에만 실림
- 루프: `for row in store.get_closed_positions` — slice마다 `(conviction, win)` append
- `attribution._trade_group_id`: `parent_id`로 묶음 (캘리브와 불일치)
- `cycle_runner`: `conv_sz = sizing_enabled(store, configured=True)`

### 영향

- 의도: 캘리브 전 사이징 평평(보수)
- 실제: 얇은/왜곡 표본으로 잠금 해제 → 확신도 배율(floor~cap)이 **노이즈에 열림**
- 부분매도·피라미딩이 많은 계좌일수록 n이 부풀어 `MIN_N=20`에 빨리 도달

### 후보 (미채택)

1. 전 bin(또는 유효 bin) **비감소 단조성** + 동점 금지 또는 최소 기울기
2. `parent_id`로 1거래 집계 후 pnl 합으로 win 판정 (attribution과 동일)
3. Brier/로그손실 상한 또는 bootstrap CI로 게이트
4. bin별·전체 n 상향, 최소 유효 bin 수 ≥ 3
5. 캘리브 통과를 대시보드 관측만 하고 사이징 잠금은 수동 플래그

### 트레이드오프

- 엄격화하면 잠금이 오래 감(의도일 수 있음)
- parent 묶으면 표본이 줄어 해제 더 늦음

### 반박 / 고려

- 주석의 “단조 대략”은 양 끝 비교만이라 중간 역전을 허용 — 노이즈 해제의 핵심.
- win 정의가 `pnl>0`이라 수수료 후 본전은 패로 잡힘(별 이슈, 본 J와 독립).

### ops_evidence

실 bot.db 캘리브 스냅 없음 → 운영 해제 여부 unknown.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 단조성·parent 묶음·Brier 중 최소 세트
- [ ] 자동 해제 유지 vs 수동 승격

---

## J10 — 게이트 사후분석이 실승률을 깎고 반사실과 나란히 비교한다

### 확인 결과: CONFIRMED

`scripts/_gate_postmortem.py`의 “실제” 승률:

```sql
SELECT ... FROM positions WHERE state='closed'
-- pnl IS NOT NULL 없음, qty>0 없음
actual_wins = sum(1 for r in closed if (r[3] or 0) > 0)  # pnl null → 0 → 패
```

반면 store API는 구분함: `get_closed_positions`는 `pnl IS NOT NULL`, `closed_trades`는 `qty>0`.

분모에 패배로 들어가는 가짜 청산:
- **armed 해제** (`disarm:*`, qty=0, pnl null) — 매수 없음
- **dedupe** (`dedupe:duplicate_open` 등, pnl null)
- **분할매도 부모행** (qty=0으로 닫히며 pnl null; slice는 별도 승/패)

같은 JSON의 **반사실(counterfactual)**:
- 정의: `entry=prior daily close`, `win=forward close > entry`
- **수수료 0 · 세금 0 · 손절 0** (가격 경로만)
- 헤드라인에 `actual_closed.win_rate`와 `counterfactual.overall.d*`를 **나란히** 둠 → 실거래는 깎이고 막힌 쪽은 부풀려 보임

### 재현 (2026-08-27)

합성: 실승 2 + disarm null + dedupe null + partial 부모 null + slice 승 2  
→ 포스트모템식: n=7, wins=4, **wr=0.571**  
→ `pnl IS NOT NULL AND qty>0`: n=4, wins=4, **wr=1.0**  
→ **Δ ≈ 42.9pp** (주장 41.5pp와 동형)

저장된 `data/gate_postmortem.json`: n=3 중 null 1건 → 보고 0.667 vs 필터 시 1.0 (**33.3pp**). CF d5는 0.677(비용·손절 없음).

### 코드 근거

- `_gate_postmortem.py` 219–223, 304–314 (`actual_closed`) / 225–274, 314–325 (`counterfactual`)
- `store._dedupe_open_positions` / `disarm` / `record_partial_exit` 부모 마감: pnl 미설정

### 영향

- 게이트 “우리가 막아서 손해/이득” 헤드라인이 **측정 편향**
- 운영 판단·PR에 쓰면 실전략 승률을 과소, 차단 반사실을 과대 평가

### 후보 (미채택)

1. actual: `pnl IS NOT NULL AND qty>0` (+ 선택적으로 parent_id 1거래 집계)
- disarm/dedupe/부모 qty=0 제외
2. CF에 보수적 비용(수수료·세금) 및/또는 손절 가정 명시, 또는 헤드라인 분리(“비교 금지” 배너)
3. 스크립트를 `get_closed_positions` / attribution과 동일 정의로 통일
4. one-shot 스크립트라면 대시보드·의사결정 입력에서 제거

### 트레이드오프

- CF에 손절을 넣으면 가정 논쟁(어느 손절?) — 최소한 **비대칭 가정 고지**는 필요
- parent 묶음은 J9와 동일 이슈

### 반박 / 고려

- 41.5pp는 특정 DB 구성에 따른 수치; 재현은 **~43pp** 동형. 저장 JSON은 null 1건이라 33pp.
- 반사실 자체가 무의미한 건 아님 — **실측과 같은 척도로 나란히 두는 것**이 문제.

### ops_evidence

`data/gate_postmortem.json` 존재(2026-08-21): actual 0.667 with null pnl row; cf d5 0.677.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] actual 필터 최소선 / CF 비용 가정 / 비교 UI 분리

---

## J11 — 그림자 채점 `has_open_since` 생존편향 + 비용 0

### 확인 결과: CONFIRMED

`score_open_shadows`가 pending 그림자를 지울 때 `has_open_since(symbol, entry_ts)`만 본다. 구현은 **현재 `state='open'`** 행만:

```sql
SELECT 1 FROM positions WHERE symbol=? AND state='open' AND opened_at >= ?
```

채점 시점에 이미 손절·청산된 실포지션은 안 보인다 → **아직 들고 있는(대개 유리한) 케이스만 취소**, **손절 후 케이스만 살아남아 호라이즌 종가로 채점**.

편향 방향: **「손절 안 했으면 벌었다」**. 채점식은 `(exit/entry-1)*100` — **수수료·세금·슬리피지 0** (실거래 왕복 비용과 비대칭).

### 재현 (2026-08-27, 로컬)

동일 pending 그림자·entry=100·horizon 20일·일봉 종가 선형 +1/일.

| 케이스 | 채점 전 실포지션 | `has_open_since` | score 결과 | ret |
|--------|------------------|------------------|------------|-----|
| A 보유 중 | open | True | **cancelled=1** | — |
| B 손절 후 | close @95 (−5%) | False | **scored=1** | **+20.0%** (호라이즌 종가 120) |

→ 실거래 −5%인데 그림자는 +20%로 생존·채점. 방향이 주장과 일치.

예시 KR 왕복(config.example): `2×fee 0.015% + tax 0.15% + 2×5bp slip` ≈ **0.28%** (주장 0.31%와 동형 구간).

### 코드 근거

- `store.has_open_since` — `state='open'` only
- `shadow_ledger.score_open_shadows` — pending일 때만 위 체크로 cancel; **hard-block `state=open` 그림자는 이 분기 자체를 안 탐**
- 채점 ret: 가격비 순수, 비용 필드 없음
- `cancel_shadow_on_fill`: execution / value_trade / cycle_runner / broker_sync(adopt) 등 **체결 직후** 호출 — happy path면 채점 전 취소로 완화
- `broker.apply_fill` 자체에는 cancel 없음; 재대사 **기존 보유 qty 갱신** 경로도 cancel 없음 → 채점 시 안전망이  asymmetrically 동작

### 영향

- 그림자 승률·avg_ret가 **손절 생존 표본 + 무비용 호라이즌**으로 상향
- attribution/`shadow_stats`로 게이트·슬리브 판단에 쓰면 **「막아서 / 손절해서 손해」** 쪽으로 기울기 쉬움
- **J10 CF**와 같은 족보: 반사실은 손절·비용 0, 실측은 깎임

### 후보 (미채택)

1. `has_open_since` → `had_open_or_closed_since` (닫힌 포지션·동일 심볼 entry 이후 포함) 또는 filled 플래그
2. 채점 ret에 paper와 동일 왕복 비용 차감(또는 집계에 비용 가정 명시)
3. hard-block open 그림자에도 “실체결 이후면 취소” 동일 규칙
4. `cancel_on_fill`을 `apply_fill`/재대사 갱신까지 단일화해 잔여 의존 제거(1과 병행)

### 트레이드오프

- closed-since만 보면 “예전에 다른 이유로 샀다 판” 오취소 가능 → symbol+시간창+thesis/sleeve 매칭 필요할 수 있음
- 비용 가정도 시장·사이즈에 따라 달라짐 — 최소한 **비대칭 고지** vs 숫자 보정

### 반박 / 고려

- 주요 체결 경로에 `cancel_shadow_on_fill`이 있어 **ops 빈도는 cancel 누락·레이스에 좌우**될 수 있음. 그래도 채점 안전망 로직 자체는 생존편향이 맞음.
- hard-block(`state=open`) 그림자는 이 cancel 분기 밖 — 별도·더 큰 무손절 CF일 수 있음.

### ops_evidence

워크스페이스 `data/bot.db` 없음 → **실운영 발생 빈도 unknown**. 코드+재현으로 결함 확정.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] closed-since / 비용 / hard-block 범위 · J10과 한 묶음 보정 여부

---

## J12 — 널 게이트 ≠ 라이브(stance) → `delta_vs_gated` 오염

### 확인 결과: CONFIRMED

라이브 스윙/장투 BUY 하드게이트는 **신선한 `stance=='bullish'` 도시에**.  
널 `eligible_candidates`는 `require_dossier`일 때 **도시레 dict 존재만** 보고 stance를 안 본다.

문서(`EVAL_PROTOCOL.md`)는 “도시레/존/슬롯을 아카이브 스칼라로 재현한 통과 집합”이라 쓰지만, 라이브와 **동일 게이트가 아님**.  
그 집합 차이로 널이 사는 종목·못 사는 종목이 갈리면, 그 차이가 그대로 `delta_vs_gated = mean_live − mean_null_gated`에 섞인다. **매니저 스킬 0이어도 Δ≠0**.

### 재현 (2026-08-27)

후보: BULL(bullish) / NEUT(neutral) / BEAR(bearish) / NONE(무도시레), 존·가격은 통과 가능.

| | 통과 집합 |
|--|-----------|
| 널 `eligible_candidates` | BULL, NEUT, BEAR |
| 라이브 bullish 전제 | BULL만 |

- 널이 NEUT/BEAR를 BUY 가능, 라이브 스윙은 불가 → **게이트 불일치 확정**.
- skill=0(라이브·널 각각 해시 랜덤, n_buy=1)이어도 픽 불일치율 ~52%(3000시드). Δ 평균은 fwd 가정에 따라 **양·음 모두** (아래 반박).

### 코드 근거

- 라이브: `cycle_runner._has_bullish_dossier` → `stance == "bullish"`; `cycle.run_cycle` `no_dossier` 차단
- 널: `null_manager.eligible_candidates` — `if require_dossier and not d: continue` (stance 미검사). 존/무효화 스칼라 필터는 있음
- 채점: `score.score_journal` → `null_random_gated` → `_summary["delta_vs_gated"]`

### 영향

- “널 대비 알파”가 **선택 스킬 + 게이트 정의 차이**의 혼합물
- 양의 Δ로 Athena 프롬프트·stance·`require_dossier`를 손대면 **측정 오염으로 판단 소스 오염**(아래 메모)
- J10/J11과 같은 족: 반사실/베이스라인이 실게이트·비용과 비대칭

### 후보 (미채택)

1. 널 eligible에 `stance=='bullish'`(+신선도는 아카이브에 있으면) 정렬 — 라이브와 동일 풀에서만 랜덤
2. Δ를 `delta_vs_gated_same_pool` / `delta_gate_diff`로 분해 보고
3. day 예외(도시레 없이 arm)도 널·라이브 대칭 명시
4. 문서에 “존재≠bullish” 경고 + 대시보드 승격 문구 금지 강화(이미 score note에 승격 금지는 있음)

### 트레이드오프

- bullish로 맞추면 Δ는 “게이트 통과 후 선택”만 측정 — **Athena stance 자체의 품질은 안 봄**(의도적으로 범위 축소)
- stance까지 널에 넣지 않고 “코드 재현 가능 스칼라만”을 고수하면 문서의 “동일 게이트” 주장을 버려야 함

### 반박 / 고려

- **「상승장이면 Δ 항상 양수」는 기각.** 비bullish 풀의 fwd가 더 좋으면 Δ는 음수(재현: 전 종목 상승·NEUT>BULL이면 mean_delta&lt;0, frac_pos=0).  
  확정되는 것은 **부호가 아니라 오염(스킬 0인데 Δ≠0)**.
- `can_promote(replay_score)`는 이미 False(NO_PROMOTE) — 오염 Δ가 **자동 승격**하진 않음. 위험은 **사람이 Δ를 보고 프롬프트/stance를 손댈 때**.

### 판단 메모 (stance / bullish·bearish를 건드릴 소스?)

연결은 있다. 다만 방향이 한 가지가 아님.

1. **잘못된 소스**: 오염된 `delta_vs_gated`로 “뇌가 알파가 있다/없다”를 읽고 Athena 프롬프트·stance 정의를 고치면, 고치는 대상이 **측정을 정의하는 쪽**이라 순환·착시.
2. **올바른 분리**: stance 품질은 널 랜덤이 아니라 **bullish vs neutral/bearish 라벨의 전방 수익·hit율** 같은 **라벨 평가**로 봐야 함. 널을 bullish에 맞추면 선택 스킬 측정은 깨끗해지지만 stance 검증은 여전히 별도.
3. J13(사전등록 미집행)과 겹치면: 보호망이 연극인 상태에서 J12 Δ를 근거로 `risk_gate`/검증 규칙을 바꾸기 쉬움.

종합 판단 때: J12 보정(풀 정렬)과 “stance 변경의 허용 증거”를 **한 안건으로** 묶을지 검토.

### ops_evidence

실저널 Δ 분포는 워크스페이스 DB/저널에 의존 → **ops 부호·빈도 unknown**. 코드+합성 재현으로 결함 확정.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 널에 bullish 정렬 vs Δ 분해 vs 문서만 정정
- [ ] stance 변경 증거 표준을 J12와 분리할지

---

## J13 — 사전등록 프로토콜이 미집행(킬·pass·배선 없음)

### 확인 결과: CONFIRMED (코드 전수 감사)

`eval_protocol` / `EVAL_PROTOCOL.md`는 사전등록·kill·PROTECTED 승격 게이트처럼 읽히지만, **집행 코드가 없다.**

| 주장(문서/필드) | 실제 |
|-----------------|------|
| `kill_if` 문자열 평가·자동 `kill` | **저장만.** `src/`에서 `kill_if` 참조는 `eval_protocol.py`뿐. eval/파서/스케줄러 없음 |
| `status='pass'` | `register_experiment`는 `"registered"`만 세팅. **pass로 올리는 코드 경로 없음**(테스트가 JSON을 손으로 고침) |
| registry 무결성 | `save_registry` = 파일 **전체 `write_text` 덮어쓰기** — 사후 필드 수정·pass 기입에 기계적 제약 없음 |
| `can_promote`가 변경을 막음 | 호출처: `score.py`(항상 `replay_score`→NO_PROMOTE False) + **테스트만**. `risk_gate` / `exit_policy` / `athena` / 프롬프트·config 적용 경로에 **미연결** |
| PROTECTED에 `risk_gate`,`exit_policy` | 목록만 존재. 해당 모듈은 `can_promote`를 import조차 안 함 |

문서 규칙 4(“kill 조건 충족 시 즉시 kill”)는 **aspirational**.

### 코드 근거

- `eval_protocol.py`: `PROTECTED`, `register_experiment`, `can_promote`(status in pass/running, min_n, touches) — kill_if 미사용
- `save_registry`: `p.write_text(json.dumps(reg...))`
- grep: `can_promote(` = score + tests; `kill_if` = protocol + docs + test register args

### 영향

- “등록했다”는 **감사 로그 수준**이지 승격 락이 아님
- J12 오염 Δ·그림자 Δ와 결합하면, thin-sample/게이트 착시를 **프로토콜이 막아주리라 기대하기 위험**
- `exit_policy`가 PROTECTED에 있는 것은 청산 바닥 보호 의도이나, **실제 프롬프트/리스크게이트 변경 경로와 무관**

### 후보 (미채택)

1. `kill_if`를 구조화(임계값 필드) + score/attribution 후 자동 status 갱신(문자열 eval 금지)
2. `status` 전이를 append-only 이벤트 로그로(덮어쓰기 레지스트리 폐기 또는 서명)
3. config/프롬프트 적용·PR 체크에 `can_promote` 강제(또는 CI)
4. 집행 전엔 문서를 “수동 체크리스트”로 격하하고 대시보드에서 승격 CTA 제거

### 트레이드오프

- 자동 kill DSL은 복잡도·오탐; 구조화 필드가 단순
- CI 강제 없으면 로컬 config 편집은 계속 우회 가능 — **의도된 마찰** vs 형식만

### 반박 / 고려

- 리플레이 Δ로 메인 승격은 이미 `NO_PROMOTE`로 코드가 막음 — J13의 구멍은 **PROTECTED 실변경 경로 미배선**과 **kill/pass 연극**
- 사람이 JSON에 pass를 쓰는 운영을 “신뢰”로 둘 수도 있으나, 그때는 프로토콜이 아니라 **약속**임. 코드 게이트라고 부르면 과대광고

### ops_evidence

`data/eval_registry.json` 실사용 여부는 환경 의존 → **unknown**. 미집행은 저장소 코드만으로 확정.

### 판단 체크

- [ ] 채택/보류/기각
- [ ] 자동 집행 vs 문서 격하 vs CI만
- [ ] J12와 묶어 “평가 인프라” 안건으로 볼지

---

## 다음에 이어서

새 이슈: **코드 추적 → 재현(또는 명시적 불가 사유) → 표에 추가**.  
`unverified`로 올리지 말 것. 종합 판단 세션에서는 이 파일의 `CONFIRMED` 항목만 안건으로.
