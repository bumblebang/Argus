"""공개 배포용 설정·doctor·레거시 진입점."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def test_example_config_parses_as_paper():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert raw["broker"]["mode"] == "paper"
    assert raw["run"]["dry_run"] is True
    assert raw["agents"]["claude_command"] == "claude"
    assert raw["agents"]["cursor_bridge"]["enabled"] is False
    assert "MinBang" not in text
    from src.config import load_config
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.dry_run is True


def test_doctor_paper_example(tmp_path, monkeypatch):
    import doctor as doc
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    (tmp_path / ".env").write_text("DRY_RUN=true\n", encoding="utf-8")
    monkeypatch.setattr(doc, "ROOT", tmp_path)
    monkeypatch.setattr(doc, "public_ip", lambda: "1.2.3.4")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(doc, "probe_toss", lambda cfg: (True, "skip"))
    assert doc.main() == 0


def test_mask_acct_hides_prefix():
    import doctor as doc
    assert doc.mask_acct("12345678901") == "*******8901"
    assert "1234567" not in doc.mask_acct("12345678901")


def test_doctor_live_blocks_on_toss_fail(tmp_path, monkeypatch):
    import doctor as doc
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("mode: \"paper\"", "mode: \"live\"")
    text = text.replace("dry_run: true", "dry_run: false")
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DRY_RUN=false\nTOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n", encoding="utf-8")
    monkeypatch.setattr(doc, "ROOT", tmp_path)
    monkeypatch.setattr(doc, "public_ip", lambda: "1.2.3.4")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TOSS_CLIENT_ID", "x")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "y")
    monkeypatch.setattr(doc, "probe_toss", lambda cfg: (False, "조회 실패"))
    assert doc.main() == 1


def test_bootstrap_does_not_overwrite(tmp_path, monkeypatch):
    import bootstrap as boot
    (tmp_path / "config.example.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("X=\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("keep: true\n", encoding="utf-8")
    monkeypatch.setattr(boot, "ROOT", tmp_path)
    assert boot.main() == 0
    assert "keep: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert (tmp_path / ".env").exists()


def test_load_config_in_pytest_uses_example():
    from src.config import default_config_path, load_config
    assert default_config_path().name == "config.example.yaml"
    cfg = load_config()
    assert cfg.raw["broker"]["mode"] == "paper"
    assert cfg.dry_run is True


def test_doctor_masks_account_no(tmp_path, monkeypatch, capsys):
    import doctor as doc
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    (tmp_path / ".env").write_text("DRY_RUN=true\nTOSS_ACCOUNT_NO=12345678901\n",
                                   encoding="utf-8")
    monkeypatch.setattr(doc, "ROOT", tmp_path)
    monkeypatch.setattr(doc, "public_ip", lambda: "1.2.3.4")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TOSS_ACCOUNT_NO", "12345678901")
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(doc, "probe_toss", lambda cfg: (True, "skip"))
    assert doc.main() == 0
    out = capsys.readouterr().out
    assert "1234567" not in out
    assert "8901" in out


def test_main_does_not_trade(monkeypatch):
    import main as m
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert m.main() == 2
    assert not hasattr(m, "_legacy")
