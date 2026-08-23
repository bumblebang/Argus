"""scripts/screen.py — 얇은 CLI 가 universe_roll.core_refresh 경로로 동작하는지.

AST 컴파일(임포트 가능) + --dry 실행 exit 0(합성, 실네트워크 없음). --dry 는 라이브
universe.yaml 을 건드리지 않고 전용 파일에 쓴다.
"""
import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCREEN = ROOT / "scripts" / "screen.py"


def test_screen_ast_compiles():
    ast.parse(SCREEN.read_text(encoding="utf-8"))


def test_screen_dry_exits_zero(tmp_path, monkeypatch):
    # scripts 를 임포트 경로에 넣고 screen 모듈을 로드해 main() 을 --dry 로 호출.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    screen = importlib.import_module("screen")
    import src.universe_roll as UR

    dry_out = tmp_path / "universe.dry.yaml"
    monkeypatch.setattr(screen, "OUT_DRY", dry_out)
    monkeypatch.setattr(sys, "argv", ["screen.py", "--dry", "--count", "6"])
    rc = screen.main()
    assert rc == 0
    # 라이브 유니버스 경로(UR.OUT)는 복원됐고, dry 파일에 유효 yaml 이 쓰였다.
    assert UR.OUT.name == "universe.yaml"          # 원복됨(finally)
    assert UR._DRY is False                        # 원복됨(finally)
    data = yaml.safe_load(dry_out.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("KR")
    for it in data["KR"]:
        assert it["layer"] == "core"
