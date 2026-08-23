"""Athena 리서치 창 진입점 — 시장 교대 딥리서치 배치.

  python scripts/athena.py                 # 창 자동 판별(KST): KR=US마감 후 아침 / US=KR마감 후 저녁
  python scripts/athena.py --market US     # 시장 강제 지정(창 밖이어도 실행, 하드스톱 무시)
  python scripts/athena.py --dry           # MockLLM(합성 도시에) — 배선 검증
  python scripts/athena.py --limit 5       # 이번 실행 도시에 수 상한

설계: 직전에 닫힌 시장의 정보를 물고 곧 열릴 시장을 리서치한다. 개장 전
하드스톱(config athena.windows)으로 장중 뇌의 LLM 사용량을 보호한다.
토스 무접촉(Yahoo/파일) — 데몬과 동시 실행 안전(SQLite WAL).
배치가 끝나면(성공·도씨에 0건이어도) watch 뇌에 athena_done 각성 요청을 남긴다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, ROOT, resolve_universe
from src.logging_setup import setup_logging, get_logger
from src.engine.store import Store
from src.engine.wake_request import request_brain_wake
from src.agents.athena import run_batch
from src.universe_roll import sort_core_by_turnover
from src.agents.llm import ClaudeCLIClient, MockLLM
from src.agents.schemas import DossierOutput
from src.datasources.history import fetch_history

log = get_logger("athena.cli")

KST = ZoneInfo("Asia/Seoul")
DATA = ROOT / "data"


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _window_of(cfg: dict, now: datetime) -> tuple[str | None, float | None]:
    """지금이 어느 리서치 창인지. (market, stop_at_epoch) — 창 밖이면 (None, None).

    시장당 창 여러 개 지원(list) — 예: KR 은 본창(05:30~08:10) + 갭필 창(08:20~08:55,
    08:30 스크리너 신규 종목을 개장 전에 커버).
    """
    for market, w in (cfg.get("windows") or {}).items():
        specs = w if isinstance(w, list) else [w]
        for spec in specs:
            start, stop = _parse_hhmm(spec["start"]), _parse_hhmm(spec["stop"])
            if start <= now.time() < stop:
                stop_dt = now.replace(hour=stop.hour, minute=stop.minute,
                                      second=0, microsecond=0)
                return market, stop_dt.timestamp()
    return None, None


def _dry_llm() -> MockLLM:
    def responder(schema, system, user):
        assert schema is DossierOutput
        sym = json.loads(user).get("symbol", "?")
        return DossierOutput(stance="neutral", conviction=0.5,
                             thesis=f"[DRY] {sym} 합성 도시에(배선 검증)",
                             evidence=["dry-run"])
    return MockLLM(responder)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["KR", "US", "auto"], default="auto")
    ap.add_argument("--limit", type=int, default=None, help="도시에 수 상한(기본 config)")
    ap.add_argument("--dry", action="store_true", help="MockLLM 배선 검증")
    args = ap.parse_args()
    setup_logging("INFO", log_file="athena.log")

    cfg = load_config()
    acfg = cfg.raw.get("athena", {})
    now = datetime.now(KST)

    if args.market == "auto":
        market, stop_at = _window_of(acfg, now)
        if not market:
            log.info("리서치 창 밖(%s) — 실행 안 함. --market 로 강제 가능.",
                     now.strftime("%H:%M"))
            return 0
    else:
        market, stop_at = args.market, None      # 강제 지정 시 하드스톱 없음(수동 책임)

    store = Store(cfg.raw.get("run", {}).get("db_path", "data/bot.db"))
    # 스윙 멤버십은 유지하고 거래대금 순으로만 재정렬(Athena 미커버 우선순위).
    sort_core_by_turnover(market)
    cfg.raw["universe"] = resolve_universe(cfg.raw, ROOT / "data")
    if args.dry:
        llm = _dry_llm()
    else:
        a = cfg.raw.get("agents", {})
        llm = ClaudeCLIClient(command=a.get("claude_command", "claude"),
                              model=(acfg.get("model") or "sonnet"),
                              timeout=int(acfg.get("timeout", 240)),
                              fallback_model=(a.get("claude_fallback_model") or None))

    ms = (json.loads((DATA / "market_state.json").read_text(encoding="utf-8"))
          if (DATA / "market_state.json").exists() else {})
    br = {}
    if (DATA / "base_rates.json").exists():
        br = json.loads((DATA / "base_rates.json").read_text(encoding="utf-8")) \
            .get("symbols", {})

    def fetch_df(sym: str, mkt: str):
        return fetch_history(sym, interval="1d", range_="2y", market=mkt,
                             max_age_hours=20)

    summary = run_batch(cfg, store, llm, market,
                        limit=args.limit or int(acfg.get("max_per_run", 30)),
                        ttl_hours=float(acfg.get("ttl_hours", 60)),
                        stop_at=stop_at, fetch_df=fetch_df,
                        market_state=ms, base_rates=br)
    print(json.dumps(summary, ensure_ascii=False))
    # 도씨에 배치 직후 뇌 1회 — watch 데몬이 data/brain_wake_request.json 을 소비.
    # --dry 는 배선 검증만이라 각성 요청을 남기지 않는다.
    if not args.dry:
        wake_path = ((cfg.raw.get("watch") or {}).get("wake_request_path")
                     or "data/brain_wake_request.json")
        request_brain_wake(reason="athena_done", market=market, path=wake_path,
                           extra={"done": summary.get("done"),
                                  "failed": summary.get("failed"),
                                  "targets": summary.get("targets")})
        log.info("뇌 각성 요청 기록 — reason=athena_done market=%s path=%s",
                 market, wake_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
