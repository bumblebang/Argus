"""EDGAR 워처 — 중대성 필터·프라이밍·3단 라우팅·Athena US 공시 큐."""
import json

import src.engine.edgar_watch as emod
from src.datasources.edgar import is_material_filing, parse_items
from src.engine.edgar_watch import EdgarWatcher
from src.engine.store import Store


def _filing(acc, sym="AAPL", form="8-K", items=None, report_nm=None,
            empty_items=None, date="2026-08-27"):
    items = items if items is not None else ["1.01"]
    label, empty = is_material_filing(form, items)
    f = {
        "accession": acc,
        "symbol": sym,
        "form": form,
        "items": parse_items(items) if not isinstance(items, list) else items,
        "filing_date": date,
        "report_nm": report_nm or label or form,
        "corp_name": sym,
        "empty_items": empty if empty_items is None else empty_items,
    }
    return f


def _watcher(store, filings_ref, universe=("AAPL", "NVDA"), on_wake=None):
    return EdgarWatcher(store, lambda: list(filings_ref),
                        lambda: set(universe), on_wake=on_wake)


# ── 중대성 필터 ────────────────────────────────────────────────────
def test_is_material_8k_items():
    label, empty = is_material_filing("8-K", "1.01,9.01")
    assert label and "1.01" in label and empty is False
    assert is_material_filing("8-K", "7.01,9.01") == (None, False)
    label, empty = is_material_filing("8-K", "")
    assert label == "8-K" and empty is True
    assert is_material_filing("6-K", None)[0] == "6-K"
    assert is_material_filing("10-K", "1.01") == (None, False)


def test_parse_items():
    assert parse_items("1.01, 2.01") == ["1.01", "2.01"]
    assert parse_items(None) == []
    assert parse_items(["5.02", "9.01"]) == ["5.02", "9.01"]


# ── 프라이밍 + dedup ───────────────────────────────────────────────
def test_first_poll_primes_without_waking(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 100)
    woke = []
    filings = [_filing("ACC-1"), _filing("ACC-2", sym="NVDA")]
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p))
    res = w.poll_once()
    assert res == {"new": 0, "woke": [], "queued": []} and woke == []
    assert w.poll_once()["new"] == 0


def test_dedup_by_accession(tmp_path):
    store = Store(tmp_path / "t.db")
    filings = [_filing("ACC-1")]
    w = _watcher(store, filings)
    w.poll_once()
    filings.append(_filing("ACC-2", sym="ZZZZ"))  # 비유니버스
    assert w.poll_once()["new"] == 1
    assert w.poll_once()["new"] == 0


# ── 3단 라우팅 ─────────────────────────────────────────────────────
def test_routing_held_wakes_brain(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("AAPL", "US", 10, 100)
    woke = []
    filings = []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append((why, p)))
    w.poll_once()
    filings.append(_filing("ACC-1", sym="AAPL", items=["2.01"]))
    res = w.poll_once()
    assert res["woke"] == ["AAPL"]
    assert woke and woke[0][0] == "disclosure"
    assert woke[0][1][0]["symbol"] == "AAPL"
    ev = store.recent_events("disclosure", 0)[0]
    p = json.loads(ev["payload"])
    assert p["route"] == "wake" and p["market"] == "US" and p["form"] == "8-K"


def test_routing_universe_queues_no_wake(tmp_path):
    store = Store(tmp_path / "t.db")
    woke = []
    filings = []
    w = _watcher(store, filings, universe=("NVDA",),
                 on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    filings.append(_filing("ACC-1", sym="NVDA", items=["5.02"]))
    res = w.poll_once()
    assert res["queued"] == ["NVDA"] and res["woke"] == [] and woke == []
    ev = store.recent_events("disclosure", 0)[0]
    assert json.loads(ev["payload"])["route"] == "queue"


def test_empty_items_held_only_skips_universe(tmp_path):
    store = Store(tmp_path / "t.db")
    filings = []
    w = _watcher(store, filings, universe=("NVDA",))
    w.poll_once()
    filings.append(_filing("ACC-1", sym="NVDA", items=[], empty_items=True,
                           report_nm="8-K"))
    res = w.poll_once()
    assert res["queued"] == [] and res["woke"] == []
    assert store.recent_events("disclosure", 0) == []

    store.open_position("NVDA", "US", 1, 100)
    filings.append(_filing("ACC-2", sym="NVDA", items=[], empty_items=True,
                           report_nm="8-K"))
    res2 = w.poll_once()
    assert res2["woke"] == ["NVDA"]


def test_routing_ignores_noise_and_outsiders(tmp_path):
    store = Store(tmp_path / "t.db")
    filings = []
    w = _watcher(store, filings, universe=("AAPL",))
    w.poll_once()
    # 7.01 only — is_material None; 강제 report_nm 없이 form만
    noise = _filing("ACC-1", sym="AAPL", items=["7.01"])
    noise["report_nm"] = None  # fetch would drop; mock with form only
    # form+items 로 재검증되어 스킵되도록 report_nm 제거 후 label None
    filings.append({
        "accession": "ACC-1", "symbol": "AAPL", "form": "8-K",
        "items": ["7.01"], "filing_date": "2026-08-27",
        "report_nm": None, "corp_name": "Apple", "empty_items": False,
    })
    filings.append(_filing("ACC-2", sym="ZZZZ", items=["1.01"]))
    res = w.poll_once()
    assert res["new"] == 2 and res["woke"] == [] and res["queued"] == []
    assert store.recent_events("disclosure", 0) == []


def test_armed_us_also_wakes(tmp_path):
    store = Store(tmp_path / "t.db")
    store.arm_candidate("NVDA", "US", strategy="rsi_reversion")
    woke = []
    filings = []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    filings.append(_filing("ACC-1", sym="NVDA", items=["1.03"]))
    assert w.poll_once()["woke"] == ["NVDA"] and woke


# ── 주기 ───────────────────────────────────────────────────────────
def test_interval_adaptive(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    w = _watcher(store, [])
    monkeypatch.setattr(emod, "is_open", lambda m, now=None: True)
    assert w.interval() == 120.0
    monkeypatch.setattr(emod, "is_open", lambda m, now=None: False)
    monkeypatch.setattr(emod, "within_after_close", lambda m, h, now=None: True)
    assert w.interval() == 120.0
    monkeypatch.setattr(emod, "within_after_close", lambda m, h, now=None: False)
    assert w.interval() == 900.0


# ── Athena US 공시 큐 ──────────────────────────────────────────────
def test_select_symbols_prioritizes_us_disclosure_queue(tmp_path):
    from src.agents.athena import select_symbols
    from src.config import load_config
    cfg = load_config()
    cfg.universe["US"] = [
        {"symbol": "AAA", "name": "a"},
        {"symbol": "NVDA", "name": "n"},
        {"symbol": "CCC", "name": "c"},
    ]
    store = Store(tmp_path / "t.db")
    for s in ("AAA", "NVDA", "CCC"):
        store.save_dossier(s, "US", thesis="old")
    store.log_event("disclosure", "NVDA", {
        "market": "US", "route": "queue", "form": "8-K",
        "report_nm": "8-K Item 2.01"})
    order = [t["symbol"] for t in select_symbols(cfg, store, "US")]
    assert order[0] == "NVDA"
    # KR 창에 US 공시 큐가 섞이지 않는다
    cfg.universe["KR"] = [{"symbol": "005930", "name": "삼성"}]
    store.save_dossier("005930", "KR", thesis="old")
    kr_order = [t["symbol"] for t in select_symbols(cfg, store, "KR")]
    assert "NVDA" not in kr_order
