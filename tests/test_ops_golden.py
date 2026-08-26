"""Gate G0 — 운영 계약 골든 (구조 리팩터 전 고정).

돈의 경로·외부 경로·진입점·research import 금지를 잠근다.
실패 = 하우스킵/경로 이동이 운영(ArgusWatch·브릿지·트레이)을 깨뜨릴 수 있음.
"""
from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "golden" / "ops_path_manifest.json"

pytestmark = pytest.mark.ops_golden


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _cfg_get(raw: dict, dotted: str):
    cur: object = raw
    for part in dotted.split("."):
        assert isinstance(cur, dict), f"config path broken at {part!r} in {dotted}"
        cur = cur.get(part)
    return cur


# ── G0-A. 돈의 경로 불변 ───────────────────────────────────────────────

class _DummyClient:
    """live_client 주입용 — place_order 호출되면 안 되는 케이스용 스텁."""

    def place_order(self, **kw):
        raise AssertionError("live place_order must not run in this golden case")


@pytest.mark.parametrize(
    "mode,dry,inject,expect",
    [
        ("live", False, False, "paper"),   # live_client 미주입 → PAPER
        ("live", False, True, "live"),     # 주입 + dry false → LIVE
        ("live", True, True, "paper"),     # dry → PAPER
        ("paper", False, True, "paper"),   # mode paper → PAPER
    ],
    ids=[
        "live_no_client_paper",
        "live_injected_live",
        "dry_forces_paper",
        "mode_paper_stays_paper",
    ],
)
def test_g0a_execution_mode_table(monkeypatch, tmp_path, mode, dry, inject, expect):
    monkeypatch.chdir(tmp_path)
    from src.config import load_config
    from src.agents.pipeline import build_paper_core

    cfg = load_config()
    cfg.raw["broker"] = {"mode": mode, "account_seq": 1, "live_markets": ["KR"]}
    cfg.dry_run = dry
    client = _DummyClient() if inject else None
    broker, _ = build_paper_core(cfg, live_client=client)
    assert broker.mode == expect
    if expect == "paper":
        assert broker.client is None
    else:
        assert broker.client is client


