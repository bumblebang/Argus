"""Phase 2 — paths dual-resolve + migrate dry-run (물리 이동 없음)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "golden" / "ops_path_manifest.json"

pytestmark = pytest.mark.ops_golden


def test_p2_layout_matches_manifest():
    from src import paths

    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert paths.LAYOUT == man["layout"]
    assert paths.CANONICAL == man["paths"]


def test_p2_resolve_prefers_layout_when_present(tmp_path):
    from src import paths

    legacy = tmp_path / "data" / "bot.db"
    layout = tmp_path / "data" / "state" / "bot.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old", encoding="utf-8")
    assert paths.resolve("db", root=tmp_path) == legacy.resolve()

    layout.parent.mkdir(parents=True)
    layout.write_text("new", encoding="utf-8")
    assert paths.resolve("db", root=tmp_path) == layout.resolve()


def test_p2_resolve_skips_empty_layout_for_populated_legacy(tmp_path):
    """빈 LAYOUT 파일이 찬 레거시를 가리지 않는다 (부분 컷오버 사고 방지)."""
    from src import paths

    legacy = tmp_path / "data" / "bot.db"
    layout = tmp_path / "data" / "state" / "bot.db"
    legacy.parent.mkdir(parents=True)
    layout.parent.mkdir(parents=True)
    legacy.write_bytes(b"real-db-bytes")
    layout.write_bytes(b"")  # touch 된 빈 파일
    assert paths.resolve("db", root=tmp_path) == legacy.resolve()


def test_p2_resolve_skips_empty_inbox_dir_for_populated_legacy(tmp_path):
    """빈 data/inbox 가 찬 llm_inbox 를 가리지 않는다."""
    from src import paths

    leg = tmp_path / "data" / "llm_inbox"
    neu = tmp_path / "data" / "inbox"
    leg.mkdir(parents=True)
    (leg / "bridge.heartbeat").write_text("{}", encoding="utf-8")
    neu.mkdir(parents=True)  # empty
    assert paths.resolve("inbox", root=tmp_path) == leg.resolve()


def test_p2_resolve_configured_wins_if_exists(tmp_path):
    from src import paths

    custom = tmp_path / "custom" / "halt.txt"
    custom.parent.mkdir(parents=True)
    custom.write_text("x", encoding="utf-8")
    (tmp_path / "data" / "state").mkdir(parents=True)
    (tmp_path / "data" / "state" / "HALT").write_text("y", encoding="utf-8")
    got = paths.resolve("halt", root=tmp_path, configured="custom/halt.txt")
    assert got == custom.resolve()


def test_p2_inbox_legacy_or_layout(tmp_path):
    from src import paths

    leg = tmp_path / "data" / "llm_inbox"
    leg.mkdir(parents=True)
    assert paths.resolve("inbox", root=tmp_path) == leg.resolve()
    neu = tmp_path / "data" / "inbox"
    neu.mkdir(parents=True)
    assert paths.resolve("inbox", root=tmp_path) == neu.resolve()


def test_p2_migrate_dry_run(tmp_path):
    from src.paths_migrate import apply_moves, plan_moves

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bot.db").write_bytes(b"db")
    (tmp_path / "data" / "llm_inbox").mkdir()
    (tmp_path / "data" / "llm_inbox" / "bridge.heartbeat").write_text("{}", encoding="utf-8")

    plan = plan_moves(root=tmp_path)
    by_src = {r["src"]: r["action"] for r in plan}
    assert by_src["data/bot.db"] == "move"
    assert by_src["data/llm_inbox"] == "move_inbox"

    rows = apply_moves(root=tmp_path, dry_run=True)
    assert (tmp_path / "data" / "bot.db").is_file()  # unchanged
    assert any(r.get("result", "").startswith("dry:") for r in rows)


def test_p2_migrate_apply_moves_files(tmp_path):
    from src.paths_migrate import apply_moves

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bot.db").write_bytes(b"db")
    (tmp_path / "data" / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    inbox = tmp_path / "data" / "llm_inbox"
    inbox.mkdir()
    (inbox / "bridge.heartbeat").write_text("{}", encoding="utf-8")

    rows = apply_moves(root=tmp_path, dry_run=False)
    assert (tmp_path / "data" / "state" / "bot.db").is_file()
    assert not (tmp_path / "data" / "bot.db").exists()
    assert (tmp_path / "data" / "ledgers" / "decisions.jsonl").is_file()
    assert (tmp_path / "data" / "inbox" / "bridge.heartbeat").is_file()
    # 레거시 llm_inbox 별칭이 신 inbox 를 가리켜야 함
    legacy = tmp_path / "data" / "llm_inbox"
    assert legacy.exists()
    assert (legacy / "bridge.heartbeat").is_file()
    assert any(r.get("result") in ("moved", "moved+alias") for r in rows)


def test_p2_halt_gate_finds_layout(tmp_path):
    from src.broker import Broker
    from src.paper_account import PaperAccount
    from src.risk_gate import RiskGate, Order

    halt = tmp_path / "data" / "state" / "HALT"
    halt.parent.mkdir(parents=True)
    halt.write_text("halt", encoding="utf-8")
    # 레거시 경로 문자열을 config 로 넘겨도 state/HALT 를 찾는다
    acct = PaperAccount(cash={"KR": 1_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({
        "capital": {"KR": 1_000_000},
        "max_position_pct": 0.5,
        "max_positions": 5,
        "max_order_notional": {"KR": 500_000},
        "kill_switch_file": "data/HALT",
    })
    # ROOT 가 레포라서 tmp HALT 를 못 봄 — configured 를 절대경로로
    gate.kill_switch_file = str(halt)
    from src import paths as pathmod
    assert pathmod.resolve("halt", configured=str(halt), root=tmp_path).exists()

    class _Dummy:
        def place_order(self, **kw):
            raise AssertionError("no order")

    b = Broker(account=acct, gate=gate, client=_Dummy(), mode="live",
               account_seq=1, live_markets=["KR"])
    # RiskGate 는 configured 절대경로 exists 체크
    result = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "p2")
    assert result.ok is False
