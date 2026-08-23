# 사전등록 평가 프로토콜 (Argus)

백테스트가 약한 LLM 재량 시스템에서 **사후 숫자로 전략을 고치지 않는다.**
변경 전에 가설·지표·kill 기준을 `data/eval_registry.json` 에 등록한다.

## 규칙

1. **PROTECTED** (`main_sleeve`, `flat_sleeve`, `exit_policy`, `risk_gate`,
   `conviction_weights`, `validation_rules`) 변경은 등록된 실험 id 없이 금지.
2. 그림자 장부 Δ·OOS 블렌드 Δ만으로 승격 금지 (workspace thin-sample 규칙과 동일).
3. `min_n` 미달 → `status=shadow_only` / "관심 섀도" 라벨만.
4. kill 조건 충족 시 즉시 `kill` — 파라미터를 고쳐가며 재시도하지 않음 (새 실험 등록).

## 등록 예시

```python
from src.eval_protocol import register_experiment, can_promote

register_experiment(
    name="verifier_rule3_relax",
    hypothesis="rule3 완화 시 vetoed 반사실 ret가 악화되지 않는다",
    metric="shadow.by_bucket['검증:규칙거부'].avg_ret_pct",
    kill_if="avg_ret_pct < filled_actual - 2pp after n>=30",
    min_n=30,
    touches=["validation_rules"],
)
ok, why = can_promote(change="validation_rules", evidence_n=12,
                      experiment_id="exp_...")
# ok=False — 표본 부족
```

## 매니저 에포크

모델/프롬프트 변경 시 `manager.epoch` 가 바뀐다. 이전 에포크 표본으로 새 매니저를
채점하지 않는다. attribution `manager_epochs` 참고.

## LLM 재량 경계 (#5)

- **뇌(LLM)**: thesis·방향·(카탈로그 내) 전략 선택·스틸맨.
- **코드**: 사이징 루브릭·하드 게이트·존/무효화·시간손절·수수료.
- 전략 **파라미터는 카탈로그 범위로 클램프** (`resolve_strategy`). 범위 밖 LLM 값은 무시.
- 캘리브레이션 전 `conviction_sizing` 은 평평 (코드가 강제).

## PIT 컨텍스트 아카이브

라이브 `run_cycle` 이 LLM에 넘긴 `context_json` 을 gzip 으로 동결한다.
경로: `data/context_archive/{YYYY-MM-DD}/{cycle_ts}_{sha16}.json.gz`.
저널에는 `context_ref` / `context_sha256` / `context_bytes` 포인터만 남긴다.
아카이브 쓰기 실패는 사이클을 죽이지 않는다.

- 아카이브는 라이브 시점 입력이라 PIT 가 깨끗하다.
- `track_record` / `past_trades` 는 **당시 매니저 성과** — 상황 재현에는 맞고 독립 스킬은 아니다. 기본 리플레이는 JSON 그대로. `--strip-track-record` 는 별 모드.
- 새 **모델** 리플레이: `--min-date` = 학습 컷오프 다음날. 그 이전은 채점 제외.
- 새 **프롬프트·같은 모델**: 아카이브 전체 가능. 결과는 새 `epoch`.
- **과거 컨텍스트를 market_state 로 역생성하지 않는다** (look-ahead). 아카이브 이후 날짜만 채점.
- 리플레이 출력은 라이브 `decisions` / `track_record` / store 에 **쓰지 않는다**.

## 판단 단위 채점 (체결이 아니라 제안)

대상: 아카이브 사이클의 **candidates 전원** × 매니저 액션 (`BUY`/`HOLD`/`SELL`, 제안 없는 후보는 `HOLD`).

- `fwd_ret` = horizon 종가 / 판단일 종가 − 1.
- 정책 수익 (포트 복리 없음): `1[side==BUY] * fwd_ret`. HOLD = 0.
- HOLD가 의미 있으려면 **널 대비 Δ** 가 필수. 절대 수익만 보면 상승장 착시.
- `min_n` 미달 → `shadow_only`. 승격 문구 금지.

## 널 매니저

LLM 없이, 아카이브에 찍힌 후보·제약만 사용.

- `null_cash`: 전부 HOLD.
- `null_random_gated`: 재현 가능한 코드 게이트(도시레 유무, 진입존, max_positions)를 통과한 집합에서 `sha256(cycle_ts|symbol)` 순으로 당시 BUY 개수만큼 추출. 같은 시드면 재현.

해석: LLM 정책수익 − 널 정책수익 = 게이트 통과 후 종목 선택의 상대엣지.
**널 대비 없이 스킬을 주장하지 않는다.** 결론이 "널과 구분 안 됨"이면 그게 결과다.

## 리플레이 하네스

`scripts/replay_manager.py`

- `score-live`: 저널 액션 vs 라벨 vs 널 (LLM 콜 0).
- `redecide`: 동결 컨텍스트 → 새 DecisionAgent. `broker.execute` / `run_cycle` 집행 분기 **호출 금지**.

`--min-date`, `--sleeve`, `--strip-track-record`.

**순차 포트 리플레이는 v1 금지.** 포트는 이전 판단에 의존한다. 판단 단위만 유효하다.

리플레이/널 Δ (`replay_score`, `null_manager`, `context_replay`, `consistency`) 로는 `can_promote` 가 항상 False.

## 일관성 (P1, 오프라인만)

`scripts/consistency_manager.py`: 같은 컨텍스트를 N회 재결정.
종목별 side 일치율, Fleiss' kappa, 국면 버킷.
라이브 다수결 앙상블·표결 마진 집행은 넣지 않는다.

## 점예측 (P2)

`Proposal.p_target_before_stop` (선택, 0~1): BUY + 도시레 target/invalidation 있을 때만.
conviction 과 다르다 — conviction 은 코드가 덮어쓰는 매매 확신, p 는 **경로 확률**.
스키마 추가 = 새 `manager.epoch` — 이전 표본과 섞지 않는다.

사후 라벨: 도시레 `target`/`invalidation` 으로 `target_hit_before_stop` 이진.
`score-live` 의 `proper_score` 블록이 Brier/log-loss 를 적립한다.
`min_n` 미달 → `proper_score.status=shadow_only`.

기존 conviction Brier(`calibration.py`) 는 확률이 아니므로 mis-specified — 사이징 게이트 용도만.
proper_score 와 혼동하지 마라.
