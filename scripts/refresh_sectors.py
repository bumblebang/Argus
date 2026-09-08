"""현재 유니버스의 섹터를 실데이터로 채우고 data/universe.yaml 에 반영한다.

평소에는 universe_roll 이 코어 리프레시/무버 스캔마다 자동으로 채운다. 이 스크립트는
그 사이클을 기다리지 않고 지금 당장 커버리지를 채우거나(도입 직후), 캐시를 강제
갱신할 때 쓴다.

사용:
    python scripts/refresh_sectors.py            # 캐시 채우고 universe.yaml 갱신
    python scripts/refresh_sectors.py --dry-run  # 커버리지만 보고 파일은 안 건드림
    python scripts/refresh_sectors.py --force    # TTL 무시하고 전량 재조회

US 는 Finnhub 무료 티어(60req/min)라 심볼당 ~1.1초 걸린다. --max-us 로 조절.
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

from src.datasources.sector_class import (                        # noqa: E402
    CACHE_PATH, ensure_sectors, load_cache, save_cache, sector_for)
from src.sector_taxonomy import normalize_sector                  # noqa: E402
from src.universe_roll import OUT, _atomic_write, _load_yaml      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="조회·커버리지 보고만, universe.yaml 미변경")
    ap.add_argument("--force", action="store_true",
                    help="캐시 TTL 무시하고 전량 재조회")
    ap.add_argument("--max-us", type=int, default=250,
                    help="이번 실행에서 새로 조회할 US 심볼 상한(기본 250)")
    args = ap.parse_args()

    universe = _load_yaml(OUT)
    if not universe:
        print(f"{OUT} 가 비었습니다 — 먼저 유니버스를 굴리세요.")
        return 1
    symbols = {m: [it["symbol"] for it in (lst or []) if it.get("symbol")]
               for m, lst in universe.items()}
    total = sum(len(v) for v in symbols.values())
    print(f"유니버스 {total}종목 — " +
          ", ".join(f"{m} {len(v)}" for m, v in symbols.items()))

    if args.force and CACHE_PATH.exists():
        backup = CACHE_PATH.with_suffix(".json.bak")
        backup.write_text(CACHE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        CACHE_PATH.unlink()
        print(f"--force: 기존 캐시를 {backup.name} 로 옮기고 전량 재조회")

    t0 = time.time()
    cache = ensure_sectors(symbols, max_us_fetch=args.max_us)
    print(f"섹터 조회 {time.time() - t0:.0f}초")

    filled, missing, dist = 0, [], collections.Counter()
    for market, lst in universe.items():
        for it in (lst or []):
            sym = it.get("symbol")
            if not sym:
                continue
            sec = sector_for(cache, market, sym) or normalize_sector(it.get("sector"))
            if sec:
                it["sector"] = sec
                filled += 1
                dist[sec] += 1
            else:
                it.pop("sector", None)
                missing.append(f"{market}:{sym}")

    pct = 100.0 * filled / total if total else 0.0
    print(f"\n커버리지 {filled}/{total} ({pct:.0f}%)")
    for sec, n in dist.most_common():
        print(f"  {sec:8s} {n:4d}  ({100.0 * n / total:4.1f}%)")
    if missing:
        print(f"\n미분류 {len(missing)}종목: {', '.join(missing[:20])}"
              + (" ..." if len(missing) > 20 else ""))

    if args.dry_run:
        print("\n--dry-run: universe.yaml 미변경")
        return 0
    _atomic_write(universe, OUT)
    save_cache(cache)
    print(f"\n{OUT.name} 갱신 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
