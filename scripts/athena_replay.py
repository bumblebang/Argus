"""Athena 리플레이 A/B — 과거 (종목, 날짜)를 그 날 시점 데이터로 다시 판정.

  python scripts/athena_replay.py probe                       # 모델 학습 시점 확인(1콜)
  python scripts/athena_replay.py sample --market KR --n 10 --tag pilot
  python scripts/athena_replay.py run --tag pilot --limit 4    # 4콜만(토큰 확인)
  python scripts/athena_replay.py score --tag pilot

A안(base)=차트·베이스레이트, B안(inputs)=+재무·수급·공매도. 결과는
data/athena_replay/<tag>.cases.json · <tag>.results.jsonl (라이브 store 미접촉).
claude CLI 구독 한도를 라이브 뇌와 나눠 쓰므로 Athena 창 안에서는 시작하지 않고,
도는 중 창이 열리면 멈춘다. --limit 으로 호출 수를 끊어 쓴다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import paths as _paths  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine.store import Store  # noqa: E402
from src.eval import athena_replay as ar  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "athena_replay"
KST = ZoneInfo("Asia/Seoul")


def _set_home(home: Path) -> None:
    """데이터·설정·.env 위치(기본 이 저장소). worktree 에서 라이브 데이터로 돌릴 때 지정."""
    global DATA, OUT
    DATA, OUT = home / "data", home / "data" / "athena_replay"
    try:
        from dotenv import load_dotenv
        load_dotenv(home / ".env", override=False)
    except ImportError:
        pass


def _store() -> Store:
    p = DATA / "state" / "bot.db"
    return Store(p if p.exists() else _paths.resolve("db", configured="data/bot.db"))


def _paths_for(tag: str) -> tuple[Path, Path]:
    return OUT / f"{tag}.cases.json", OUT / f"{tag}.results.jsonl"


def _in_athena_window(cfg_raw: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    hm = now.strftime("%H:%M")
    for wins in ((cfg_raw.get("athena") or {}).get("windows") or {}).values():
        for w in wins or []:
            if str(w.get("start")) <= hm < str(w.get("stop")):
                return True
    return False


def _llm(cfg_raw: dict, *, dry: bool):
    if dry:
        from src.agents.llm import MockLLM
        from src.agents.schemas import DossierOutput

        def respond(schema, system, user):
            ctx = json.loads(user)
            px = float((ctx.get("technical") or {}).get("price") or 100)
            stance = "bullish" if ctx.get("fundamentals") else "neutral"
            return DossierOutput(stance=stance, thesis="dry", conviction=0.5,
                                 entry_low=round(px * .98, 2), entry_high=round(px * 1.0, 2),
                                 invalidation=round(px * .94, 2), target=round(px * 1.1, 2))
        llm = MockLLM(respond, model="dry")
        llm.calls = []
        return llm
    from src.agents.llm import ClaudeCLIClient
    a, acfg = cfg_raw.get("agents") or {}, cfg_raw.get("athena") or {}
    OUT.mkdir(parents=True, exist_ok=True)
    return ar.usage_cli_client(
        ClaudeCLIClient, command=a.get("claude_command", "claude"),
        model=(acfg.get("model") or "sonnet"), timeout=int(acfg.get("timeout", 240)),
        fallback_model=None, error_dump_path=OUT / "cli_error.json")


def cmd_probe(args, cfg) -> int:
    llm = _llm(cfg.raw, dry=False)
    out = llm._run("Answer in one line: your model name and the year-month of your "
                   "training data cutoff (YYYY-MM). No other text.")
    print("응답:", out.strip())
    print("사용량:", json.dumps(llm.calls, ensure_ascii=False))
    return 0


def cmd_sample(args, cfg) -> int:
    store = _store()
    until = (datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc).timestamp()
             if args.until else time.time() - 21 * 86400)
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
    cases = ar.sample_cases(store, data_dir=DATA, market=args.market, since=since,
                            until=until, n=args.n, seed=args.seed, cfg=cfg.raw)
    cpath, _ = _paths_for(args.tag)
    OUT.mkdir(parents=True, exist_ok=True)
    cpath.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    live = {}
    for c in cases:
        live[c["live_stance"]] = live.get(c["live_stance"], 0) + 1
    print(f"표본 {len(cases)}건 → {cpath}  (라이브 판정 {live})")
    return 0


def cmd_run(args, cfg) -> int:
    cpath, rpath = _paths_for(args.tag)
    cases = json.loads(cpath.read_text(encoding="utf-8"))
    if not args.dry and not args.force and _in_athena_window(cfg.raw):
        print("Athena 창 안 — 라이브와 한도가 겹쳐 시작하지 않음(--force 로 무시)")
        return 1
    inputs = None
    arms = tuple(a for a in args.arms.split(",") if a)
    if "inputs" in arms:
        from src.agents.athena_inputs import AthenaInputs
        inputs = AthenaInputs.load(DATA, connect_krx=not args.dry)
    names = {}
    try:
        import yaml
        uni = yaml.safe_load((DATA / "universe.yaml").read_text(encoding="utf-8")) or {}
        names = {it["symbol"]: it.get("name") for m in uni.values() for it in (m or [])
                 if isinstance(it, dict) and it.get("symbol")}
    except Exception:
        pass
    llm = _llm(cfg.raw, dry=args.dry)
    stop = None if (args.dry or args.force) else (lambda: _in_athena_window(cfg.raw))
    res = ar.run_cases(cases, llm=llm, data_dir=DATA, out_path=rpath, arms=arms,
                       inputs=inputs, limit=args.limit, stop_fn=stop, names=names)
    print("실행:", res)
    _print_tokens(llm.calls)
    return 0


def _print_tokens(calls: list[dict]) -> None:
    if not calls:
        return
    tot = {k: sum(int(c.get(k) or 0) for c in calls)
           for k in ("input_tokens", "cache_creation_input_tokens",
                     "cache_read_input_tokens", "output_tokens")}
    cost = sum(float(c.get("cost_usd") or 0) for c in calls)
    n = len(calls)
    print(f"이번 실행 {n}콜 · 입력 {tot['input_tokens']:,} + 캐시생성 "
          f"{tot['cache_creation_input_tokens']:,} + 캐시읽기 {tot['cache_read_input_tokens']:,}"
          f" · 출력 {tot['output_tokens']:,} · 환산 ${cost:.3f}")
    print(f"  콜당 평균: 입력계 {(sum(tot.values()) - tot['output_tokens']) // n:,} · "
          f"출력 {tot['output_tokens'] // n:,} · ${cost / n:.4f}")


def cmd_score(args, cfg) -> int:
    _, rpath = _paths_for(args.tag)
    rows = ar.load_results(rpath)
    rep = ar.score_results(rows, data_dir=DATA, cfg=cfg.raw)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Athena 리플레이 A/B")
    ap.add_argument("--home", type=Path, default=ROOT,
                    help="data/·config.yaml·.env 가 있는 argus 루트(기본: 이 저장소)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    s = sub.add_parser("sample")
    s.add_argument("--tag", required=True)
    s.add_argument("--market", default="KR")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--since", default="2026-07-13")
    s.add_argument("--until", default=None, help="기본: 21일 전(20일 창 성숙)")
    s.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--limit", type=int, default=4, help="이번 실행 최대 LLM 콜")
    r.add_argument("--arms", default="base,inputs")
    r.add_argument("--dry", action="store_true")
    r.add_argument("--force", action="store_true")
    c = sub.add_parser("score")
    c.add_argument("--tag", required=True)
    args = ap.parse_args()
    _set_home(args.home)
    cfg_path = args.home / "config.yaml"
    cfg = load_config(cfg_path if cfg_path.exists() else None)
    return {"probe": cmd_probe, "sample": cmd_sample, "run": cmd_run,
            "score": cmd_score}[args.cmd](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
