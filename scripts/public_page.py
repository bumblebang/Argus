"""Argus 공개 공유용 정적 페이지 생성기 — data/ 를 읽어 자기완결형 HTML 한 장을 만든다.

■ 위협 모델(이 파일을 고치기 전에 반드시 읽을 것)
    이 페이지는 **친구에게 링크로 공유**하기 위한 것이고, 받은 사람이 2차 유포할 수 있다고
    전제한다. 따라서 여기 남아도 되는 정보는 "이 봇이 무엇을 어떻게 보고 판단하는가" 뿐이고,
    **"이 사람이 돈을 얼마나 굴리는가"는 절대 남으면 안 된다.**

■ 경계선
    - 종목 가격(평단·현재가·손절·목표)은 *시장 가격*이라 공개해도 된다.
    - **금액과 수량은 제외한다.** 둘 중 하나만으로는 자산 규모가 안 나오지만 결합하면
      나오기 때문이다(가격 × 수량 = 평가금액). 그래서 수량은 SELECT 문에도 넣지 않는다.

■ 화이트리스트(담는 것)
    시장 심리(공포탐욕) · 시장 국면 · 보유(가격/수익률%) · 진입대기 · 도시에 ·
    성과(비율만) · 최근 브레인 판단(시각·종목·액션·확신도·판정)

■ 블랙리스트(접근 자체를 안 한다)
    총자산·현금·투입원금·평가손익(원)·일손익·평가금액 / 수량 / 계좌번호·주문ID·체결내역 /
    데몬 PID·파일경로·heartbeat·경보 / decisions·positions 의 payload·meta 원문 / 절대경로

■ 설계 원칙
    `scripts.dashboard._gather()` 를 **절대 호출하지 않는다.** 그 함수는 계좌 스냅샷·PID·
    경보를 전부 담기 때문에, 대시보드에 필드가 하나 추가될 때마다 공개판이 조용히 새게 된다.
    공개판은 아래 `gather_public()` 가 **필요한 필드만 직접** 읽는다(전부 읽기전용).
    렌더가 아니라 **수집 단계에서 거른다** — 렌더 필터는 한 번 빠뜨리면 그대로 새어 나간다.
    마지막 방어선으로 `assert_no_leak()` 가 완성된 HTML 을 스캔하고, 한 건이라도 걸리면
    파일을 쓰지 않고 종료한다(한 번 새면 회수가 안 된다).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import public_brief  # noqa: E402
# 유출 가드는 src/leakguard.py 에 있다 — 공개 페이지와 공개용 브리핑 생성기가 같은 가드를
# 써야 하는데 서로 import 하면 순환이 생기기 때문이다. 여기서 재수출해 기존 import 경로
# (`from scripts.public_page import assert_no_leak`)를 그대로 유지한다.
from src.leakguard import (  # noqa: E402,F401
    FOOTER_NOTE, LEAK_PATTERNS, assert_no_leak,
)
from src import paths as _paths  # noqa: E402
from src.logging_setup import setup_logging, get_logger  # noqa: E402
# 데이터를 들고 있지 않은 순수 렌더 헬퍼만 재사용한다(_gather 계열은 절대 쓰지 않는다).
from scripts.dashboard import (  # noqa: E402
    BENCH, CSS, _bench_ret_pct, _fear_html, _fmt, _hms, _load_names, _name,
    _stance_badge, sort_dossiers,
)

log = get_logger("public_page")

DB = _paths.resolve("db", configured="data/bot.db")
MARKET_STATE = ROOT / "data" / "market_state.json"
FEAR_HISTORY = ROOT / "data" / "fear_history.json"
VALUE_WATCHLIST = ROOT / "data" / "value_watchlist.json"   # symbol→name 보충용(이름만 읽는다)
DEFAULT_OUT = ROOT / "public" / "index.html"

DECISION_LIMIT = 8          # 최근 판단 표시 건수
_REGIME_MARKETS = ("KR", "US")
BRIEF_TTL_HOURS = 20.0      # 이 시간 넘은 브리핑은 싣지 않는다(config public_page 로 덮임)


def _value_names() -> dict:
    """밸류 트랙 보유 종목의 이름 보충 — universe.yaml 에 없어 코드로만 뜨는 걸 막는다.

    value_watchlist 는 리서치 산출물이라 계좌 정보가 없다. **symbol→name 만** 뽑는다
    (다른 필드는 손대지 않는다 — 화이트리스트 원칙).
    """
    out: dict = {}
    for sym, v in (_read_json(VALUE_WATCHLIST) or {}).items():
        if isinstance(v, dict) and v.get("name"):
            out[str(sym)] = str(v["name"])
    return out


# ── LLM 자유서술을 싣지 않는 이유 ────────────────────────────────────────
# 브레인의 market_view 에는 현금·여력 같은 잔고 서술이 섞여 나온다. 처음엔 이
# 필드만 패턴으로 걸러 통과시키려 했으나 — **자유서술은 패턴으로 안전을 보장할
# 수 없다.** "여력은 얼마 정도" 처럼 어떤 라벨도 금액기호도 없이 새는 문장을
# 정규식으로 잡을 방법이 없고, 이 페이지가 약속하는 것은 정확히 "자산 규모는
# 절대 안 나간다" 이다. 걸렀을 때만 막는 필터는 그 약속을 못 지킨다.
# → **열거 가능한 구조화 필드만 싣는다.** 종목·액션·확신도·판정은 값의 집합이 유한하고
#   금액이 끼어들 자리가 없다.
# → 시황 코멘트('오늘의 국면')는 `src/public_brief.py` 가 **계좌 정보가 없는 컨텍스트**로
#   따로 생성한 것만 싣는다(입력에 없으면 출력에 못 나온다). 이 파일은 그 캐시를 읽기만
#   하고 **LLM 을 부르지 않는다** — 페이지는 하루에도 여러 번 만들 수 있어야 한다.


# ───────────────────────── 수집(화이트리스트만) ─────────────────────────

def _ro_conn() -> sqlite3.Connection | None:
    """bot.db 읽기전용 연결(mode=ro). 라이브 데몬의 쓰기를 절대 방해하지 않는다."""
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _num(v):
    """수치 캐스팅 — 못 하면 None(0 으로 뭉개면 수익률이 거짓말한다)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def public_cfg() -> dict:
    """config.yaml 의 public_page 블록 + 기본값. 설정을 못 읽어도 페이지는 만들어야 한다."""
    raw: dict = {}
    try:
        from src.config import load_config
        raw = (load_config().raw.get("public_page") or {})
    except Exception as e:
        log.debug("public_page 설정 로드 실패(기본값 사용): %s", e)
    return {
        "enabled": bool(raw.get("enabled", True)),
        "out": str(raw.get("out") or DEFAULT_OUT),
        "brief_model": str(raw.get("brief_model") or "sonnet"),
        "brief_timeout": int(raw.get("brief_timeout", 180)),
        "brief_ttl_hours": float(raw.get("brief_ttl_hours", BRIEF_TTL_HOURS)),
    }


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _prices(cur, symbols: list[str]) -> dict:
    """종목의 최신 스냅샷 **가격**(감시 루프가 매 틱 기록). 금액이 아니라 시장가다."""
    out: dict[str, float] = {}
    for s in symbols:
        r = cur.execute("select price from snapshots where symbol=? and price is not null"
                        " order by ts desc limit 1", (s,)).fetchone()
        if r and r["price"] is not None:
            out[s] = r["price"]
    return out


