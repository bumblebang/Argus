"""Athena 리플레이 A/B(eval.athena_replay) — 시점 컨텍스트·이어하기·토큰 계측·채점."""
import json
from datetime import date, datetime, timedelta

import pytest

from src.agents.llm import MockLLM
from src.agents.schemas import DossierOutput
from src.eval import athena_replay as ar


def _hist(tmp_path, sym="111111", n=120, d0=datetime(2026, 5, 1), step=1.0):
    h = tmp_path / "history"
    h.mkdir(exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"]
    for i in range(n):
        c = 100 + i * step
        lines.append(f"{(d0 + timedelta(days=i)):%Y-%m-%d},{c},{c*1.01},{c*0.99},{c},1000")
    (h / f"{sym}.KS_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_load_history_before_excludes_asof_day(tmp_path):
    _hist(tmp_path)
    df = ar.load_history_before(tmp_path, "111111", date(2026, 7, 20))
    assert df["time"].max().date() == date(2026, 7, 19)
    assert {"open", "high", "low", "close", "volume"} <= set(df.columns)


class _FakeInputs:
    def __init__(self):
        self.calls = []

    def for_symbol(self, sym, market, *, price, asof, live):
        self.calls.append((sym, asof, live))
        return {"fundamentals": {"pb": 1.2}, "flows": {"foreign_5d": -1.0}}


def test_build_case_context_arms(tmp_path):
    _hist(tmp_path)
    case = {"symbol": "111111", "market": "KR", "asof": "2026-07-20"}
    fake = _FakeInputs()
    base = ar.build_case_context(case, "base", data_dir=tmp_path, inputs=fake)
    inp = ar.build_case_context(case, "inputs", data_dir=tmp_path, inputs=fake)
    assert base["technical"] and not base.get("fundamentals")
    for k in ("sentiment", "markets", "macro", "regime", "focus"):
        assert k not in base                       # 과거 재현 불가 슬롯은 두 안 모두 제외
    assert inp["fundamentals"] == {"pb": 1.2}
    assert fake.calls == [("111111", date(2026, 7, 20), False)]   # 라이브 전용 슬롯 금지
    assert ar.build_case_context({**case, "asof": "2026-05-10"}, "base",
                                 data_dir=tmp_path) is None        # 봉 부족


def _llm():
    def respond(schema, system, user):
        ctx = json.loads(user)
        px = float(ctx["technical"]["price"])
        return DossierOutput(stance="bullish" if ctx.get("fundamentals") else "neutral",
                             thesis="t", conviction=0.6, entry_low=px * .98, entry_high=px,
                             invalidation=px * .94, target=px * 1.1)
    llm = MockLLM(respond)
    llm.calls = []
    return llm


def test_run_cases_resume_and_limit(tmp_path):
    _hist(tmp_path)
    cases = [{"symbol": "111111", "market": "KR", "asof": "2026-07-20",
              "live_stance": "bullish", "live_excess_pp": -3.0}]
    out = tmp_path / "r.jsonl"
    r1 = ar.run_cases(cases, llm=_llm(), data_dir=tmp_path, out_path=out,
                      inputs=_FakeInputs(), limit=1)
    assert r1["calls"] == 1 and r1["stopped"] == "limit"
    r2 = ar.run_cases(cases, llm=_llm(), data_dir=tmp_path, out_path=out,
                      inputs=_FakeInputs())
    assert r2["calls"] == 1                      # 끝난 안은 건너뛰고 남은 안만
    rows = ar.load_results(out)
    assert {r["arm"] for r in rows} == {"base", "inputs"}
    assert {r["arm"]: r["stance"] for r in rows} == {"base": "neutral", "inputs": "bullish"}
    assert rows[1]["inputs"]["fundamentals"] is True and rows[0]["prompt_rev"]


def test_run_cases_stop_fn(tmp_path):
    _hist(tmp_path)
    cases = [{"symbol": "111111", "market": "KR", "asof": "2026-07-20"}]
    r = ar.run_cases(cases, llm=_llm(), data_dir=tmp_path, out_path=tmp_path / "r.jsonl",
                     stop_fn=lambda: True)
    assert r["stopped"] == "window" and r["calls"] == 0


def test_usage_cli_client_parses_json_output():
    class _Base:
        def __init__(self, **k):
            self.base_args = ["-p"]

        def _invoke(self, prompt, model):
            return json.dumps({"result": "본문", "total_cost_usd": 0.1, "duration_ms": 900,
                               "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                         "cache_read_input_tokens": 200, "output_tokens": 30},
                               "modelUsage": {"claude-sonnet-5": {}}})
    c = ar.usage_cli_client(_Base)
    assert c.base_args == ["-p", "--output-format", "json"]
    assert c._invoke("x", "sonnet") == "본문"
    assert c.calls[0]["model"] == "claude-sonnet-5" and c.calls[0]["output_tokens"] == 30


def test_score_results_flips_agreement_tokens(tmp_path):
    h = tmp_path / "history"
    h.mkdir()
    d0 = datetime(2026, 6, 1)
    for sym, f in (("SPY", lambda i: 100 + i * .1), ("UP", lambda i: 100 + i),
                   ("DN", lambda i: 100 - i * .5)):
        lines = ["Date,Open,High,Low,Close,Volume"] + [
            f"{(d0 + timedelta(days=i)):%Y-%m-%d},{f(i)},{f(i)},{f(i)},{f(i)},1" for i in range(60)]
        (h / f"{sym}_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    u = [{"input_tokens": 1, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 20,
          "output_tokens": 5, "cost_usd": 0.1}]
    rows = [
        {"symbol": "UP", "asof": "2026-06-05", "arm": "base", "stance": "neutral",
         "live_stance": "neutral", "usage": u},
        {"symbol": "UP", "asof": "2026-06-05", "arm": "inputs", "stance": "bullish",
         "live_stance": "neutral", "usage": u},
        {"symbol": "DN", "asof": "2026-06-05", "arm": "base", "stance": "neutral",
         "live_stance": "bullish", "usage": u},
        {"symbol": "DN", "asof": "2026-06-05", "arm": "inputs", "stance": "neutral",
         "live_stance": "bullish", "usage": u},
    ]
    rep = ar.score_results(rows, data_dir=tmp_path)
    assert rep["base_to_inputs"] == {"neutral->bullish": 1, "neutral->neutral": 1}
    assert rep["agree_with_live"]["base"] == {"agree": 1, "n": 2}
    assert rep["by_arm"]["inputs"]["US"]["bullish"]["excess_avg_pp"] > 0
    assert rep["tokens"]["calls"] == 4 and rep["tokens"]["cost_usd"] == pytest.approx(0.4)


def test_score_results_lean_arms_flips_and_flip_excess(tmp_path):
    rows = []
    for sym, live, ex, b, i in (("A", "neutral", 4.0, "neutral", "bullish"),
                                ("B", "bullish", -3.0, "bullish", "neutral"),
                                ("C", "bullish", 2.0, "bullish", "neutral")):
        for arm, st in (("base_lean", b), ("inputs_lean", i)):
            rows.append({"symbol": sym, "asof": "2026-06-05", "arm": arm, "stance": st,
                         "live_stance": live, "live_excess_pp": ex})
    rep = ar.score_results(rows, data_dir=tmp_path)
    assert rep["base_to_inputs"] == {}
    assert rep["base_to_inputs_lean"] == {"neutral->bullish": 1, "bullish->neutral": 2}
    assert rep["agree_with_live"]["base_lean"] == {"agree": 3, "n": 3}
    assert rep["agree_with_live"]["inputs_lean"] == {"agree": 0, "n": 3}
    assert rep["flip_excess"]["base_to_inputs_lean"] == {
        "bullish->neutral": {"n": 2, "excess_avg_pp": -0.5},
        "neutral->bullish": {"n": 1, "excess_avg_pp": 4.0}}


def test_sample_cases_stratifies_bullish(tmp_path):
    from src.engine.store import Store
    h = tmp_path / "history"
    h.mkdir()
    d0 = datetime(2026, 6, 1)
    syms = [f"S{i}" for i in range(8)] + ["SPY"]
    for sym in syms:
        lines = ["Date,Open,High,Low,Close,Volume"] + [
            f"{(d0 + timedelta(days=i)):%Y-%m-%d},100,100,100,{100 + i},1" for i in range(60)]
        (h / f"{sym}_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = Store(tmp_path / "t.db")
    for i in range(8):
        store.save_dossier(f"S{i}", "US", thesis="t", evidence={
            "stance": "bullish" if i < 2 else "neutral"}, ttl_hours=48)
    from zoneinfo import ZoneInfo
    created = datetime(2026, 6, 5, 12, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    store.conn.execute("UPDATE dossiers SET created_at=?", (created,))
    store.conn.commit()
    cases = ar.sample_cases(store, data_dir=tmp_path, market="US", since=created - 1,
                            until=created + 1, n=4)
    assert len(cases) == 4
    assert sum(c["live_stance"] == "bullish" for c in cases) == 2   # 절반까지 bullish
    # asof 는 시장 현지 날짜 — KST 6/5 12시 = 뉴욕 6/4
    assert all(c["asof"] == "2026-06-04" and "live_excess_pp" in c for c in cases)


def test_lean_arm_uses_lean_llm_and_scores_agreement(tmp_path):
    _hist(tmp_path)
    cases = [{"symbol": "111111", "market": "KR", "asof": "2026-07-20",
              "live_stance": "neutral", "live_excess_pp": 1.0}]
    out = tmp_path / "r.jsonl"
    main, lean = _llm(), _llm()
    lean.calls.append({"marker": "pre"})               # 경량 쪽 계측 분리 확인용
    r = ar.run_cases(cases, llm=main, data_dir=tmp_path, out_path=out,
                     arms=("inputs", "inputs_lean"), inputs=_FakeInputs(), llm_lean=lean)
    assert r["calls"] == 2
    rows = ar.load_results(out)
    assert {x["arm"] for x in rows} == {"inputs", "inputs_lean"}
    assert all(x["inputs"]["fundamentals"] for x in rows)   # 같은 컨텍스트(inputs)
    rep = ar.score_results(rows, data_dir=tmp_path)
    assert rep["lean_agreement"]["inputs"] == {"agree": 1, "n": 1, "conv_diff_abs": 0.0}
    with pytest.raises(ValueError):
        ar.run_cases(cases, llm=main, data_dir=tmp_path, out_path=tmp_path / "x.jsonl",
                     arms=("base_lean",))
