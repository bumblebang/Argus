"""공개 페이지용 '오늘의 국면' 코멘트 생성 — 하루 1콜(TTL 안이면 호출조차 안 한다).

  python scripts/public_brief.py           # TTL 안이면 스킵, 지났으면 실호출(sonnet)
  python scripts/public_brief.py --dry     # MockLLM(배선 검증, LLM 0콜)
  python scripts/public_brief.py --force   # TTL 무시하고 무조건 갱신

컨텍스트는 `src/public_brief.build_public_context()` 가 조립한다 — **계좌 정보가 아예
없는** 화이트리스트다(왜 그런지는 그 모듈 독스트링 참조). 토스 무접촉(파일/DB 읽기 + LLM).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.agents.llm import ClaudeCLIClient, MockLLM
from src.public_brief import BRIEF_PATH, PublicBrief, generate, load_brief

log = get_logger("public_brief.cli")

MARKET_STATE = ROOT / "data" / "market_state.json"


def _dry_llm() -> MockLLM:
    def responder(schema, system, user):
        assert schema is PublicBrief
        ctx = json.loads(user)
        reg = ", ".join(f"{m}={r.get('label')}" for m, r in (ctx.get("regime") or {}).items())
        return PublicBrief(headline="[DRY] 합성 국면 코멘트(배선 검증)",
                           body=f"[DRY] 국면 {reg or '없음'}. 실제 LLM 호출 없이 생성된 "
                                "더미 브리핑이다. 배선만 검증한다.",
                           watch=["[DRY] 지켜볼 것 없음"])
    return MockLLM(responder)


def _market_state() -> dict:
    """data/market_state.json 읽기(실패는 빈 dict — 컨텍스트가 얇아질 뿐 죽지 않는다)."""
    try:
        d = json.loads(MARKET_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("[brief] market_state 로드 실패(빈 컨텍스트로 진행): %s", e)
        return {}
    return d if isinstance(d, dict) else {}


def _dossier_stances() -> dict:
    """신선한 도시에의 stance **개수 분포**만 집계한다(종목·가격은 담지 않는다).

    수집은 `public_page.gather_public()` 재사용 — 이미 bot.db 를 읽기전용으로 열어
    도시에를 가져온다(라이브 데몬 무방해). 반환 dict 의 다른 필드는 쓰지 않는다.
    """
    try:
        from scripts.public_page import gather_public
        rows = gather_public().get("dossiers") or []
    except Exception as e:
        log.warning("[brief] 도시에 집계 실패(분포 없이 진행): %s", e)
        return {}
    counts: dict[str, int] = {}
    for x in rows:
        s = (x or {}).get("stance")
        if s:
            counts[str(s)] = counts.get(str(s), 0) + 1
    return counts


def refresh_brief(*, force: bool = False, dry: bool = False) -> dict:
    """브리핑 갱신 1회. TTL 안이고 --force 가 아니면 **LLM 을 부르지 않고** 스킵.

    반환 요약 dict(stdout JSON 및 public_page --refresh-brief 로그용).
    """
    from scripts.public_page import public_cfg
    pcfg = public_cfg()
    if not pcfg["enabled"]:
        log.info("public_page.enabled=false — 브리핑 생성 안 함.")
        return {"status": "disabled"}

    cached = load_brief(ttl_hours=pcfg["brief_ttl_hours"])
    if cached and not force:
        log.info("[brief] TTL(%.0fh) 안 — 재호출 없이 종료.", pcfg["brief_ttl_hours"])
        return {"status": "cached", "headline": cached.get("headline"),
                "ts": cached.get("ts")}

    if dry:
        llm = _dry_llm()
    else:
        a = load_config().raw.get("agents", {}) or {}
        llm = ClaudeCLIClient(command=a.get("claude_command", "claude"),
                              model=pcfg["brief_model"],
                              timeout=pcfg["brief_timeout"],
                              fallback_model=(a.get("claude_fallback_model") or None),
                              error_dump_path="data/public_brief_cli_error.json")

    brief = generate(llm, _market_state(), _dossier_stances())
    if brief is None:
        # 생성 실패거나 유출 가드에 걸렸다 — 캐시는 건드리지 않았다(이전 것이 남아 있으면 그게 쓰인다).
        return {"status": "failed", "path": str(BRIEF_PATH)}
    return {"status": "generated", "headline": brief["headline"],
            "watch": brief["watch"], "path": str(BRIEF_PATH)}


def main() -> int:
    ap = argparse.ArgumentParser(description="공개 페이지용 '오늘의 국면' 코멘트 생성")
    ap.add_argument("--dry", action="store_true", help="MockLLM 배선 검증(LLM 0콜)")
    ap.add_argument("--force", action="store_true", help="TTL 무시하고 무조건 갱신")
    args = ap.parse_args()
    setup_logging("INFO", log_file="public_brief.log")

    summary = refresh_brief(force=args.force, dry=args.dry)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
