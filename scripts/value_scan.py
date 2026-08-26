"""저평가 주 스캔 진입점 — 5.5h 간격 스케줄(ArgusValueScan)이 돌린다.

  python scripts/value_scan.py             # 실스캔(sonnet) — 창 게이팅 없음(아무 때나)
  python scripts/value_scan.py --dry       # MockLLM(배선 검증, 발굴/히스토리는 실호출)
  python scripts/value_scan.py --limit 2   # 이번 실행 LLM 콜 상한
  python scripts/value_scan.py --pool 20   # 풀 축소(가벼운 프로빙용)

토스 무접촉(Naver/Yahoo/파일). LLM 한도에 걸리면 조용히 종료(다음 주기 재시도).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, ROOT
from src.logging_setup import setup_logging, get_logger
from src.agents.llm import ClaudeCLIClient, MockLLM
from src.agents.schemas import ValueDossier
from src.value_scan import run_scan
from src import paths as _paths

log = get_logger("value_scan.cli")


def _held_symbols() -> set[str]:
    """원장의 열린 포지션 — 재검증 우선순위. 실패해도 스캔은 계속(빈 set)."""
    try:
        from src.engine.store import Store
        store = Store(_paths.resolve("db", configured="data/bot.db"))
        return {r["symbol"] for r in store.get_open_positions() if r["symbol"]}
    except Exception as e:
        log.warning("보유 심볼 로드 실패(재검증 우선순위 없이 진행): %s", e)
        return set()


def _dry_llm() -> MockLLM:
    def responder(schema, system, user):
        assert schema is ValueDossier
        sym = json.loads(user).get("symbol", "?")
        return ValueDossier(stance="fair", conviction=0.5,
                            thesis=f"[DRY] {sym} 합성 밸류 도시에(배선 검증)",
                            evidence=["dry-run"])
    return MockLLM(responder)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="MockLLM 배선 검증")
    ap.add_argument("--limit", type=int, default=None, help="LLM 콜 상한(기본 config)")
    ap.add_argument("--pool", type=int, default=None, help="발굴 풀 축소(프로빙용)")
    args = ap.parse_args()
    setup_logging("INFO", log_file="value_scan.log")

    cfg = load_config()
    vcfg = cfg.raw.get("value_scan", {}) or {}
    if not vcfg.get("enabled", True):
        log.info("value_scan.enabled=false — 실행 안 함.")
        return 0
    if args.pool:
        cfg.raw.setdefault("value_scan", {})["pool"] = args.pool

    if args.dry:
        llm = _dry_llm()
    else:
        a = cfg.raw.get("agents", {})
        llm = ClaudeCLIClient(command=a.get("claude_command", "claude"),
                              model=(vcfg.get("model") or "sonnet"),
                              timeout=int(vcfg.get("timeout", 240)),
                              fallback_model=(a.get("claude_fallback_model") or None),
                              error_dump_path="data/value_scan_cli_error.json")

    summary = run_scan(cfg, llm, limit=args.limit, held_symbols=_held_symbols())
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus value-scan")
    sys.exit(main())