def _ret_pct(entry, exit_) -> float | None:
    """가격만으로 내는 수익률(%) = (나중가 - 평단) / 평단 × 100.

    store 의 pnl 은 (exit-avg)×qty 라서 pnl/(avg×qty) 와 값이 같다 — 즉 수량·손익을
    건드리지 않고도 같은 숫자를 얻는다. 공개판은 이 경로만 쓴다.
    """
    a, b = _num(entry), _num(exit_)
    if a is None or b is None or a <= 0:
        return None
    return (b - a) / a * 100


def _bench_ret_cached(market: str, since: datetime) -> float | None:
    """지수 B&H 수익률(%) — **디스크 캐시만** 읽는다(네트워크 호출·캐시 쓰기 없음).

    정적 공유물 생성기가 장중에 외부 API 를 때리면 라이브 봇의 레이트리밋을 갉아먹는다.
    캐시가 없으면 조용히 None(알파 칸이 '–' 로 남는다).
    """
    try:
        import pandas as pd
        from src.datasources.history import CACHE, to_yahoo
        sym = BENCH[market][0]
        f = CACHE / f"{to_yahoo(sym, market)}_1d_1y.csv"
        if not f.exists():
            return None
        return _bench_ret_pct(pd.read_csv(f, parse_dates=["time"]), since)
    except Exception:
        return None


