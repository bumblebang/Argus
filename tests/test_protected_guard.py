"""J13 PROTECTED 변경 가드."""
import json

import yaml

from src.eval.protected_guard import (
    check_protected_changes,
    config_touches,
    is_defect_fix,
    parse_experiment_id,
    touches_for_paths,
)
from src.eval_protocol import (
    experiment_evidence_n,
    register_experiment,
    can_promote,
    record_experiment_evidence,
)


def test_touches_for_paths():
    t = touches_for_paths(["src/risk_gate.py", "README.md"])
    assert t == {"risk_gate"}


def test_config_touches_risk_block():
    old = {"risk": {"max_position_pct": 0.2}, "exit_policy": {"enabled": True}}
    new = {"risk": {"max_position_pct": 0.25}, "exit_policy": {"enabled": True}}
    assert config_touches(old, new) == {"risk_gate"}


def test_parse_experiment_id():
    body = "요약\n\neval_experiment: exp_123_0\n"
    assert parse_experiment_id(body) == "exp_123_0"


def test_is_defect_fix():
    assert is_defect_fix("defect-fix: 손절 NaN 통과")
    assert not is_defect_fix("게이트 완화 실험")


def test_can_promote_with_guard_registry(tmp_path):
    reg = tmp_path / "reg.json"
    exp = register_experiment(
        name="t", hypothesis="h", metric="m", min_n=10,
        touches=["risk_gate"], path=reg)
    data = json.loads(reg.read_text(encoding="utf-8"))
    data["experiments"][0]["status"] = "pass"
    reg.write_text(json.dumps(data), encoding="utf-8")
    ok, _ = can_promote(
        change="risk_gate", evidence_n=20,
        experiment_id=exp["id"], registry_path=reg)
    assert ok is True


def test_config_touches_agents():
    old = yaml.safe_load("agents:\n  min_conviction: 0.6\n")
    new = yaml.safe_load("agents:\n  min_conviction: 0.7\n")
    assert config_touches(old, new) == {"validation_rules"}


def test_experiment_evidence_n_from_registry(tmp_path):
    reg = tmp_path / "reg.json"
    exp = register_experiment(
        name="t", hypothesis="h", metric="m", min_n=10,
        touches=["risk_gate"], path=reg)
    assert experiment_evidence_n(exp) is None
    record_experiment_evidence(experiment_id=exp["id"], n=25, path=reg)
    import json
    saved = json.loads(reg.read_text(encoding="utf-8"))["experiments"][0]
    assert experiment_evidence_n(saved) == 25


def test_check_protected_requires_registry_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.eval.protected_guard.git_changed_files",
        lambda base, head="HEAD": ["src/risk_gate.py"],
    )
    reg = tmp_path / "reg.json"
    exp = register_experiment(
        name="t", hypothesis="h", metric="m", min_n=10,
        touches=["risk_gate"], path=reg)
    import json
    data = json.loads(reg.read_text(encoding="utf-8"))
    data["experiments"][0]["status"] = "pass"
    reg.write_text(json.dumps(data), encoding="utf-8")

    ok, msgs = check_protected_changes(
        base_ref="main", registry_path=reg, experiment_id=exp["id"],
        pr_body=f"eval_experiment: {exp['id']}",
    )
    assert ok is False
    assert any("표본 n 미기록" in m for m in msgs)

    record_experiment_evidence(experiment_id=exp["id"], n=25, path=reg)
    ok2, _ = check_protected_changes(
        base_ref="main", registry_path=reg, experiment_id=exp["id"],
        pr_body=f"eval_experiment: {exp['id']}",
    )
    assert ok2 is True
