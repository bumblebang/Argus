"""Athena 리플레이 — 과거 (종목, 날짜)를 그 날 시점 데이터로 다시 판정해 A/B 비교.

질문: 재무·수급·공매도를 넣으면 Athena 의 stance 가 종목을 더 잘 가르나?
- A안(base): 차트(technical)·베이스레이트만 — 지금까지 유니버스 91% 의 실제 조건.
- B안(inputs): A안 + 재무·수급·공매도(AthenaInputs, asof 이전 공개분만).
두 안은 같은 프롬프트·같은 모델·같은 표본이고 입력만 다르다. 뉴스·시황·매크로는 과거
시점 재현이 어려워 **두 안 모두 뺀다**(실전보다 정보가 적은 공정 비교).

표본은 라이브 도시에가 실제로 만들어진 (종목, 날짜) 중 20일 창이 끝난 것만.
리플레이 결과는 라이브 store·저널에 쓰지 않는다(전용 jsonl). 승격 근거 아님(Tier 0).
"""
from __future__ import annotations

import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..logging_setup import get_logger
from .labels import asof_local_date, symbol_market

log = get_logger("eval.athena_replay")

ARMS = ("base", "inputs")
LEAN_SUFFIX = "_lean"          # "inputs_lean" = inputs 컨텍스트를 경량 CLI 로 판정


def split_arm(arm: str) -> tuple[str, bool]:
    """"inputs_lean" → ("inputs", True)."""
    if arm.endswith(LEAN_SUFFIX):
        return arm[: -len(LEAN_SUFFIX)], True
    return arm, False


