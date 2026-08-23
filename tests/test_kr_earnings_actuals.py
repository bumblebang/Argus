"""KR 잠정실적 DART document 파싱 + 컨센서스 서프라이즈."""
from __future__ import annotations

from src.datasources.dart import (parse_earnings_document_xml, surprise_vs_consensus,
                                  _clean_amount, fetch_earnings_actuals)
from src.engine.disclosure import (DisclosureWatcher, EARNINGS_KEYWORDS,
                                   is_earnings_actuals_report)
from src.engine.store import Store


def test_clean_amount():
    assert _clean_amount("1,234") == 1234.0
    assert _clean_amount("(500)") == -500.0
    assert _clean_amount("-1,368") == -1368.0
    assert _clean_amount("△430") == -430.0
    assert _clean_amount("-") is None


def test_parse_simple_text_xml():
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <DOCUMENT><BODY>
    <TABLE><TR><TD>구분</TD><TD>당기</TD></TR>
    <TR><TD>매출액</TD><TD>1,000</TD></TR>
    <TR><TD>영업이익</TD><TD>200</TD></TR>
    <TR><TD>당기순이익</TD><TD>150</TD></TR>
    </TABLE>
    <P>단위: 억원 (연결)</P>
    </BODY></DOCUMENT>""".encode("utf-8")
    out = parse_earnings_document_xml(xml)
    assert out["parse_ok"]
    assert out["revenue"] == 1000.0
    assert out["op_profit"] == 200.0
    assert out["net_income"] == 150.0
    assert out["scope"] == "consolidated"
    assert out["unit"] == "억원"


def test_parse_failure_is_soft():
    out = parse_earnings_document_xml(b"<not><earnings/></not>")
    assert out["parse_ok"] is False


def test_surprise_vs_consensus_unit_match():
    actuals = {"revenue": 1100.0, "op_profit": 220.0, "net_income": 160.0,
               "unit": "억원", "parse_ok": True}
    consensus = {"revenue": 1000.0, "op_profit": 200.0, "net_income": 150.0,
                 "unit": "억원"}
    s = surprise_vs_consensus(actuals, consensus)
    assert s["revenue_surprise_pct"] == 10.0
    assert s["op_profit_surprise_pct"] == 10.0


def test_surprise_skips_unknown_unit():
    actuals = {"revenue": 1100.0, "unit": "unknown", "parse_ok": True}
    assert surprise_vs_consensus(actuals, {"revenue": 1000.0}) == {}


def test_fetch_actuals_empty_without_key():
    out = fetch_earnings_actuals("", "20260101000001")
    assert out["parse_ok"] is False


def test_disclosure_attaches_actuals_and_earnings_result(tmp_path):
    import json
    import time
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    filings = [{
        "rcept_no": "R_NEW", "stock_code": "005930", "corp_name": "삼성",
        "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "rcept_dt": "20260801",
    }]
    w = DisclosureWatcher(
        store, lambda: list(filings), lambda: {"005930"},
        earnings_fn=lambda s: {"consensus": {"revenue": 1000.0, "op_profit": 200.0,
                                             "net_income": 150.0, "unit": "억원"}},
        actuals_fn=lambda r: {"revenue": 1100.0, "op_profit": 220.0, "net_income": 165.0,
                              "unit": "억원", "scope": "consolidated", "parse_ok": True,
                              "rcept_no": r},
    )
    w.poll_once()  # prime
    filings.append({
        "rcept_no": "R_EARN", "stock_code": "005930", "corp_name": "삼성",
        "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "rcept_dt": "20260801",
    })
    res = w.poll_once()
    assert "005930" in res["woke"]
    events = store.recent_events("earnings_result", time.time() - 60, limit=5)
    assert len(events) >= 1
    payload = json.loads(events[0]["payload"])
    assert payload.get("market") == "KR"
    assert payload.get("revenue_surprise_pct") == 10.0


def test_earnings_keywords_include_잠정():
    assert any("잠정" in k for k in EARNINGS_KEYWORDS)
    assert is_earnings_actuals_report("연결재무제표기준영업(잠정)실적(공정공시)")
    assert is_earnings_actuals_report("매출액또는손익구조30%(대규모법인은15%)이상변경")
    assert not is_earnings_actuals_report("결산실적공시예고(안내공시)")
    assert not is_earnings_actuals_report("영업실적등에관한전망(공정공시)")


_LG_HTML = """<html><body>
<p>연결재무제표 기준 영업(잠정)실적(공정공시)</p>
<p>단위 : 백만원, %</p>
<table>
<tr><td>구분</td><td>당기실적</td><td>전기실적</td><td>전년동기실적</td></tr>
<tr><td>매출액</td><td>당해실적</td><td>2,139,622</td><td>1,800,623</td><td>18.8</td></tr>
<tr><td></td><td>누계실적</td><td>3,940,245</td></tr>
<tr><td>영업이익</td><td>당해실적</td><td>532,978</td><td>413,830</td><td>28.8</td></tr>
<tr><td>당기순이익</td><td>당해실적</td><td>533,838</td><td>378,965</td><td>40.9</td></tr>
</table>
</body></html>""".encode("utf-8")


def test_parse_html_당해실적_not_누계():
    out = parse_earnings_document_xml(_LG_HTML)
    assert out["parse_ok"]
    assert out["revenue"] == 2139622.0
    assert out["op_profit"] == 532978.0
    assert out["net_income"] == 533838.0
    assert out["unit"] == "백만원"
    assert out["scope"] == "consolidated"
    assert out["revenue"] != 3940245.0  # 누계실적 금지


def test_parse_opendart_error_014_retryable():
    from src.datasources.dart import _parse_opendart_error
    err = _parse_opendart_error(
        b"<?xml version='1.0'?><result><status>014</status>"
        b"<message>file does not exist</message></result>")
    assert err["status"] == "014"
    assert _parse_opendart_error(b"PK\x03\x04rest") is None


def test_fetch_nonzip_xml_error_is_retryable(monkeypatch):
    class _Resp:
        content = (b"<?xml version='1.0'?><result><status>014</status>"
                   b"<message>not found</message></result>")
        headers = {}
        def raise_for_status(self):
            return None
    monkeypatch.setattr("src.datasources.dart.requests.get",
                        lambda *a, **k: _Resp())
    out = fetch_earnings_actuals("dummy-key", "20260813800552")
    assert out["parse_ok"] is False
    assert out["retryable"] is True
    assert out["dart_status"] == "014"


def test_disclosure_retries_then_logs_earnings_result(tmp_path):
    import json
    clock = {"t": 1_000.0}
    calls = {"n": 0}

    def actuals(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"parse_ok": False, "retryable": True, "rcept_no": r}
        return {"revenue": 2139622.0, "op_profit": 532978.0, "net_income": 533838.0,
                "unit": "백만원", "scope": "consolidated", "parse_ok": True,
                "rcept_no": r}

    store = Store(tmp_path / "t.db")
    filings = [{
        "rcept_no": "R_OLD", "stock_code": "003550", "corp_name": "LG",
        "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "rcept_dt": "20260813",
    }]
    w = DisclosureWatcher(
        store, lambda: list(filings), lambda: {"003550"},
        actuals_fn=actuals, now_fn=lambda: clock["t"],
    )
    w.poll_once()  # prime
    filings.append({
        "rcept_no": "20260813800552", "stock_code": "003550", "corp_name": "LG",
        "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "rcept_dt": "20260813",
    })
    w.poll_once()  # fail → enqueue
    assert store.recent_events("earnings_result", 0) == []
    assert "20260813800552" in w._pending_actuals
    clock["t"] += 601
    res = w.poll_once()  # retry success, cover only → no wake
    events = store.recent_events("earnings_result", 0, limit=5)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["revenue_actual"] == 2139622.0
    assert payload["unit"] == "백만원"
    assert payload.get("recovered") is True
    assert res["woke"] == []


def test_recover_unparsed_earnings_from_store(tmp_path):
    import json
    store = Store(tmp_path / "t.db")
    store.log_event("disclosure", "003550", {
        "rcept_no": "20260813800552",
        "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)",
        "keyword": "영업(잠정)실적", "route": "queue", "rcept_dt": "20260813",
    })
    w = DisclosureWatcher(
        store, lambda: [], lambda: {"003550"},
        actuals_fn=lambda r: {"revenue": 2139622.0, "op_profit": 532978.0,
                              "net_income": 533838.0, "unit": "백만원",
                              "scope": "consolidated", "parse_ok": True,
                              "rcept_no": r},
    )
    w.poll_once()  # prime + recover + immediate fetch
    events = store.recent_events("earnings_result", 0, limit=5)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["revenue_actual"] == 2139622.0
    assert payload.get("recovered") is True


def test_recent_earnings_results_kr_fields(tmp_path):
    from src.agents.pipeline import CycleRunner
    store = Store(tmp_path / "t.db")
    store.log_event("earnings_result", "003550", {
        "market": "KR", "symbol": "003550", "date": "20260813",
        "rcept_no": "20260813800552",
        "revenue_actual": 2139622.0, "op_profit_actual": 532978.0,
        "net_income_actual": 533838.0, "unit": "백만원",
        "scope": "consolidated", "parse_ok": True, "route": "queue",
    })
    runner = CycleRunner.__new__(CycleRunner)
    runner.store = store
    out = runner._recent_earnings_results()
    assert out[0]["revenue_actual"] == 2139622.0
    assert out[0]["op_profit_actual"] == 532978.0
    assert out[0]["unit"] == "백만원"
    assert "eps_actual" in out[0]  # US 키는 유지(값은 None)


def test_parse_negative_억원_당해실적():
    html = """<html><body>
    <p>연결재무제표 기준 영업(잠정)실적</p>
    <p>단위 : 억원, %</p>
    <table>
    <tr><td>매출액</td><td>당해실적</td><td>69,150</td><td>66,806</td></tr>
    <tr><td>영업이익</td><td>당해실적</td><td>-430</td><td>1,200</td></tr>
    <tr><td>당기순이익</td><td>당해실적</td><td>-1,368</td><td>794</td><td>적자전환</td></tr>
    </table>
    </body></html>""".encode("utf-8")
    out = parse_earnings_document_xml(html)
    assert out["parse_ok"]
    assert out["revenue"] == 69150.0
    assert out["op_profit"] == -430.0
    assert out["net_income"] == -1368.0
    assert out["unit"] == "억원"


def test_parse_dash_only_table_is_not_ok():
    """월별 공정공시가 표는 비우고 대수/국가별 각주를 넣는 경우 — 숫자를 지어내지 않는다."""
    html = """<html><body>
    <p>영업(잠정)실적(공정공시)</p>
    <p>단위 : 백만원, %</p>
    <table>
    <tr><td>매출액</td><td>당해실적</td><td>-</td><td>-</td></tr>
    <tr><td>영업이익</td><td>당해실적</td><td>-</td><td>-</td></tr>
    <tr><td>당기순이익</td><td>당해실적</td><td>-</td><td>-</td></tr>
    </table>
    <p>구분(단위:대,%) 당기실적 120,000</p>
    </body></html>""".encode("utf-8")
    out = parse_earnings_document_xml(html)
    assert out["parse_ok"] is False
    assert out["revenue"] is None


def test_parse_손익구조_원_단위():
    html = """<html><body>
    <p>매출액또는손익구조30%이상변경</p>
    <p>단위 : 원</p>
    <table>
    <tr><td>과목</td><td>당해사업연도</td><td>직전사업연도</td></tr>
    <tr><td>매출액</td><td>7,828,116,654</td><td>1,000,000,000</td></tr>
    <tr><td>영업이익</td><td>-9,333,569,310</td><td>-1,000,000,000</td></tr>
    <tr><td>당기순이익</td><td>-8,819,461,717</td><td>-900,000,000</td></tr>
    </table>
    </body></html>""".encode("utf-8")
    out = parse_earnings_document_xml(html)
    assert out["parse_ok"]
    assert out["revenue"] == 7828116654.0
    assert out["op_profit"] == -9333569310.0
    assert out["net_income"] == -8819461717.0
    assert out["unit"] == "원"


def test_예고_does_not_call_actuals_fn(tmp_path):
    store = Store(tmp_path / "t.db")
    called = []
    filings = [{"rcept_no": "R0", "stock_code": "005930", "corp_name": "삼성",
                "report_nm": "결산실적공시예고", "rcept_dt": "20260801"}]
    w = DisclosureWatcher(
        store, lambda: list(filings), lambda: {"005930"},
        actuals_fn=lambda r: called.append(r) or {"parse_ok": False},
    )
    w.poll_once()
    filings.append({"rcept_no": "R_NOTICE", "stock_code": "005930",
                    "corp_name": "삼성", "report_nm": "결산실적공시예고(안내공시)",
                    "rcept_dt": "20260801"})
    w.poll_once()
    assert called == []
    assert store.recent_events("earnings_result", 0) == []