def _collect_positions(cur) -> tuple[list[dict], list[dict]]:
    """보유(open)·진입대기(armed) — qty·meta 는 SELECT 문에 넣지 않는다."""
    rows = [dict(r) for r in cur.execute(
        "select symbol,market,state,strategy,avg_price,stop_price,target_price "
        "from positions where state in ('open','armed') order by state,symbol")]
    px = _prices(cur, [str(r["symbol"]) for r in rows])
    held, armed = [], []
    for r in rows:
        item = {
            "symbol": str(r["symbol"] or ""), "market": str(r["market"] or "KR"),
            "strategy": r["strategy"], "avg_price": _num(r["avg_price"]),
            "stop_price": _num(r["stop_price"]), "target_price": _num(r["target_price"]),
        }
        if r["state"] == "open":
            item["last_price"] = _num(px.get(item["symbol"]))
            item["ret_pct"] = _ret_pct(item["avg_price"], item["last_price"])
            held.append(item)
        else:
            armed.append(item)
    return held, armed


def _collect_dossiers(cur, now: float) -> list[dict]:
    """신선한(TTL 안) 도시에 — 심볼별 최신 1건. stance 만 evidence 에서 뽑고 나머지는 버린다.

    표시 정렬은 강세→중립→약세, 같은 stance 안에서는 최신·고확신 순(sort_dossiers).
    """
    out = []
    for r in cur.execute(
            "select symbol,created_at,entry_low,entry_high,invalidation,target,rr,"
            "conviction,evidence from dossiers d where expires_at > :now and created_at = "
            "(select max(created_at) from dossiers where symbol = d.symbol)",
            {"now": now}):
        try:
            stance = (json.loads(r["evidence"]) or {}).get("stance")
        except (ValueError, TypeError):
            stance = None
        created = _num(r["created_at"])
        out.append({
            "symbol": str(r["symbol"] or ""), "stance": stance,
            "created_at": created,
            "entry_low": _num(r["entry_low"]), "entry_high": _num(r["entry_high"]),
            "invalidation": _num(r["invalidation"]), "target": _num(r["target"]),
            "rr": _num(r["rr"]), "conviction": _num(r["conviction"]),
            "age_h": ((now - created) / 3600) if created else None,
        })
    return sort_dossiers(out)


def _collect_perf(cur) -> tuple[list[dict], list[dict], list[dict]]:
    """청산 거래 → (거래별 수익률%, 전략별 집계, 시장별 알파). 금액은 한 칸도 안 읽는다."""
    closed = []
    for r in cur.execute(
            "select symbol,market,strategy,avg_price,exit_price,opened_at,closed_at "
            "from positions where state='closed' and exit_price is not null "
            "order by closed_at desc"):
        rp = _ret_pct(r["avg_price"], r["exit_price"])
        if rp is None:
            continue
        closed.append({"symbol": str(r["symbol"] or ""), "market": str(r["market"] or "?"),
                       "strategy": r["strategy"], "ret_pct": rp,
                       "opened_at": _num(r["opened_at"]), "closed_at": _num(r["closed_at"])})
    agg: dict[str, dict] = {}
    for c in closed:
        a = agg.setdefault(str(c["strategy"] or "–"),
                           {"strategy": str(c["strategy"] or "–"), "trades": 0,
                            "wins": 0, "_rets": []})
        a["trades"] += 1
        a["wins"] += 1 if c["ret_pct"] > 0 else 0
        a["_rets"].append(c["ret_pct"])
    perf = []
    for a in sorted(agg.values(), key=lambda x: x["trades"], reverse=True):
        rets = a.pop("_rets")
        a["win_rate"] = a["wins"] / a["trades"] if a["trades"] else None
        a["avg_ret_pct"] = sum(rets) / len(rets) if rets else None
        perf.append(a)
    # 알파: 시장별 [청산 거래 평균 수익률 − 같은 기간 지수 B&H]. 계좌 평가액을 쓰는
    # 대시보드 알파와는 정의가 다르다(공개판은 자본 규모를 못 읽으므로 거래 수익률 기준).
    by_market: dict[str, list[dict]] = {}
    for c in closed:
        by_market.setdefault(c["market"], []).append(c)
    alpha = []
    for m, cs in sorted(by_market.items()):
        rets = [c["ret_pct"] for c in cs]
        starts = [c["opened_at"] for c in cs if c["opened_at"]]
        if not rets or not starts:
            continue
        since = datetime.fromtimestamp(min(starts))
        bot = sum(rets) / len(rets)
        bench = _bench_ret_cached(m, since) if m in BENCH else None
        alpha.append({"market": m, "since": since.strftime("%m-%d"), "bot_ret_pct": bot,
                      "bench": BENCH.get(m, ("?", "?"))[1], "bench_ret_pct": bench,
                      "alpha": (bot - bench) if bench is not None else None})
    return closed, perf, alpha


