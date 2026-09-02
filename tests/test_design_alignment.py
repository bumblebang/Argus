"""design_alignment — manifest 기반 설계–구현 점검."""
from __future__ import annotations

from pathlib import Path

import yaml

from src.eval.design_alignment import alignment_ok, resolve_config_path, run_alignment


def test_design_invariants_pass_on_repo(tmp_path):
    """저장소 config.example + golden manifest — error급 전부 통과(waive 제외)."""
    root = Path(__file__).resolve().parents[1]
    results = run_alignment(root=root, config_path=root / "config.example.yaml")
    hard_fails = [r for r in results if not r.ok and not r.waived]
    assert not hard_fails, "\n".join(f"{r.check_id}: {r.detail}" for r in hard_fails)


def test_resolve_config_prefers_live_yaml(tmp_path):
    root = tmp_path
    (root / "config.example.yaml").write_text("x: 1\n", encoding="utf-8")
    assert resolve_config_path(root).name == "config.example.yaml"
    (root / "config.yaml").write_text("x: 2\n", encoding="utf-8")
    assert resolve_config_path(root).name == "config.yaml"


def test_config_eq_detects_mismatch(tmp_path):
    root = tmp_path
    cfg = {"screener": {"liquidity_core": {"core_size": 18}}}
    (root / "config.example.yaml").write_text(yaml.dump(cfg), encoding="utf-8")
    manifest = {
        "groups": {
            "t": {
                "checks": [{
                    "id": "size",
                    "type": "config_eq",
                    "path": ["screener", "liquidity_core", "core_size"],
                    "value": 100,
                }]
            }
        }
    }
    man = root / "m.yaml"
    man.write_text(yaml.dump(manifest), encoding="utf-8")
    results = run_alignment(man, root / "config.example.yaml", root)
    assert not alignment_ok(results)


def test_waive_allows_known_gap(tmp_path):
    root = tmp_path
    (root / "a.txt").write_text("bad needle here", encoding="utf-8")
    manifest = {
        "groups": {
            "t": {
                "checks": [{
                    "id": "gap",
                    "type": "file_not_contains",
                    "path": "a.txt",
                    "needle": "bad needle",
                    "waive": True,
                    "waive_reason": "test",
                    "waive_until": "2099-12-31",
                }]
            }
        }
    }
    man = root / "m.yaml"
    man.write_text(yaml.dump(manifest), encoding="utf-8")
    (root / "config.example.yaml").write_text("{}", encoding="utf-8")
    results = run_alignment(man, root / "config.example.yaml", root)
    assert alignment_ok(results)
    assert results[0].waived


def test_waive_without_until_is_not_waived(tmp_path):
    root = tmp_path
    (root / "a.txt").write_text("bad needle here", encoding="utf-8")
    manifest = {
        "groups": {
            "t": {
                "checks": [{
                    "id": "gap",
                    "type": "file_not_contains",
                    "path": "a.txt",
                    "needle": "bad needle",
                    "waive": True,
                    "waive_reason": "test",
                }]
            }
        }
    }
    man = root / "m.yaml"
    man.write_text(yaml.dump(manifest), encoding="utf-8")
    (root / "config.example.yaml").write_text("{}", encoding="utf-8")
    results = run_alignment(man, root / "config.example.yaml", root)
    assert not alignment_ok(results)
    assert not results[0].waived
