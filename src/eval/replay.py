"""동결 컨텍스트 재결정 — 브로커/run_cycle 집행 분기 호출 금지.

새 DecisionAgent.decide 만 호출한다. ValidationAgent 는 옵션.
라이브 store·저널에 쓰지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .archive import load_context, strip_track_record
from ..shadow_ledger import parse_ts


def redecide_record(rec: dict, journal_path: Path | str, decision_agent,
                    *, strip_track: bool = False,
                    validation_agent=None) -> dict[str, Any]:
    """한 저널 행의 동결 컨텍스트를 재결정. broker 없음."""
    ref = rec.get("context_ref")
    if not ref:
        raise ValueError("context_ref 없음 — 아카이브 이후 저널만 재결정 가능")
    raw = load_context(journal_path, ref, expected_sha256=rec.get("context_sha256"))
    if strip_track:
        raw = strip_track_record(raw)
    decision = decision_agent.decide(raw)
    validation = None
    if validation_agent is not None:
        validation = validation_agent.review(raw, decision)
    live = {p.get("symbol"): p.get("side") for p in (rec.get("proposals") or [])
            if isinstance(p, dict)}
    new_sides = {p.symbol: p.side for p in decision.proposals}
    return {
        "ts": rec.get("ts"),
        "cycle_ts": parse_ts(rec.get("ts")),
        "live_sides": live,
        "new_sides": new_sides,
        "decision": decision.model_dump(),
        "validation": (validation.model_dump() if validation is not None else None),
        "n_changed": sum(1 for s, side in new_sides.items() if live.get(s) != side),
    }


def redecide_journal(journal_path: Path | str, decision_agent, *,
                     strip_track: bool = False,
                     validation_agent=None,
                     min_date: str | None = None,
                     limit: int | None = None) -> list[dict]:
    """저널 전체를 재결정. 파일에 쓰지 않음."""
    from .score import _parse_min_date, _rec_date
    journal_path = Path(journal_path)
    cutoff = _parse_min_date(min_date)
    out: list[dict] = []
    if not journal_path.exists():
        return out
    with journal_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not rec.get("context_ref"):
                continue
            d = _rec_date(rec)
            if cutoff and d is not None and d < cutoff:
                continue
            out.append(redecide_record(
                rec, journal_path, decision_agent,
                strip_track=strip_track, validation_agent=validation_agent))
            if limit is not None and len(out) >= limit:
                break
    return out
