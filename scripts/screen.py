"""장전 종목 스크리닝 — 동적 유니버스 생성(얇은 CLI, 실로직은 src/universe_roll.py).

  python scripts/screen.py            # 거래대금 상위 발굴 + Yahoo 백테스트 -> data/universe.yaml
  python scripts/screen.py --dry      # 네트워크 없이 합성데이터로 동작 확인(data/universe.dry.yaml)
  python scripts/screen.py --count 40 # 발굴 후보 풀 크기

흐름: 발굴(Naver KR / Yahoo US 거래대금 상위) → Yahoo 일봉(검증/지표) → 하드필터+전략별
      랭킹 → data/universe.yaml(레이어 태그·원자적 쓰기). 봇(config)이 screener.enabled 면
      이 파일을 우선 사용. 역할분리: 발굴=Naver/Yahoo, 히스토리=Yahoo, 라이브 매매=토스.

이제 유니버스 생명주기는 상주 데몬(engine.universe_refresher)이 소유한다. 이 CLI 는 수동
실행·점검용으로, 데몬과 **같은 쓰기 경로**(universe_roll.core_refresh: 원자적·레이어 태그)
를 store 없이(=보존 생략) 호출한다. 작업스케줄러의 정례 스크린은 데몬 코어와 충돌하므로
run_market_state.bat 에서 제거됨(거기 주석 참고).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src import universe_roll

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DRY = DATA_DIR / "universe.dry.yaml"   # --dry 는 실제 유니버스를 덮지 않는다


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="네트워크 없이 합성데이터로 실행")
    ap.add_argument("--count", type=int, default=40, help="발굴 후보 풀 크기(거래대금 상위 N)")
    args = ap.parse_args()

    setup_logging("INFO")
    log = get_logger("screen")
    cfg = load_config()
    cfg.raw.setdefault("screener", {})["count"] = args.count
    # --dry: 합성 경로로 강제 + 라이브 유니버스 오염 방지(전용 파일로 리다이렉트).
    orig_out, orig_dry = universe_roll.OUT, universe_roll._DRY
    if args.dry:
        universe_roll._DRY = True
        universe_roll.OUT = OUT_DRY
    try:
        # 수동 실행은 store 없이(보존 생략) — 데몬과 같은 원자적·태그 쓰기 경로.
        results = {m: universe_roll.core_refresh(cfg, m) for m in ("KR", "US")}
    finally:
        universe_roll.OUT, universe_roll._DRY = orig_out, orig_dry

    out = OUT_DRY if args.dry else universe_roll.OUT
    if not any(results.values()):
        log.error("선정 0종목(발굴/필터 실패?). %s 갱신 안 함.", out.name)
        return 1
    # 마지막 성공 결과에 전체 유니버스가 담겨 있다(부분 교체 누적).
    final = next(r for r in reversed(list(results.values())) if r)
    total = sum(len(v or []) for v in final.values())
    log.info("선정 %d종목 -> %s", total, out)
    for market, items in final.items():
        for it in (items or []):
            log.info("  [%s] %s %s -> %s (%s)", market, it.get("symbol"),
                     it.get("name"), it.get("strategy"), it.get("layer"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
