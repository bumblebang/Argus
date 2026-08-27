# Ops cutover — data/ 레이아웃 이동 (Phase 2)

**장중·뇌 사이클 중 파일 이동 금지.** 장후 또는 주말에만 적용한다.

코드(경로 dual-resolve)는 레거시 `data/*` 와 신 `data/state|inbox|ledgers/*` 를
둘 다 찾는다. 이 문서는 **물리 이동** 절차다. 이동 전에도 운영은 레거시만으로 가능하다.

## 사전 조건

- [ ] Gate G0 + full pytest 그린 (이동 코드 머지 후)
- [ ] `argus doctor --migrate-data` dry-run 에 `conflict` 없음
- [ ] 백업 대상 확보: `bot.db`, `paper_account.json`, `llm_inbox/`, `HALT`(있으면), `decisions.jsonl`, `brain_mode.json`

## 중지 순서

1. Windows 작업 스케줄러 **ArgusWatch** 중지 (또는 해당 상주 작업 Disable)
2. 브릿지 `bridge_tick.py --serve` / hb 창 종료
3. `data/state/watch.pid`(또는 레거시 `data/watch.pid`) 없거나 stale, heartbeat age ≫ 60s 확인
4. 대시보드 8787 이 watch 인프로세스면 함께 내려감 — 별도면 중지

## 이동

```bat
cd /d <argus-root>
.venv\Scripts\argus.exe doctor --migrate-data
.venv\Scripts\argus.exe doctor --migrate-data --apply
```

`--apply` 는 `bot.db` 이동 직전 `PRAGMA wal_checkpoint(TRUNCATE)` 를 한 뒤 옮긴다.
WAL 사이드카(`bot.db-wal`/`-shm`)만 두고 메인만 옮기면 **원장 꼬리가 과거로 롤백**된다.
watch 를 문서대로 내려도 checkpoint 없이 수동 복사하면 같은 사고가 난다 — 반드시
doctor `--apply` 경로를 쓰거나, 수동이면 이동 전 checkpoint 한 번.

- inbox: `data/llm_inbox` → `data/inbox` 이동 후 **`data/llm_inbox` junction** (PokeTokenBarWin 무수정)
- state: `bot.db`, `paper_account.json`, heartbeats, pid, HALT, brain_* → `data/state/`
- ledgers: `decisions.jsonl` → `data/ledgers/`

## 검증

1. `argus doctor` — path:* 가 `layout` / exists
2. `argus watch --dry --ticks 1` (또는 `python scripts/watch.py --dry --ticks 1`)
3. 대시보드 `http://127.0.0.1:8787` (재기동 후)
4. 브릿지 재기동 후 `data/inbox/bridge.heartbeat` (또는 `llm_inbox` junction 경유) 90s 내 갱신
5. PokeTokenBarWin 브릿지 armed 표시 (형제 레포 있으면)
6. `data/bot.db-wal` / `data/state/bot.db-wal` 고아가 새로 생기지 않는지(정상 종료면 truncate)

## 재기동

1. ArgusWatch Enable + Run
2. 브릿지 serve 재개
3. 15~30분: heartbeat, 에러 로그, 의도치 않은 실주문 여부

## 롤백

1. ArgusWatch / 브릿지 다시 중지
2. 백업에서 레거시 평탄 `data/` 복구 (신 디렉터리 파일은 백업본으로 덮어쓰기 또는 삭제 후 복구)
3. 코드가 dual-resolve 이면 **레거시만 있어도** 기동 가능 — 이전 커밋으로 되돌릴 필요는 보통 없음
4. junction `data/llm_inbox` 가 깨졌으면 삭제 후 실디렉터리로 복구

```bat
rmdir data\llm_inbox
xcopy /E /I backup\llm_inbox data\llm_inbox
```

## 하지 말 것

- 장중 `--apply`
- `bot.db` 스키마 변경·vacuum을 이 컷오버에 묶기
- 레거시 경로 **조기 삭제** (shim/별칭 제거는 별 승인)
- **watch 실행 중·checkpoint 없이** `bot.db` 만 복사/이동 (WAL 꼬리 유실)
