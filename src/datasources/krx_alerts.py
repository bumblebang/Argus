"""KRX 시장경보·관리종목 → market_state.warnings + blocked 파일.

Toss stock_info 경고와 병행. risk_gate 는 blocked_symbols 로 BUY 하드스킵.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .base import DataSource, SourceContext
from .krx_client import bld_for, connect, ymd
from ..logging_setup import get_logger

log = get_logger("src.krx_alerts")

_BLOCKED_PATH = Path(__file__).resolve().parents[2] / "data" / "krx_blocked_symbols.json"

# 하드스킵에 쓰는 코드(Toss BLOCKED_WARNING_TYPES 와 정렬)
ALERT_CODE_MAP = {
    "주의": "INVESTMENT_WARNING",
    "경고": "INVESTMENT_WARNING",
    "위험": "INVESTMENT_RISK",
    "과열": "OVERHEATED",
    "관리": "ADMIN_ISSUE",
    "정지": "TRADING_HALT",
    "투자주의": "INVESTMENT_WARNING",
    "투자경고": "INVESTMENT_WARNING",
    "투자위험": "INVESTMENT_RISK",
}

HARD_BLOCK = frozenset({
    "INVESTMENT_WARNING", "INVESTMENT_RISK", "OVERHEATED",
    "ADMIN_ISSUE", "TRADING_HALT", "LIQUIDATION_TRADING",
})


def _sym_of(row: dict) -> str | None:
    s = (row.get("ISU_SRT_CD") or row.get("ISU_CD") or row.get("isuSrtCd") or "")
    s = str(s).strip()
    if len(s) > 6:
        s = s[-6:]
    return s or None


def parse_alert_row(row: dict, default_code: str) -> tuple[str, str] | None:
    sym = _sym_of(row)
    if not sym:
        return None
    label = str(row.get("ISU_ABBRV") or row.get("WRN_TP_NM") or row.get("ISSUE_NM")
                or row.get("TYPO_NM") or "")
    code = default_code
    for k, v in ALERT_CODE_MAP.items():
        if k in label:
            code = v
            break
    explicit = row.get("WRN_TP_CD") or row.get("warningType")
    if explicit:
        code = ALERT_CODE_MAP.get(str(explicit), str(explicit))
    return sym, code


def load_blocked_symbols(path: Path | None = None) -> set[str]:
    p = path or _BLOCKED_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        syms = raw.get("symbols") if isinstance(raw, dict) else raw
        return {str(s) for s in (syms or []) if s}
    except (OSError, ValueError, TypeError):
        return set()


def save_blocked_symbols(symbols: set[str], path: Path | None = None) -> None:
    p = path or _BLOCKED_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "symbols": sorted(symbols),
        "note": "KRX 경보·관리 — risk_gate BUY 하드스킵",
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


class KrxAlertsSource(DataSource):
    name = "krx_alerts"

    def __init__(self, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None,
                 blocked_path: Path | None = None):
        self.spacing = spacing_sec
        self.user = user
        self.password = password
        self.blocked_path = Path(blocked_path) if blocked_path else _BLOCKED_PATH

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            warnings = {"999999": ["INVESTMENT_RISK"]}
            save_blocked_symbols({"999999"}, self.blocked_path)
            return {"warnings": warnings}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {"warnings": {}}
        day = ymd()
        specs = [
            ("market_alert_주의", "INVESTMENT_WARNING"),
            ("market_alert_경고", "INVESTMENT_WARNING"),
        ]
        warnings: dict[str, list[str]] = {}
        for eid, default in specs:
            bld = bld_for(eid)
            if not bld:
                continue
            for mid in ("STK", "KSQ", "ALL"):
                rows = client.get_rows(bld, mktId=mid, trdDd=day, share="1")
                for r in rows:
                    parsed = parse_alert_row(r, default)
                    if not parsed:
                        continue
                    sym, code = parsed
                    warnings.setdefault(sym, [])
                    if code not in warnings[sym]:
                        warnings[sym].append(code)
        blocked = {s for s, codes in warnings.items()
                   if any(c in HARD_BLOCK for c in codes)}
        try:
            save_blocked_symbols(blocked, self.blocked_path)
        except OSError as e:
            log.warning("blocked symbols 저장 실패: %s", e)
        log.info("krx_alerts: warnings=%d blocked=%d", len(warnings), len(blocked))
        return {"warnings": warnings}


__all__ = ["KrxAlertsSource", "parse_alert_row", "load_blocked_symbols",
           "save_blocked_symbols", "HARD_BLOCK", "ALERT_CODE_MAP"]