def gather_public(now: float | None = None) -> dict:
    """공개 페이지용 화이트리스트 수집. 반환 dict 에 블랙리스트 항목은 애초에 없다.

    실패는 전부 조용히 빈 값으로 떨어진다 — 공유물 생성기가 운영 데이터 상태 때문에
    예외로 죽을 이유가 없다.
    """
    now = time.time() if now is None else now
    ms = _read_json(MARKET_STATE)
    sent = ms.get("sentiment")
    regime = ms.get("regime")
    d: dict = {
        "now": now, "db": False,
        "sentiment": sent if isinstance(sent, dict) else {},
        "fear_history": _read_json(FEAR_HISTORY),
        "regime": regime if isinstance(regime, dict) else {},
        "holdings": [], "armed": [], "dossiers": [], "decisions": [],
        "strategy_perf": [], "closed_trades": [], "alpha": [],
        "names": _load_names(),
        # 공개용 국면 코멘트 — **캐시만 읽는다**(여기서 LLM 을 부르면 페이지 생성이
        # 세션 예산을 먹는다). 갱신은 scripts/public_brief.py 가 하루 1회 담당.
        # (경로를 명시 전달 — 모듈 상수를 호출 시점에 읽어야 테스트가 tmp 로 갈아끼울 수 있다)
        "brief": public_brief.load_brief(path=public_brief.BRIEF_PATH,
                                         ttl_hours=public_cfg()["brief_ttl_hours"]),
    }
    con = _ro_conn()
    if con is None:
        return d
    d["db"] = True
    try:
        cur = con.cursor()
        d["holdings"], d["armed"] = _collect_positions(cur)
        d["dossiers"] = _collect_dossiers(cur, now)
        # 진입존은 원장에 없다 — 도시에에서 심볼로 물어온다(진입대기 표시용).
        zone = {x["symbol"]: (x["entry_low"], x["entry_high"]) for x in d["dossiers"]}
        for a in d["armed"]:
            lo, hi = zone.get(a["symbol"], (None, None))
            a["entry_low"], a["entry_high"] = lo, hi
        # market_view(브레인 시황 코멘트)는 **의도적으로 수집하지 않는다.** 아래 참조.
        d["decisions"] = [
            {"ts": _num(r["ts"]), "symbol": str(r["symbol"] or ""), "action": r["action"],
             "conviction": _num(r["conviction"]), "verdict": r["verdict"]}
            for r in cur.execute("select ts,symbol,action,conviction,verdict from decisions "
                                 "order by ts desc limit ?", (DECISION_LIMIT,))]
        d["closed_trades"], d["strategy_perf"], d["alpha"] = _collect_perf(cur)
    finally:
        con.close()
    return d


# ───────────────────────── 렌더 ─────────────────────────

_EXTRA_CSS = """
.pub-hd{margin-bottom:6px;}
.pub-lead{color:#8b94a3;font-size:13px;margin-top:8px;line-height:1.7;}
.pub-foot{margin-top:34px;padding-top:16px;border-top:1px solid #1c2433;color:#5a6373;
  font-size:11px;line-height:1.8;}
.pub-bhl{font-weight:700;font-size:15px;color:#e6ebf3;margin-bottom:8px;}
.pub-watch{margin:12px 0 0 0;padding-left:18px;color:#a8b1c0;font-size:13px;line-height:1.9;}
"""


