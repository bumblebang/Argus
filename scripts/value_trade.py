"""밸류 트랙 V1 수동 진입점 — 라이브 집행은 데몬(watch.py) 전용.

  python scripts/value_trade.py               # 안내문(라이브 집행은 데몬 전용)
  python scripts/value_trade.py --live-check  # 셀렉터+게이트까지만(LLM·계좌 무접촉) 후보 출력
  python scripts/value_trade.py --dry         # tmp 격리로 전 흐름(결정→검증→체결→store) 완주

라이브 집행을 기본 실행에서 막는 이유: 페이퍼 계좌 파일(data/paper_account.json)은 데몬이
단일 소유하며, 수동 실행이 동시에 쓰면 계좌가 깨진다. --dry 는 전부 tmp 경로로 격리한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.agents.llm import MockLLM
from src.agents.schemas import DecisionOutput, ValidationOutput, Proposal, ValidationVerdict
from src.agents.value_trade import (value_trade_cfg, select_candidates, timing_gate,
                                    fair_price_low, passes_margin_guard, ValueRunner)
from src.value_scan import load_watchlist

log = get_logger("value_trade.cli")
_KST = ZoneInfo("Asia/Seoul")


def _live_check() -> int:
    """셀렉터 + 사전가드 + 타이밍 게이트까지만 실행(LLM·계좌 무접촉). 후보 출력."""
    import time
    from src.engine.store import Store
    from src.datasources.history import fetch_history

    cfg = load_config()
    cfg_v = value_trade_cfg(cfg)
    if not cfg_v["enabled"]:
        print("value_trade.enabled=false")
        return 0
    now = time.time()
    watchlist = load_watchlist()
    # store 는 읽기만(보유/진입대기 제외 반영). data/bot.db 는 WAL 이라 동시 읽기 안전.
    db_path = cfg.raw.get("run", {}).get("db_path", "data/bot.db")
    from src import paths as _paths
    store = Store(_paths.resolve("db", configured=db_path))
    cands = select_candidates(watchlist, store, cfg_v, now)
    print(f"[value live-check] watchlist={len(watchlist)} "
          f"selector 통과={len(cands)} (markets={cfg_v['markets']}, "
          f"min_conv={cfg_v['min_dossier_conviction']}, ttl={cfg_v['dossier_ttl_hours']}h)")
    gated = 0
    for c in cands:
        sym, market = c["symbol"], c["market"]
        try:
            df = fetch_history(sym, interval="1d", range_="1y", market=market,
                               max_age_hours=48)
        except Exception as e:
            print(f"  - {sym:8} {c.get('name','')[:12]:12} 히스토리 실패: {e}")
            continue
        price = None
        if df is not None and len(df):
            price = float(df["close"].astype(float).iloc[-1])
        fpl = fair_price_low(c)
        margin_ok = passes_margin_guard(c, price) if price else False
        timing = timing_gate(df, price) if price else None
        status = ("GATE-PASS" if (margin_ok and timing) else
                  "no-margin" if not margin_ok else "no-timing")
        if margin_ok and timing:
            gated += 1
        print(f"  - {sym:8} {str(c.get('name',''))[:12]:12} conv={c.get('conviction')} "
              f"price={price} fair_low={fpl} -> {status}"
              + (f" {timing}" if timing else ""))
    print(f"[value live-check] 게이트 통과 후보={gated}")
    return 0


def _synth_uptrend(low: float, high: float, n: int = 120) -> pd.DataFrame:
    """상승 추세 합성 일봉(마지막 종가=high). 타이밍 게이트 통과용."""
    closes = [low + (high - low) * (i / (n - 1)) for i in range(n)]
    return pd.DataFrame({"time": range(n), "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": [1000] * n})


def _dry() -> int:
    """MockLLM + tmp 계좌 + tmp store 로 전 흐름 완주. CWD 의 data/ 무접촉."""
    from src.paper_account import PaperAccount
    from src.risk_gate import RiskGate
    from src.broker import Broker
    from src.risk import RiskManager
    from src.engine.store import Store

    cfg = load_config()
    cfg.raw.setdefault("value_trade", {})["enabled"] = True
    tmp = Path(tempfile.mkdtemp(prefix="value_trade_dry_"))
    print(f"[value dry] tmp 격리 경로: {tmp}")

    # KR 장중 고정 시각(월 10:00 KST, 휴장 아님)으로 due·신선도 결정적 처리.
    now = datetime(2026, 7, 13, 10, 0, tzinfo=_KST).timestamp()

    # 합성 watchlist: KR 저평가 1종목(scan price 1000, fair_low +30% → 1300).
    wl = {"900001": {"name": "테스트밸류", "market": "KR", "ts": now,
                     "stance": "undervalued", "conviction": 0.55,
                     "fair_low_pct": 30.0, "fair_high_pct": 60.0,
                     "thesis": "[DRY] 합성 저평가 도시에", "risks": ["dry"],
                     "evidence": ["dry"], "metrics": {"price": 1000.0}}}
    wl_path = tmp / "value_watchlist.json"
    wl_path.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")

    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp / "paper_account.json")
    gate = RiskGate({"capital": cfg.risk.get("capital", {}), "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": cfg.risk.get("max_order_notional", {}),
                     "kill_switch_file": str(tmp / "HALT"),
                     "blocked_symbols_file": str(tmp / "krx_blocked_symbols.json")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    risk = RiskManager(capital=cfg.risk.get("capital", {}), max_position_pct=0.2)
    store = Store(tmp / "bot.db")

    def dry_llm_factory(cands):
        def responder(schema, system, user):
            if schema is DecisionOutput:
                props = [Proposal(symbol=c["symbol"], market=c["market"], side="BUY",
                                  conviction=0.7, horizon="position", target_weight=0.15,
                                  thesis="[DRY] 밸류 진입", key_risks=["dry"])
                         for c in cands]
                return DecisionOutput(market_view="[DRY] 밸류 슬리브", proposals=props)
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="[DRY] 승인") for s in syms])
        return MockLLM(responder)

    runner = ValueRunner(
        cfg, store, broker, risk, dry_llm_factory, None,
        fetch_history_fn=lambda s, m: _synth_uptrend(900.0, 1100.0),
        price_fn=None, watchlist_path=wl_path, state_path=tmp / "value_trade_state.json",
        now_fn=lambda: now)
    summary = runner.run()
    print(f"[value dry] summary: {json.dumps(summary, ensure_ascii=False)}")

    rows = store.get_open_positions()
    print(f"[value dry] store 포지션 {len(rows)}건:")
    for r in rows:
        meta = json.loads(r["meta"]) if r["meta"] else {}
        print(f"  - {r['symbol']} strategy={r['strategy']} qty={r['qty']} "
              f"entry={r['avg_price']} stop={r['stop_price']} target={r['target_price']} "
              f"source={meta.get('source')}")
    jp = tmp / "value_decisions.jsonl"
    print(f"[value dry] value_decisions.jsonl 기록={'있음' if jp.exists() else '없음'}")
    ok = bool(rows) and any(r["strategy"] == "value" for r in rows) and jp.exists()
    print(f"[value dry] E2E {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="tmp 격리 전 흐름 완주(MockLLM)")
    ap.add_argument("--live-check", action="store_true",
                    help="셀렉터+게이트까지만(LLM·계좌 무접촉) 후보 출력")
    args = ap.parse_args()
    setup_logging("INFO", log_file="value_trade.log")

    if args.dry:
        return _dry()
    if args.live_check:
        return _live_check()
    print("밸류 트랙 라이브 집행은 데몬(scripts/watch.py) 전용입니다 "
          "(페이퍼 계좌 파일 동시쓰기 방지).\n"
          "  --live-check : 셀렉터+게이트 후보만 출력(무접촉)\n"
          "  --dry        : tmp 격리로 전 흐름 완주")
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus value-trade")
    sys.exit(main())
