"""라이브 사이클 입력 컨텍스트 gzip 아카이브.

저널에는 포인터만 남긴다(context_ref / sha256 / bytes). 본문은
`data/context_archive/{YYYY-MM-DD}/{cycle_ts}_{sha16}.json.gz`.
저널이 ledgers/ 로 옮겨도 아카이브는 항상 data/context_archive 고정.
쓰기가 실패해도 호출측이 사이클을 계속해야 한다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..logging_setup import get_logger

log = get_logger("eval.archive")

ARCHIVE_DIRNAME = "context_archive"
_DATA = ROOT / "data"


def sleeve_from_journal(journal_path: str | Path) -> str:
    name = Path(journal_path).name.lower()
    if "value" in name:
        return "value"
    return "brain"


def archive_root(journal_path: str | Path | None = None) -> Path:
    """repo data/ 아래 저널이면 data/context_archive, 테스트 tmp 는 저널 옆."""
    if journal_path is None:
        return _DATA / ARCHIVE_DIRNAME
    jp = Path(journal_path).resolve()
    try:
        rel = jp.relative_to(ROOT.resolve())
    except ValueError:
        return jp.parent / ARCHIVE_DIRNAME
    if rel.parts and rel.parts[0] == "data":
        return _DATA / ARCHIVE_DIRNAME
    return jp.parent / ARCHIVE_DIRNAME


def persist_context(context_json: str, *, cycle_ts: float,
                    journal_path: str | Path,
                    manager: dict | None = None) -> dict:
    """context_json 을 gzip 으로 저장하고 저널 포인터 dict 을 반환."""
    raw = (context_json or "").encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    day = datetime.fromtimestamp(cycle_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    ts_tag = f"{cycle_ts:.3f}".replace(".", "p")
    rel = f"{day}/{ts_tag}_{sha[:16]}.json.gz"
    dest = archive_root(journal_path) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(raw, mtime=0)
    dest.write_bytes(blob)
    n_cand = 0
    try:
        ctx = json.loads(context_json or "{}")
        n_cand = len(ctx.get("candidates") or ctx.get("universe") or [])
    except (ValueError, TypeError):
        pass
    log.info("context archive %s bytes=%d candidates=%d epoch=%s",
             rel, len(raw), n_cand,
             (manager or {}).get("epoch"))
    return {
        "context_ref": f"{ARCHIVE_DIRNAME}/{rel}",
        "context_sha256": sha,
        "context_bytes": len(raw),
        "sleeve": sleeve_from_journal(journal_path),
        "n_candidates": n_cand,
    }


def resolve_ref(journal_path: str | Path, context_ref: str) -> Path:
    """data/context_archive 우선, 없으면 저널 부모(레거시/테스트)에서 탐색."""
    ref = Path(context_ref)
    if ref.is_absolute() and ref.exists():
        return ref
    # 운영: 항상 data/ 기준
    data_p = _DATA / context_ref
    if data_p.exists():
        return data_p
    root = archive_root(journal_path)
    name = Path(context_ref).name
    day = Path(context_ref).parent.name
    alt = root / day / name
    if alt.exists():
        return alt
    base = Path(journal_path).resolve().parent
    p = base / context_ref
    if p.exists():
        return p
    return alt


def load_context(journal_path: str | Path, context_ref: str,
                 expected_sha256: str | None = None) -> str:
    path = resolve_ref(journal_path, context_ref)
    raw = gzip.decompress(path.read_bytes())
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and sha != expected_sha256:
        raise ValueError(f"context sha mismatch {path}: {sha[:12]} != {expected_sha256[:12]}")
    return raw.decode("utf-8")


def parse_context(context_json: str) -> dict[str, Any]:
    try:
        obj = json.loads(context_json)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def strip_track_record(context_json: str) -> str:
    ctx = parse_context(context_json)
    ctx.pop("track_record", None)
    for c in ctx.get("candidates") or ctx.get("universe") or []:
        if isinstance(c, dict):
            c.pop("past_trades", None)
    return json.dumps(ctx, ensure_ascii=False, indent=2)