def _dp(market) -> int:
    """가격 소수 자리 — KR 은 정수, US 는 2 자리."""
    return 2 if str(market or "").upper() == "US" else 0


def _price(v, market) -> str:
    return _fmt(v, _dp(market)) if v is not None else "–"


def _pct(v, dp: int = 2, sign: bool = True) -> str:
    if v is None:
        return "–"
    return f"{v:+.{dp}f}%" if sign else f"{v:.{dp}f}%"


def _pn(v) -> str:
    """부호별 색 클래스(양수 초록 / 음수 빨강)."""
    return "pos" if (v or 0) >= 0 else "neg"


def _sym(x: dict, names: dict) -> str:
    return _name(x.get("symbol"), names)


def _regime_html(d: dict) -> str:
    """시장 국면 — 시장별 label / 20일선 상회 브레드스 / 표본수 / 산출경로."""
    reg = d.get("regime") or {}
    if not isinstance(reg, dict) or not reg:
        return ""
    markets = [m for m in _REGIME_MARKETS if isinstance(reg.get(m), dict)]
    markets += [m for m in reg if m not in markets and isinstance(reg.get(m), dict)]
    if not markets:
        return ""
    p = ["<div class=sec>시장 국면</div><div class=grid>"]
    for m in markets:
        r = reg[m]
        lab = str(r.get("label") or "?")
        cls = {"risk_on": "pos", "risk_off": "neg"}.get(lab, "muted")
        br = _num(r.get("breadth_above_ma20"))
        br_s = f"20일선 상회 {br*100:.0f}%" if br is not None else "브레드스 –"
        n = r.get("n")
        n_s = f" · n={escape(str(n))}" if n is not None else ""
        src = r.get("source")
        src_s = f" · {escape(str(src))}" if src else ""
        p.append(f"<div class=card><div class=k>{escape(str(m))}</div>"
                 f"<div class='v {cls}' style='font-size:17px'>{escape(lab)}</div>"
                 f"<div class='muted mono' style='font-size:11px;margin-top:4px'>"
                 f"{br_s}{n_s}{src_s}</div></div>")
    p.append("</div>")
    return "".join(p)


def _brief_html(d: dict) -> str:
    """오늘의 국면 — `src/public_brief.py` 가 계좌 무접촉 컨텍스트로 만든 공개용 코멘트.

    렌더 직전에 가드를 **한 번 더** 돌린다(생성 시점 이후 패턴이 늘어났을 수도 있다).
    위반이면 이 섹션만 빼고 페이지는 계속 만든다 — 코멘트 하나 때문에 공유물 전체가
    없어질 이유는 없다. brief 가 없으면 섹션 자체를 생략한다.
    """
    b = d.get("brief")
    if not isinstance(b, dict):
        return ""
    head = str(b.get("headline") or "").strip()
    body = str(b.get("body") or "").strip()
    watch = [str(w).strip() for w in (b.get("watch") or []) if str(w).strip()]
    if not head and not body:
        return ""
    bad = assert_no_leak("\n".join([head, body, *watch]))
    if bad:
        log.error("[brief] 유출 가드 위반 %d건 — '오늘의 국면' 섹션을 빼고 페이지를 만듭니다: %s",
                  len(bad), " | ".join(bad))
        return ""
    p = ["<div class=sec>오늘의 국면</div><div class=panel>"]
    if head:
        p.append(f"<div class=pub-bhl>{escape(head)}</div>")
    if body:
        p.append(f"<div class=mv>{escape(body)}</div>")
    if watch:
        p.append("<ul class=pub-watch>"
                 + "".join(f"<li>{escape(w)}</li>" for w in watch[:3]) + "</ul>")
    ts = _num(b.get("ts"))
    if ts:
        stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        p.append(f"<div class='muted mono' style='font-size:11px;margin-top:10px'>"
                 f"{escape(stamp)} 생성</div>")
    p.append("</div>")
    return "".join(p)


