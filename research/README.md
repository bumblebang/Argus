# research/ — lab only (런타임 아님)

이 트리는 **백테스트·슬리브 탐색·진단** 전용이다.
`src/` · `scripts/watch` · 라이브 데몬은 여기를 **import하지 않는다** (Gate G0 / Phase 4).

산출물·캐시는 `research/quant_review/data/` 에만 둔다.
`data/quant_review/` 는 과거 잔여 — 런타임이 읽지 않음. 옮기기:

```bash
argus doctor --migrate-research          # dry-run
argus doctor --migrate-research --apply  # → research/quant_review/data/
```

## 승격 금지 (얇은 표본)

워크스페이스 규칙: [`.cursor/rules/quant-thin-sample.mdc`](../../.cursor/rules/quant-thin-sample.mdc)

- 발동 n 한 자릿수 · OOS 블렌드 Δ만으로 **메인/flat 슬리브 승격 금지**
- flat 숏리스트는 **당일 held 룩어헤드 게이트 전** 승격 문구 금지
- 기준 엔진: `research/quant_review/flat_sleeve_rescan_live.py`
- 통과해도 기본은 **현금 + 섀도**. Book A 채택은 세후·슬립까지 본 뒤
- 현재 메인 = BASE DN12/20. Dcap20·233740 Book A = 철회

## 엔트리 인덱스 (`quant_review/`)

| 클러스터 | 대표 스크립트 | 메모 |
|----------|---------------|------|
| flat 슬리브 | `flat_sleeve_scan.py` · `flat_sleeve_rescan.py` · **`flat_sleeve_rescan_live.py`** · `flat_sleeve_deepen.py` · `promote_flat_sleeve_15m.py` | 승격 게이트=`rescan_live` |
| 하이브리드 | `sweep_hybrid.py` · `compare_hybrid.py` · `compare_hybrid_sens.py` | |
| v2 / overnight | `run_t5_v2.py` · `run_v2_*` · `explore_v2_wave2.py` · `diag_v2_*` · `v2_indep_*` | 삭제하지 않음(lab) |
| 엔진·데이터 | `engine_bar.py` · `engine_portfolio.py` · `data_toss_candles.py` · `fetch_toss_1m.py` | |
| 페이퍼 검토 | `paper_flat_portfolio.py` · `paper_tax_adjusted.py` | |
| 일괄 | `run_all.py` · `test_smoke.py` | |

런타임 옆 연구 덤프(scripts, 출력만 lab):

- `scripts/dump_krx_shadow.py` → `research/quant_review/data/` (승격 금지 배너 포함)

## 하지 않는 것

- lab 스크립트 대량 삭제·통합 (이번 Phase 범위 밖)
- flat_sleeve 결과를 메인 config / watch 슬리브로 조용히 승격
- `src/` 에서 `import research`
