"""유니버스 종목 StockInfo 일괄 갱신 — nxtSupported 등 캐시 채우기.

watch 와 토큰 충돌 가능 → 짧게 끝나므로 보통 OK. 불안하면 watch 멈춘 뒤 실행.

  python scripts/refresh_stock_info_batch.py
  python scripts/refresh_stock_info_batch.py --symbols 005930,000660
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.datasources.stock_info import INFO_CACHE_PATH, _load, _save, fetch_stock_info
from src.logging_setup import setup_logging, get_logger
from src.toss_client import TossClient

log = get_logger("refresh_stock_info")


def _universe_kr() -> list[str]:
    p = Path(__file__).resolve().parent.parent / "data" / "universe.yaml"
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [str(x["symbol"]).zfill(6) for x in (raw.get("KR") or []) if x.get("symbol")]


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="콤마구분. 없으면 universe KR 전체")
    args = ap.parse_args()

    syms = [s.strip().zfill(6) for s in args.symbols.split(",")] if args.symbols else _universe_kr()
    if not syms:
        log.error("심볼 없음")
        return 1

    cfg = load_config()
    client = TossClient(cfg.creds)
    cache = _load(INFO_CACHE_PATH)
    now = time.time()
    nxt_yes = nxt_no = nxt_unk = 0

    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        try:
            infos = fetch_stock_info(client, chunk)
        except Exception as e:
            log.error("배치 실패 %s: %s", chunk[:3], e)
            continue
        for sym, info in infos.items():
            cache[sym] = {"fetched": now, "info": info}
            ns = info.get("nxtSupported")
            if ns is True:
                nxt_yes += 1
            elif ns is False:
                nxt_no += 1
            else:
                nxt_unk += 1
        time.sleep(0.25)

    _save(INFO_CACHE_PATH, cache)
    log.info("갱신 %d종목 | nxtSupported: true=%d false=%d unknown=%d",
             len(syms), nxt_yes, nxt_no, nxt_unk)
    print(f"OK {len(syms)} symbols | nxt true={nxt_yes} false={nxt_no} unknown={nxt_unk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