def _holdings_html(d: dict) -> str:
    """보유 종목 — 가격과 수익률만. 수량·평가금액·손익(원)은 수집 단계에서 이미 없다."""
    names = d.get("names") or {}
    rows = d.get("holdings") or []
    p = ["<div class=sec>보유 종목</div><div class=panel>"]
    if not rows:
        p.append("<span class=muted>보유 없음.</span></div>")
        return "".join(p)
    p.append("<table><tr><th>종목명</th><th>시장</th><th>전략</th><th>평가수익률</th>"
             "<th>평단</th><th>현재가</th><th>손절</th><th>목표</th></tr>")
    for x in rows:
        mk = x.get("market")
        rp = x.get("ret_pct")
        strat = x.get("strategy") or "–"
        p.append(f"<tr><td>{_sym(x, names)}</td><td>{escape(str(mk or '–'))}</td>"
                 f"<td class=muted>{escape(str(strat))}</td>"
                 f"<td class='mono {_pn(rp)}'>{_pct(rp)}</td>"
                 f"<td class=mono>{_price(x.get('avg_price'), mk)}</td>"
                 f"<td class=mono>{_price(x.get('last_price'), mk)}</td>"
                 f"<td class=mono>{_price(x.get('stop_price'), mk)}</td>"
                 f"<td class=mono>{_price(x.get('target_price'), mk)}</td></tr>")
    p.append("</table></div>")
    return "".join(p)


def _zone(x: dict, dp: int) -> str:
    """진입존 'low~high'. 한쪽이라도 비면 –(도시에는 존 없이 나오는 경우가 흔하다)."""
    lo, hi = x.get("entry_low"), x.get("entry_high")
    if lo is None or hi is None:
        return "–"
    return f"{_fmt(lo, dp)}~{_fmt(hi, dp)}"


def _armed_html(d: dict) -> str:
    """진입대기 — 아직 안 산 것들의 계획(진입존·손절·목표)."""
    names = d.get("names") or {}
    rows = d.get("armed") or []
    p = ["<div class=sec>진입대기</div><div class=panel>"]
    if not rows:
        p.append("<span class=muted>대기 없음.</span></div>")
        return "".join(p)
    p.append("<table><tr><th>종목명</th><th>시장</th><th>전략</th><th>진입존</th>"
             "<th>손절</th><th>목표</th></tr>")
    for x in rows:
        mk = x.get("market")
        p.append(f"<tr><td>{_sym(x, names)}</td><td>{escape(str(mk or '–'))}</td>"
                 f"<td class=muted>{escape(str(x.get('strategy') or '–'))}</td>"
                 f"<td class=mono>{_zone(x, _dp(mk))}</td>"
                 f"<td class=mono>{_price(x.get('stop_price'), mk)}</td>"
                 f"<td class=mono>{_price(x.get('target_price'), mk)}</td></tr>")
    p.append("</table></div>")
    return "".join(p)


def _dossier_html(d: dict) -> str:
    """도시에 — Athena 리서치 결론(가격 계획 + 확신도). thesis 원문은 싣지 않는다.

    thesis/evidence 는 LLM 자유서술이라 어떤 문장이 섞일지 보장할 수 없다(브레인
    코멘트와 같은 이유). 구조화된 숫자와 stance 만 공개한다.
    """
    names = d.get("names") or {}
    rows = d.get("dossiers") or []
    p = ["<div class=sec>도시에 (Athena · 강세→중립→약세 · 최신순)</div><div class=panel>"]
    if not rows:
        p.append("<span class=muted>신선한 도시에 없음.</span></div>")
        return "".join(p)
    p.append("<table><tr><th>종목명</th><th>stance</th><th>진입존</th><th>무효화</th>"
             "<th>목표</th><th>rr</th><th>확신도</th><th>나이</th></tr>")
    for x in rows:
        conv = x.get("conviction")
        rr = x.get("rr")
        age = x.get("age_h")
        p.append(f"<tr><td>{_sym(x, names)}</td><td>{_stance_badge(x.get('stance'))}</td>"
                 f"<td class=mono>{_zone(x, 2)}</td>"
                 f"<td class=mono>{_fmt(x.get('invalidation'), 2) if x.get('invalidation') is not None else '–'}</td>"
                 f"<td class=mono>{_fmt(x.get('target'), 2) if x.get('target') is not None else '–'}</td>"
                 f"<td class=mono>{f'{rr:.2f}' if rr is not None else '–'}</td>"
                 f"<td class=mono>{f'{conv:.2f}' if conv is not None else '–'}</td>"
                 f"<td class=mono>{f'{age:.0f}h' if age is not None else '–'}</td></tr>")
    p.append("</table></div>")
    return "".join(p)