# ── 표본 ──────────────────────────────────────────────────────────
def sample_cases(store, *, data_dir: Path | str, market: str | None = None,
                 since: float, until: float, n: int, seed: int = 7,
                 window_days: int = 20, cfg: dict | None = None) -> list[dict]:
    """라이브 도시에 (종목, 생성일) 중 창이 끝난 종목 에피소드를 n개 고른다.

    라이브 stance 별 층화(bullish 를 표본의 절반까지) — bullish 가 ~20% 라 단순 추출이면
    A/B 차이를 볼 bullish 표본이 너무 적다.
    """
    from .stance_excess import dossier_observations, stance_episodes

    obs = dossier_observations(store, since=since, now=until)
    if market:
        obs = [o for o in obs if symbol_market(o["symbol"]) == market]
    rows, _ = stance_episodes(obs, data_dir=data_dir, cfg=cfg)
    seen: set[tuple[str, str]] = set()
    uniq = []
    for r in rows:                                   # 같은 (종목, 날짜) 한 번만
        key = (r["symbol"], r["start"].isoformat())
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rng = random.Random(seed)
    bull = [r for r in uniq if r["label"] == "bullish"]
    rest = [r for r in uniq if r["label"] != "bullish"]
    rng.shuffle(bull)
    rng.shuffle(rest)
    k_bull = min(len(bull), n // 2)
    picked = bull[:k_bull] + rest[: n - k_bull]
    rng.shuffle(picked)
    return [{"symbol": r["symbol"], "market": r["market"], "asof": r["start"].isoformat(),
             "live_stance": r["label"], "live_excess_pp": r["excess_pp"]} for r in picked]


# ── 시점 컨텍스트 ─────────────────────────────────────────────────
def load_history_before(data_dir: Path | str, symbol: str, asof: date) -> pd.DataFrame | None:
    """history CSV 중 asof 이전 봉이 가장 많은 파일 → asof **전날까지** 일봉 df."""
    root = Path(data_dir) / "history"
    best: pd.DataFrame | None = None
    for pat in (f"{symbol}.KS_1d_*.csv", f"{symbol}.KQ_1d_*.csv", f"{symbol}_1d_*.csv"):
        for p in root.glob(pat):
            try:
                df = pd.read_csv(p)
            except Exception:
                continue
            df.columns = [str(c).strip().lower() for c in df.columns]
            tcol = "date" if "date" in df.columns else ("time" if "time" in df.columns else None)
            if tcol is None or "close" not in df.columns:
                continue
            df["time"] = pd.to_datetime(df[tcol].astype(str).str[:10], errors="coerce")
            df = df.dropna(subset=["time", "close"])
            df = df[df["time"].dt.date < asof].sort_values("time")
            if "volume" not in df.columns:
                df["volume"] = 0.0
            for c in ("open", "high", "low"):
                if c not in df.columns:
                    df[c] = df["close"]
            if best is None or len(df) > len(best):
                best = df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return best


def build_case_context(case: dict, arm: str, *, data_dir: Path | str,
                       inputs: Any = None, name: str | None = None) -> dict | None:
    """case → Athena 리서치 컨텍스트(그 날 시점). 봉 부족이면 None."""
    from ..agents.athena import build_research_context
    from ..agents.athena_inputs import fill_missing
    from ..baserate import analyze

    asof = date.fromisoformat(case["asof"])
    df = load_history_before(data_dir, case["symbol"], asof)
    if df is None or len(df) < 60:
        return None
    ctx = build_research_context(
        case["symbol"], name or case["symbol"], case["market"], history_df=df,
        market_state={}, base_rates=analyze(df), past_trades=[], focus={},
        live_news=False)
    for k in ("sentiment", "markets", "macro", "macro_kr", "flows_market",
              "program_flows", "short_market", "regime", "focus"):
        ctx.pop(k, None)                          # 두 안 공통 — 과거 재현 불가 슬롯
    if arm == "inputs" and inputs is not None:
        px = (ctx.get("technical") or {}).get("price")
        fill_missing(ctx, inputs.for_symbol(case["symbol"], case["market"], price=px,
                                            asof=asof, live=False))
    return ctx


# ── 토큰 계측 클라이언트 ─────────────────────────────────────────
def usage_cli_client(base_cls, **kw):
    """ClaudeCLIClient 를 `--output-format json` 으로 돌려 호출별 토큰·비용을 모은다.

    라이브 클라이언트는 건드리지 않는다(리플레이 전용 서브클래스).
    """
    class UsageCLIClient(base_cls):
        def __init__(self, **k):
            super().__init__(**k)
            self.base_args = [*self.base_args, "--output-format", "json"]
            self.calls: list[dict] = []

        def _invoke(self, prompt: str, model: str | None, *a, **k) -> str:
            raw = super()._invoke(prompt, model, *a, **k)
            try:
                data = json.loads(raw)
            except ValueError:
                self.calls.append({"model": model, "parse_error": True})
                return raw
            u = data.get("usage") or {}
            self.calls.append({
                "model": next(iter(data.get("modelUsage") or {}), model),
                "input_tokens": u.get("input_tokens"),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                "output_tokens": u.get("output_tokens"),
                "cost_usd": data.get("total_cost_usd"),
                "duration_ms": data.get("duration_ms"),
            })
            return str(data.get("result") or "")

    return UsageCLIClient(**kw)


# ── 실행 ──────────────────────────────────────────────────────────
def _done_keys(out_path: Path) -> set[tuple[str, str, str]]:
    keys = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("stance"):
                    keys.add((r["symbol"], r["asof"], r["arm"]))
            except (ValueError, KeyError):
                continue
    return keys


def run_cases(cases: list[dict], *, llm, data_dir: Path | str, out_path: Path,
              arms: tuple[str, ...] = ARMS, inputs: Any = None, limit: int | None = None,
              stop_fn: Callable[[], bool] | None = None,
              names: dict[str, str] | None = None, llm_lean=None) -> dict:
    """case × arm 판정 → out_path jsonl 에 한 줄씩(이어하기 지원). 반환: 이번 실행 요약."""
    from ..agents.athena import ATHENA_PROMPT_REV, AthenaAgent, sanitize

    agents = {False: AthenaAgent(llm), True: AthenaAgent(llm_lean) if llm_lean else None}
    done = _done_keys(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_calls, n_fail, n_skip = 0, 0, 0
    for case in cases:
        for arm in arms:
            key = (case["symbol"], case["asof"], arm)
            if key in done:
                continue
            if limit is not None and n_calls + n_fail >= limit:
                return {"calls": n_calls, "failed": n_fail, "skipped": n_skip, "stopped": "limit"}
            if stop_fn and stop_fn():
                return {"calls": n_calls, "failed": n_fail, "skipped": n_skip, "stopped": "window"}
            ctx_arm, lean = split_arm(arm)
            agent = agents[lean]
            if agent is None:
                raise ValueError(f"{arm}: 경량 LLM(llm_lean) 이 필요하다")
            cur_llm = llm_lean if lean else llm
            ctx = build_case_context(case, ctx_arm, data_dir=data_dir, inputs=inputs,
                                     name=(names or {}).get(case["symbol"]))
            if ctx is None:
                n_skip += 1
                continue
            before = len(getattr(cur_llm, "calls", []))
            t0 = time.time()
            row = {**case, "arm": arm, "prompt_rev": ATHENA_PROMPT_REV,
                   "ctx_bytes": len(json.dumps(ctx, ensure_ascii=False)),
                   "inputs": {k: bool(ctx.get(k)) for k in ("fundamentals", "flows", "positioning")},
                   "ts": datetime.now(timezone.utc).isoformat()}
            try:
                out = agent.research(ctx)
                px = (ctx.get("technical") or {}).get("price")
                out, notes = sanitize(out, price=px)
                row.update({"stance": out.stance, "conviction": out.conviction,
                            "entry_low": out.entry_low, "entry_high": out.entry_high,
                            "invalidation": out.invalidation, "target": out.target,
                            "sanitize_notes": notes, "thesis": (out.thesis or "")[:300]})
                n_calls += 1
            except Exception as e:
                row["error"] = str(e)[:300]
                n_fail += 1
            row["elapsed_s"] = round(time.time() - t0, 1)
            row["usage"] = getattr(cur_llm, "calls", [])[before:]
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"calls": n_calls, "failed": n_fail, "skipped": n_skip, "stopped": None}


# ── 채점 ──────────────────────────────────────────────────────────
def load_results(out_path: Path) -> list[dict]:
    rows = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return [r for r in rows if r.get("stance")]


def score_results(rows: list[dict], *, data_dir: Path | str, cfg: dict | None = None) -> dict:
    """안별 stance 의 20일 지수 대비 초과수익 + 안 사이·라이브와의 일치표 + 토큰 합계."""
    from .stance_excess import stance_episodes, summarize_episodes

    by_arm: dict[str, Any] = {}
    for arm in sorted({r["arm"] for r in rows}):
        obs = [{"symbol": r["symbol"], "ts": r["asof"], "label": r["stance"]}
               for r in rows if r["arm"] == arm]
        eps, _ = stance_episodes(obs, data_dir=data_dir, cfg=cfg)
        by_arm[arm] = summarize_episodes(eps)
    # 같은 case 의 안끼리·라이브와 stance 비교
    cases: dict[tuple[str, str], dict] = {}
    for r in rows:
        c = cases.setdefault((r["symbol"], r["asof"]), {
            "live": r.get("live_stance"), "excess": r.get("live_excess_pp"), "arms": {}})
        c["arms"][r["arm"]] = r["stance"]
    # A→B 이동은 같은 CLI 변형끼리만 짝짓는다(base↔inputs, base_lean↔inputs_lean)
    flips: dict[str, dict[str, int]] = {}
    flip_excess: dict[str, dict[str, list[float]]] = {}
    agree_live: dict[str, list[int]] = {}
    for c in cases.values():
        for sfx in ("", LEAN_SUFFIX):
            b, i = c["arms"].get("base" + sfx), c["arms"].get("inputs" + sfx)
            if not (b and i):
                continue
            name, k = "base_to_inputs" + sfx, f"{b}->{i}"
            flips.setdefault(name, {})[k] = flips.get(name, {}).get(k, 0) + 1
            if c["excess"] is not None:
                flip_excess.setdefault(name, {}).setdefault(k, []).append(float(c["excess"]))
        for arm, st in c["arms"].items():
            if c.get("live"):
                a = agree_live.setdefault(arm, [0, 0])
                a[0] += int(st == c["live"])
                a[1] += 1
    flip_perf = {name: {k: {"n": len(v), "excess_avg_pp": round(sum(v) / len(v), 2)}
                        for k, v in sorted(d.items())}
                 for name, d in flip_excess.items()}
    def _tok(rs: list[dict]) -> dict:
        t = {"calls": 0, "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
             "cost_usd": 0.0}
        for r in rs:
            for u in r.get("usage") or []:
                t["calls"] += 1
                t["input"] += int(u.get("input_tokens") or 0)
                t["cache_create"] += int(u.get("cache_creation_input_tokens") or 0)
                t["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
                t["output"] += int(u.get("output_tokens") or 0)
                t["cost_usd"] += float(u.get("cost_usd") or 0)
        t["cost_usd"] = round(t["cost_usd"], 4)
        return t

    tok = _tok(rows)
    tok_by_arm = {arm: _tok([r for r in rows if r["arm"] == arm])
                  for arm in sorted({r["arm"] for r in rows})}
    # 경량 CLI 검증 — 같은 case·같은 컨텍스트에서 현행 vs 경량 판정 일치
    lean_agree: dict[str, dict] = {}
    by_case_arm = {(r["symbol"], r["asof"], r["arm"]): r for r in rows}
    for (sym, asof, arm), r in by_case_arm.items():
        if arm.endswith(LEAN_SUFFIX):
            continue
        rl = by_case_arm.get((sym, asof, arm + LEAN_SUFFIX))
        if not rl:
            continue
        a = lean_agree.setdefault(arm, {"agree": 0, "n": 0, "conv_diff_abs": 0.0})
        a["n"] += 1
        a["agree"] += int(r["stance"] == rl["stance"])
        a["conv_diff_abs"] += abs(float(r.get("conviction") or 0) - float(rl.get("conviction") or 0))
    for a in lean_agree.values():
        a["conv_diff_abs"] = round(a["conv_diff_abs"] / a["n"], 3) if a["n"] else None
    return {"n_cases": len(cases), "by_arm": by_arm,
            "base_to_inputs": flips.get("base_to_inputs", {}),
            "base_to_inputs_lean": flips.get("base_to_inputs" + LEAN_SUFFIX, {}),
            "flip_excess": flip_perf,
            "agree_with_live": {k: {"agree": v[0], "n": v[1]} for k, v in agree_live.items()},
            "tokens": tok, "tokens_by_arm": tok_by_arm, "lean_agreement": lean_agree}