def test_g0a_halt_blocks_live_order(tmp_path):
    from src.broker import Broker
    from src.paper_account import PaperAccount
    from src.risk_gate import RiskGate, Order

    halt = tmp_path / "HALT"
    halt.write_text("halt", encoding="utf-8")
    acct = PaperAccount(cash={"KR": 1_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({
        "capital": {"KR": 1_000_000},
        "max_position_pct": 0.5,
        "max_positions": 5,
        "max_order_notional": {"KR": 500_000},
        "kill_switch_file": str(halt),
    })
    client = _DummyClient()
    b = Broker(account=acct, gate=gate, client=client, mode="live",
               account_seq=1, live_markets=["KR"])
    result = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "g0")
    assert result.ok is False
    assert "HALT" in (result.reject_reason or "") or "킬" in (result.reject_reason or "")


def test_g0a_pipeline_public_api_importable():
    """구 import 경로 — shim 기간에도 깨지면 안 됨."""
    from src.agents.pipeline import CycleRunner, build_paper_core, build_live_llm
    from src.agents.wiring import resolve_execution_mode
    assert callable(build_paper_core)
    assert callable(build_live_llm)
    assert CycleRunner is not None
    assert resolve_execution_mode(broker_mode="live", dry_run=True, live_client=object()) == "paper"
    assert resolve_execution_mode(broker_mode="live", dry_run=False, live_client=object()) == "live"
    assert resolve_execution_mode(broker_mode="live", dry_run=False, live_client=None) == "paper"


# ── G0-B. 외부 경로 계약 ───────────────────────────────────────────────

def test_g0b_manifest_matches_config_example():
    man = _manifest()
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    for dotted, expected in man["config_example_keys"].items():
        assert _cfg_get(raw, dotted) == expected, dotted


def test_g0b_code_defaults_match_manifest():
    import inspect

    man = _manifest()["paths"]
    from src import paths as pathmod
    from src.agents.llm import FileInboxLLM, bridge_heartbeat_path
    from src.agents.pipeline import DATA
    from src.engine.store import Store
    from src.paper_account import PaperAccount
    from src.risk_gate import RiskGate
    from src.engine.loop import WatchConfig
    from src.engine import brain_mode as bm

    # 기본 생성은 resolve → 레거시(미이동) 절대경로
    assert FileInboxLLM().inbox_dir == pathmod.resolve("inbox")
    assert bridge_heartbeat_path(man["inbox"]) == Path(man["bridge_hb"])
    assert (DATA / "llm_inbox").as_posix().endswith(man["inbox"])

    pa_sig = inspect.signature(PaperAccount.__init__)
    assert str(pa_sig.parameters["state_path"].default) == man["paper"]
    store_sig = inspect.signature(Store.__init__)
    assert str(store_sig.parameters["path"].default) == man["db"]

    gate = RiskGate({})
    assert Path(gate.kill_switch_file).as_posix() == man["halt"]

    wc = WatchConfig()
    assert wc.wake_request_path == man["wake_request"]
    assert bm.DEFAULT_PATH_NAME == Path(man["brain_mode"]).name

def test_g0b_dashboard_legacy_paths():
    man = _manifest()["paths"]
    dash = (ROOT / "scripts" / "dashboard.py").read_text(encoding="utf-8")
    for key in ("db", "watch_hb", "paper", "watch_pid"):
        rel = man[key]
        # Phase 2+: paths.resolve(..., configured="data/…") 또는 "data" / "bot.db" 조각
        ok = (rel in dash) or all(f'"{p}"' in dash for p in rel.split("/"))
        assert ok, f"dashboard missing {rel}"
        assert "paths" in dash or "_paths" in dash


def test_g0b_bridge_tick_inbox_default():
    man = _manifest()["paths"]
    src = (ROOT / "scripts" / "bridge_tick.py").read_text(encoding="utf-8")
    assert 'llm_inbox' in src
    assert Path(man["bridge_script"]).as_posix() == "scripts/bridge_tick.py"
    assert (ROOT / man["bridge_script"]).is_file()


def test_g0b_poketokenbar_hardcoded_paths_if_present():
    """형제 PokeTokenBarWin 이 워크스페이스에 있으면 하드코딩 경로 일치 검사."""
    man = _manifest()["paths"]
    sibling = ROOT.parent / "PokeTokenBarWin" / "src" / "poketokenbar_win" / "argus_bridge.py"
    if not sibling.is_file():
        pytest.skip("PokeTokenBarWin not beside argus")
    text = sibling.read_text(encoding="utf-8")
    assert 'Path("data") / "llm_inbox" / "bridge.heartbeat"' in text or (
        '"llm_inbox"' in text and "bridge.heartbeat" in text
    )
    assert "brain_mode.json" in text
    assert "bridge_tick.py" in text
    for needle in (man["inbox"].split("/")[-1], "bridge.heartbeat", "brain_mode.json"):
        assert needle in text


def test_g0b_inbox_heartbeat_roundtrip(tmp_path):
    from src.agents.llm import write_bridge_heartbeat, is_bridge_armed

    inbox = tmp_path / "llm_inbox"
    write_bridge_heartbeat(inbox, now=1_000_000.0)
    assert (inbox / "bridge.heartbeat").is_file()
    assert is_bridge_armed(inbox, max_age_sec=90.0, now=1_000_050.0)
    assert not is_bridge_armed(inbox, max_age_sec=90.0, now=1_000_200.0)


# ── G0-C. 진입점 계약 ─────────────────────────────────────────────────

def test_g0c_main_stub_exits_2(capsys):
    ns = runpy.run_path(str(ROOT / "main.py"), run_name="not_main")
    assert ns["main"]() == 2
    err = capsys.readouterr().err
    assert "argus watch" in err or "scripts/watch.py" in err


def test_g0c_entry_scripts_exist():
    for rel in (
        "scripts/watch.py",
        "scripts/doctor.py",
        "scripts/bootstrap.py",
        "scripts/bridge_tick.py",
        "scripts/agent_cycle.py",
    ):
        assert (ROOT / rel).is_file(), rel


# ── G0-D. import / 레이어 가드 ─────────────────────────────────────────

def _iter_py_files(base: Path):
    for p in base.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "research"} for part in p.parts):
            continue
        yield p


def test_g0d_runtime_src_does_not_import_research():
    bad: list[str] = []
    src_root = ROOT / "src"
    for path in _iter_py_files(src_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "research" or alias.name.startswith("research."):
                        bad.append(f"{path.relative_to(ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "research" or mod.startswith("research."):
                    bad.append(f"{path.relative_to(ROOT)}: from {mod}")
    assert bad == [], "runtime must not import research:\n" + "\n".join(bad)


def test_g0d_scripts_tree_no_research_import():
    bad: list[str] = []
    for path in _iter_py_files(ROOT / "scripts"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "research" or alias.name.startswith("research."):
                        bad.append(str(path.relative_to(ROOT)))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "research" or mod.startswith("research."):
                    bad.append(str(path.relative_to(ROOT)))
    assert bad == []