def _perf_html(d: dict) -> str:
    """성과 — 전부 비율(%). 실현손익(원)·누적손익은 애초에 수집하지 않는다."""
    names = d.get("names") or {}
    perf = d.get("strategy_perf") or []
    alpha = d.get("alpha") or []
    closed = d.get("closed_trades") or []
    p = ["<div class=sec>성과</div><div class=panel>"]
    if perf:
        p.append("<table><tr><th>전략</th><th>거래수</th><th>승률</th>"
                 "<th>평균수익률</th></tr>")
        for a in perf:
            wr = a.get("win_rate")
            ar = a.get("avg_ret_pct")
            p.append(f"<tr><td>{escape(str(a.get('strategy') or '–'))}</td>"
                     f"<td class=mono>{a.get('trades', 0)}</td>"
                     f"<td class=mono>{_pct(wr*100, 0, sign=False) if wr is not None else '–'}</td>"
                     f"<td class='mono {_pn(ar)}'>{_pct(ar)}</td></tr>")
        p.append("</table>")
    else:
        p.append("<span class=muted>청산 표본 없음 — 아직 종료된 거래가 없습니다.</span>")
    if alpha:
        p.append("<div class=sec style='margin-top:18px'>지수 대비 알파 "
                 "<span class=muted>(청산 거래 평균 수익률 − 같은 기간 지수 B&amp;H)</span></div>")
        p.append("<table><tr><th>시장</th><th>시작</th><th>봇 평균수익률</th>"
                 "<th>벤치마크</th><th>지수 수익률</th><th>알파</th></tr>")
        for a in alpha:
            bot = a.get("bot_ret_pct")
            br = a.get("bench_ret_pct")
            al = a.get("alpha")
            al_s = f"{al:+.2f}%p" if al is not None else "–"
            p.append(f"<tr><td>{escape(str(a.get('market') or '–'))}</td>"
                     f"<td class=mono>{escape(str(a.get('since') or '–'))}~</td>"
                     f"<td class='mono {_pn(bot)}'>{_pct(bot)}</td>"
                     f"<td>{escape(str(a.get('bench') or '–'))}</td>"
                     f"<td class=mono>{_pct(br)}</td>"
                     f"<td class='mono {_pn(al)}'><b>{al_s}</b></td></tr>")
        p.append("</table>")
    if closed:
        p.append("<div class=sec style='margin-top:18px'>종료 거래</div>")
        p.append("<table><tr><th>청산</th><th>시장</th><th>종목명</th><th>전략</th>"
                 "<th>수익률</th></tr>")
        for c in closed:
            ca = c.get("closed_at")
            ts = datetime.fromtimestamp(ca).strftime("%m-%d") if ca else "–"
            rp = c.get("ret_pct")
            p.append(f"<tr><td class=mono>{ts}</td>"
                     f"<td>{escape(str(c.get('market') or '–'))}</td>"
                     f"<td>{_sym(c, names)}</td>"
                     f"<td class=muted>{escape(str(c.get('strategy') or '–'))}</td>"
                     f"<td class='mono {_pn(rp)}'>{_pct(rp)}</td></tr>")
        p.append("</table>")
    p.append("</div>")
    return "".join(p)


def _brain_html(d: dict) -> str:
    """최근 브레인 판단 — 시황 코멘트 + 판단 로그(시각·종목·액션·확신도·판정)."""
    names = d.get("names") or {}
    rows = d.get("decisions") or []
    p = ["<div class=sec>최근 판단</div><div class=panel>"]
    if not rows:
        p.append("<span class=muted>아직 완주한 사이클 없음.</span>")
    if rows:
        p.append("<table style='margin-top:12px'><tr><th>시각</th><th>종목명</th>"
                 "<th>액션</th><th>확신도</th><th>판정</th></tr>")
        for r in rows:
            vd = str(r.get("verdict") or "")
            vcls = {"approved": "vd-approved", "vetoed": "vd-vetoed"}.get(vd, "vd-other")
            conv = r.get("conviction")
            ts = r.get("ts")
            p.append(f"<tr><td class=mono>{_hms(ts) if ts else '–'}</td>"
                     f"<td>{_sym(r, names)}</td>"
                     f"<td class=mono>{escape(str(r.get('action') or '–'))}</td>"
                     f"<td class=mono>{f'{conv:.2f}' if conv is not None else '–'}</td>"
                     f"<td><span class={vcls}>{escape(vd or '–')}</span></td></tr>")
        p.append("</table>")
    p.append("</div>")
    return "".join(p)


