"""오프라인 일관성 CLI — 라이브 앙상블 없음.

  python scripts/consistency_manager.py --journal data/decisions.jsonl --n 5 --dry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.eval.archive import load_context, parse_context
from src.eval.consistency import bucket_by_regime, consistency_report
from src.logging_setup import setup_logging


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Offline decision consistency")
    ap.add_argument("--journal", type=Path, default=ROOT / "data" / "decisions.jsonl")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    from src.agents.decision_agent import DecisionAgent
    from src.agents.llm import MockLLM
    from src.agents.schemas import DecisionOutput, Proposal

    if args.dry:
        def respond(schema, system, user):
            ctx = json.loads(user) if user.strip().startswith("{") else {}
            props = []
            for c in (ctx.get("candidates") or ctx.get("universe") or []):
                if isinstance(c, dict) and c.get("symbol"):
                    props.append(Proposal(
                        symbol=c["symbol"], market=c.get("market") or "KR",
                        side="HOLD", conviction=0.5, target_weight=0.0,
                        thesis="dry-consistency"))
            return DecisionOutput(market_view="dry", proposals=props)
        llm = MockLLM(respond, model="dry-consistency")
        agent = DecisionAgent(llm)
    else:
        from src.agents.pipeline import build_live_llm
        from src.config import load_config
        cfg = load_config(ROOT / "config.yaml")
        a = cfg.raw.get("agents") or {}
        agent = DecisionAgent(build_live_llm(
            cfg, use_cli=str(a.get("backend") or "").lower() in ("cli", "claude_cli"),
            subscription=bool(a.get("subscription")), api_key=None))

    reports = []
    n_done = 0
    if args.journal.exists():
        for line in args.journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            ref = rec.get("context_ref")
            if not ref:
                continue
            raw = load_context(args.journal, ref, expected_sha256=rec.get("context_sha256"))
            ctx = parse_context(raw)
            reports.append(consistency_report(
                ctx, agent.decide, n=args.n, context_json=raw))
            n_done += 1
            if n_done >= args.limit:
                break
    print(json.dumps({
        "n_contexts": len(reports),
        "n_runs": args.n,
        "by_regime": bucket_by_regime(reports),
        "mean_agreement": (
            sum(r["exact_agreement"] for r in reports if r.get("exact_agreement") is not None)
            / max(1, sum(1 for r in reports if r.get("exact_agreement") is not None))
            if reports else None),
        "note": "오프라인 전용. 라이브 다수결 집행 없음.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
