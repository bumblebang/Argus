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