def render_public(d: dict) -> str:
    """화이트리스트 dict → 자기완결형 HTML 문서 한 장(외부 리소스 참조 0, 자동갱신 없음).

    d 가 비어 있어도({}) 예외 없이 껍데기 페이지를 만든다 — 공유물 생성이 데이터 상태
    때문에 실패하면 안 된다.
    """
    d = d if isinstance(d, dict) else {}
    now = _num(d.get("now")) or time.time()
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    p = ["<!doctype html><html lang=ko><head><meta charset=utf-8>",
         "<meta name=viewport content='width=device-width,initial-scale=1'>",
         # 친구 공유용이지 검색엔진에 올릴 물건이 아니다.
         '<meta name="robots" content="noindex, nofollow">',
         "<title>Argus · 자율 투자 에이전트</title><style>", CSS, _EXTRA_CSS,
         "</style></head><body><div class=wrap>"]
    p.append("<div class=pub-hd><h1><span class=eye>&#128065;</span> Argus "
             "<span style='color:#8b94a3'>· 자율 투자 에이전트</span></h1>"
             f"<div class=sub>{escape(stamp)} 기준 스냅샷</div>"
             "<div class=pub-lead>시장 심리와 국면을 읽고, 종목별 리서치(도시에)를 근거로 "
             "진입·손절·목표를 스스로 정해 운용하는 봇의 현황입니다. "
             "아래는 봇이 <b>무엇을 보고 어떻게 판단했는지</b>만 담고 있습니다.</div></div>")
    p.append(_fear_html(d))
    p.append(_brief_html(d))
    p.append(_regime_html(d))
    p.append(_holdings_html(d))
    p.append(_armed_html(d))
    p.append(_dossier_html(d))
    p.append(_perf_html(d))
    p.append(_brain_html(d))
    p.append(f"<div class=pub-foot>{escape(FOOTER_NOTE)}</div>")
    p.append("</div></body></html>")
    return "".join(p)


# ───────────────────────── CLI ─────────────────────────

def main(argv: list[str] | None = None) -> int:
    """argv 를 주면 그걸 파싱한다 — 발행 스크립트가 이 함수를 직접 부를 때
    자기 argv(--dry 등)를 물려받아 파싱 에러가 나는 걸 막는다."""
    ap = argparse.ArgumentParser(description="Argus 공개 공유용 정적 페이지 생성")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"출력 파일 경로 (기본 {DEFAULT_OUT})")
    ap.add_argument("--stdout", action="store_true", help="파일 대신 표준출력으로(검증용)")
    ap.add_argument("--refresh-brief", action="store_true",
                    help="페이지 생성 전에 '오늘의 국면'을 갱신(TTL 존중, LLM 1콜). "
                         "기본은 갱신하지 않는다(페이지 생성은 항상 LLM 0콜).")
    args = ap.parse_args(argv)
    setup_logging("INFO", log_file="public_page.log")

    if args.refresh_brief:
        # 지연 import — scripts.public_brief 가 이 모듈을 쓰므로 최상단에 두면 순환된다.
        from scripts.public_brief import refresh_brief
        log.info("브리핑 갱신: %s", json.dumps(refresh_brief(), ensure_ascii=False))

    html = render_public(gather_public())
    bad = assert_no_leak(html)
    if bad:
        # 한 번 새면 회수가 안 된다 — 쓰지 않고 죽는다.
        log.error("유출 가드 위반 %d건 — 페이지를 쓰지 않고 중단합니다: %s",
                  len(bad), " | ".join(bad))
        return 2

    data = html.encode("utf-8")
    if args.stdout:
        sys.stdout.write(html)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")          # 원자적 쓰기(부분 파일 공개 방지)
    tmp.write_bytes(data)
    os.replace(tmp, out)
    log.info("공개 페이지 생성: %s (%d bytes)", out, len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
