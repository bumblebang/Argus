"""등록 실험 자동 채점 + 도시에 bullish 비중 하한 감시 (Tier 0).

평일 측정 작업(run_measurement_reports)이 부른다. 하는 일:
  1. 레지스트리 실험 중 지표를 계산할 줄 아는 것만 채점 → apply_kill_rules.
     상태가 바뀌면(shadow_only→pass/kill 등) 알림 대상으로 돌려준다.
  2. 신선 도시에 bullish 비중이 시장별 하한 아래면 알림 대상 — 성적 피드백이
     bullish 를 과하게 줄여 스윙 매수 후보가 마르는 과보정을 조기에 잡는다.

kill 이 나도 스위치를 자동으로 끄지 않는다(실계좌 동작) — 사람에게 알리기만 한다.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..eval_protocol import DEFAULT_PATH, apply_kill_rules, load_registry
from .dossier_quality import dossier_stance
from .stance_excess import dossier_observations, stance_excess

# dossier.<MKT>.bullish_minus_neutral_excess_pp — prompt_rev 한정 bullish−neutral 초과수익 차이
_STANCE_SPREAD = re.compile(r"^dossier\.(KR|US)\.bullish_minus_neutral_excess_pp$")

DEFAULT_BULLISH_FLOOR = 0.10
MIN_FRESH_FOR_FLOOR = 10


def compute_metric(exp: dict, *, store, data_dir: Path | str,
                   cfg: dict | None = None) -> dict[str, Any] | None:
    """실험 지표 → {metric: 값, metric__n: n}. 모르는 지표면 None."""
    metric = str((exp.get("kill") or {}).get("metric") or exp.get("metric") or "")
    m = _STANCE_SPREAD.match(metric)
    if not m:
        return None
    mkt = m.group(1)
    since = float(exp.get("created_at") or 0)
    obs = dossier_observations(store, since=since, prompt_rev=exp.get("prompt_rev"),
                               evidence_match=exp.get("evidence_match"))
    labels = (stance_excess(obs, data_dir=data_dir, cfg=cfg)["by_market"].get(mkt) or {})
    bull, neu = labels.get("bullish") or {}, labels.get("neutral") or {}
    n = int(bull.get("n") or 0)
    val = None
    if bull and neu:
        val = round(float(bull["excess_avg_pp"]) - float(neu["excess_avg_pp"]), 2)
    return {metric: val, f"{metric}__n": n,
            f"{metric}__neutral_n": int(neu.get("n") or 0)}


def evaluate_experiments(*, store, data_dir: Path | str, cfg: dict | None = None,
                         registry_path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """채점 가능한 running 계열 실험을 채점. 반환: {evaluated: [...], changed: [...]}."""
    reg = load_registry(registry_path)
    evaluated, changed = [], []
    for exp in reg.get("experiments") or []:
        if exp.get("status") == "kill":
            continue
        metrics = compute_metric(exp, store=store, data_dir=data_dir, cfg=cfg)
        if metrics is None:
            continue
        for c in apply_kill_rules(metrics=metrics, path=registry_path):
            if c.get("id") == exp.get("id"):
                changed.append(c)
        evaluated.append({"id": exp.get("id"), "metrics": metrics})
    after = {e["id"]: e for e in load_registry(registry_path).get("experiments") or []}
    for e in evaluated:
        cur = after.get(e["id"]) or {}
        e["status"] = cur.get("status")
        e["status_reason"] = cur.get("status_reason")
    return {"evaluated": evaluated, "changed": changed}


def bullish_floor_check(store, *, cfg: dict | None = None, prompt_rev: str | None = None,
                        now: float | None = None) -> list[dict]:
    """신선 도시에 시장별 bullish 비중이 하한 미만이면 위반 목록.

    prompt_rev 를 주면 그 판본 도시에만 판정한다(피드백이 바꾼 건 새 판본뿐 — 옛 판본
    비중이 원래 낮은 국면을 과보정으로 오인하지 않게). 비교용으로 다른 판본 비중도 싣는다.
    """
    floor = float(((cfg or {}).get("athena") or {}).get("bullish_floor_pct",
                                                        DEFAULT_BULLISH_FLOOR))
    cur: dict[str, list[int]] = {}
    other: dict[str, list[int]] = {}
    for r in store.list_fresh_dossiers(now=now):
        row = dict(r)
        rev = None
        try:
            ev = json.loads(row.get("evidence") or "{}")
            rev = ev.get("prompt_rev") if isinstance(ev, dict) else None
        except (TypeError, ValueError):
            pass
        bucket = cur if (prompt_rev is None or rev == prompt_rev) else other
        c = bucket.setdefault(str(row.get("market") or "KR"), [0, 0])
        c[1] += 1
        if dossier_stance(row) == "bullish":
            c[0] += 1
    out = []
    for mkt, (bull, total) in sorted(cur.items()):
        if total < MIN_FRESH_FOR_FLOOR:
            continue
        share = bull / total
        if share < floor:
            hit = {"market": mkt, "bullish": bull, "fresh": total,
                   "share": round(share, 3), "floor": floor}
            ob, ot = other.get(mkt, [0, 0])
            if ot:
                hit["other_share"] = round(ob / ot, 3)
                hit["other_fresh"] = ot
            out.append(hit)
    return out


# 알림은 결론이 난 전이만 — shadow_only(표본 부족)는 몇 주간 정상 상태라 소음.
_ALERT_STATUSES = frozenset({"pass", "kill"})


def alert_lines(result: dict, floor_hits: list[dict]) -> list[str]:
    """푸시 본문 줄 — pass/kill 전이·하한 위반만(변화 없으면 빈 목록)."""
    lines = []
    for c in result.get("changed") or []:
        if c.get("status") not in _ALERT_STATUSES:
            continue
        lines.append(f"실험 {c.get('name') or c.get('id')}: {c.get('status')} — "
                     f"{c.get('status_reason')}")
        if c.get("status") == "kill" and "athena" in (c.get("touches") or []):
            lines.append("  → 되돌리기: config.yaml athena.stance_feedback: false")
    for h in floor_hits:
        prev = (f", 이전 판본 {h['other_share']:.0%}" if h.get("other_share") is not None
                else "")
        lines.append(f"도시에 bullish 비중 [{h['market']}] {h['share']:.0%} "
                     f"({h['bullish']}/{h['fresh']}{prev}) < 하한 {h['floor']:.0%} — "
                     "성적 피드백 과보정 의심")
    return lines


def dedupe_daily(lines: list[str], state_path: Path, *, now: float | None = None) -> list[str]:
    """같은 줄은 하루 한 번만 — 평일 매일 도는 작업이 같은 위반을 반복 푸시하지 않게."""
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        sent = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    fresh = [ln for ln in lines if sent.get(ln) != day]
    if fresh:
        sent.update({ln: day for ln in fresh})
        try:
            state_path.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return fresh
