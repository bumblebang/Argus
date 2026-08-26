"""판단 단위 리플레이 CLI.

  python scripts/replay_manager.py score-live
  python scripts/replay_manager.py score-live --min-date 2026-08-01
  python scripts/replay_manager.py redecide --dry
  python scripts/replay_manager.py redecide --with-validation --strip-track-record

라이브 store/저널에 쓰지 않는다. 리플레이 Δ 로 승격 금지.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.eval.score import score_journal
from src.logging_setup import setup_logging
from src import paths as _paths

DATA = ROOT / "data"


def _print(obj: dict) -> None:
    slim = {k: v for k, v in obj.items() if k != "rows"}
    print(json.dumps(slim, ensure_ascii=False, indent=2, default=str))
    if obj.get("rows") is not None:
        print(json.dumps({"n_row_preview": len(obj["rows"])}, ensure_ascii=False))


def _brain_journal() -> Path:
    return _paths.resolve("decisions", configured="data/decisions.jsonl")


def _cmd_score_live(args: argparse.Namespace) -> None:
    cfg = load_config(ROOT / "config.yaml") if (ROOT / "config.yaml").exists() else None
    raw = cfg.raw if cfg is not None else {}
    sleeve = args.sleeve
    journal = (DATA / "value_decisions.jsonl" if sleeve == "value"
               else _brain_journal())
    if args.journal:
        journal = Path(args.journal)
    out = score_journal(journal_path=journal, data_dir=DATA, cfg=raw,
                        min_date=args.min_date, min_n=args.min_n)
    _print(out)


def _cmd_redecide(args: argparse.Namespace) -> None:
    from src.agents.decision_agent import DecisionAgent
    from src.agents.llm import MockLLM
    from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput
    from src.agents.validation_agent import ValidationAgent
    from src.eval.replay import redecide_journal

    sleeve = args.sleeve
    journal = (DATA / "value_decisions.jsonl" if sleeve == "value"
               else _brain_journal())
    if args.journal:
        journal = Path(args.journal)

    if args.dry:
        def respond(schema, system, user):
            if schema is DecisionOutput:
                ctx = json.loads(user) if user.strip().startswith("{") else {}
                props = []
                for c in (ctx.get("candidates") or ctx.get("universe") or []):
                    if isinstance(c, dict) and c.get("symbol"):
                        props.append(Proposal(
                            symbol=c["symbol"], market=c.get("market") or "KR",
                            side="HOLD", conviction=0.5, target_weight=0.0,
                            thesis="dry-redecide"))
                return DecisionOutput(market_view="dry", proposals=props)
            return ValidationOutput(verdicts=[])
        llm = MockLLM(respond, model="dry-replay")
        decision_agent = DecisionAgent(llm)
        validation_agent = ValidationAgent(llm, min_conviction=0) if args.with_validation else None
    else:
        from src.agents.pipeline import build_live_llm
        cfg = load_config(ROOT / "config.yaml")
        a = cfg.raw.get("agents") or {}
        llm = build_live_llm(
            cfg, use_cli=str(a.get("backend") or "").lower() in ("cli", "claude_cli"),
            subscription=bool(a.get("subscription")), api_key=None)
        decision_agent = DecisionAgent(llm)
        validation_agent = (ValidationAgent(llm) if args.with_validation else None)

    rows = redecide_journal(
        journal, decision_agent, strip_track=args.strip_track_record,
        validation_agent=validation_agent, min_date=args.min_date,
        limit=args.limit)
    print(json.dumps({
        "n": len(rows),
        "changed": sum(r.get("n_changed") or 0 for r in rows),
        "note": "재결정 결과는 라이브 저널/store 에 쓰지 않음. 승격 금지.",
        "preview": rows[:3],
    }, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Argus decision-unit replay")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("score-live", help="journal+archive vs labels vs null (no LLM)")
    p1.add_argument("--journal", type=Path, default=None)
    p1.add_argument("--sleeve", choices=("brain", "value"), default="brain")
    p1.add_argument("--min-date", default=None)
    p1.add_argument("--min-n", type=int, default=20)
    p1.set_defaults(func=_cmd_score_live)

    p2 = sub.add_parser("redecide", help="frozen context -> new DecisionAgent (no broker)")
    p2.add_argument("--journal", type=Path, default=None)
    p2.add_argument("--sleeve", choices=("brain", "value"), default="brain")
    p2.add_argument("--min-date", default=None)
    p2.add_argument("--limit", type=int, default=None)
    p2.add_argument("--dry", action="store_true", help="MockLLM HOLD-all (no API)")
    p2.add_argument("--with-validation", action="store_true")
    p2.add_argument("--strip-track-record", action="store_true")
    p2.set_defaults(func=_cmd_redecide)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
