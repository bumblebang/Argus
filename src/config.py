"""설정 로딩: .env(시크릿) + config.yaml(전략/리스크 파라미터)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def resolve_universe(raw: dict, data_dir: Path, now: float | None = None) -> dict:
    """매매 유니버스 결정: screener.enabled 면 신선한 data/universe.yaml(동적 발굴 결과)을,
    아니면 config.yaml 의 정적 universe 블록을 쓴다.

    안전장치: universe.yaml 이 너무 오래됐으면(기본 24h) 무시하고 정적으로 폴백 —
    스크리너가 멈춰도 낡은 유니버스로 매매하지 않게.
    """
    static = raw.get("universe", {}) or {}
    sc = raw.get("screener", {}) or {}
    if not sc.get("enabled"):
        return static
    p = Path(data_dir) / "universe.yaml"
    if not p.exists():
        return static
    max_age_h = float(sc.get("universe_max_age_hours", 24))
    age_h = ((now or time.time()) - p.stat().st_mtime) / 3600.0
    if age_h > max_age_h:
        return static
    try:
        dyn = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return static
    if not (isinstance(dyn, dict) and dyn):
        return static
    # ETF/ETN 등 매수 불가 유형은 로드 시점에 제외(도씨에·뇌 후보 토큰 낭비 방지).
    # 브로커 check_tradable 과 동일 집합 — 최종 가드는 그대로 유지.
    from .security_filter import filter_universe
    return filter_universe(dyn, log_drops=False)


@dataclass
class TossCredentials:
    base_url: str
    client_id: str
    client_secret: str
    account_no: str

    @classmethod
    def from_env(cls) -> "TossCredentials":
        return cls(
            base_url=os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com").rstrip("/"),
            client_id=os.getenv("TOSS_CLIENT_ID", ""),
            client_secret=os.getenv("TOSS_CLIENT_SECRET", ""),
            account_no=os.getenv("TOSS_ACCOUNT_NO", ""),
        )

    def validate(self) -> list[str]:
        missing = [k for k, v in {
            "TOSS_CLIENT_ID": self.client_id,
            "TOSS_CLIENT_SECRET": self.client_secret,
            "TOSS_ACCOUNT_NO": self.account_no,
        }.items() if not v]
        return missing


@dataclass
class AppConfig:
    raw: dict
    creds: TossCredentials
    dry_run: bool

    @property
    def run(self) -> dict:
        return self.raw.get("run", {})

    @property
    def risk(self) -> dict:
        return self.raw.get("risk", {})

    @property
    def universe(self) -> dict:
        return self.raw.get("universe", {})

    @property
    def strategies(self) -> dict:
        return self.raw.get("strategies", {})


def default_config_path() -> Path:
    """운영은 config.yaml. 테스트는 ARGUS_CONFIG 또는 example (실계좌 설정 격리)."""
    env = (os.getenv("ARGUS_CONFIG") or "").strip()
    if env:
        return Path(env)
    if os.getenv("PYTEST_CURRENT_TEST") and (ROOT / "config.example.yaml").exists():
        return ROOT / "config.example.yaml"
    return ROOT / "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    path = Path(path) if path is not None else default_config_path()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # Phase 1: watcher(구) ↔ disclosure/events(신) 호환 — 운영 yaml 즉시 rename 강제 금지.
    raw = _normalize_watcher_keys(raw)
    # 동적 유니버스(스크리너 결과)가 있으면 정적 블록 대신 사용(없거나 낡으면 정적 폴백).
    # 테스트는 ARGUS_DISABLE_DYNAMIC_UNIVERSE=1 로 정적 유니버스 고정(production 파일 격리).
    if os.getenv("ARGUS_DISABLE_DYNAMIC_UNIVERSE") != "1":
        raw["universe"] = resolve_universe(raw, ROOT / "data")
    creds = TossCredentials.from_env()
    # .env 의 DRY_RUN 과 yaml 의 run.dry_run 중 하나라도 true 면 모의 실행(안전 우선).
    env_dry = os.getenv("DRY_RUN", "true").lower() != "false"
    yaml_dry = bool(raw.get("run", {}).get("dry_run", True))
    return AppConfig(raw=raw, creds=creds, dry_run=env_dry or yaml_dry)


def _normalize_watcher_keys(raw: dict) -> dict:
    """disclosure/events 블록이 있으면 watcher 로 병합(구키 우선 유지).

    코드는 계속 raw['watcher'] 를 읽는다. 신키만 있는 설정도 동작하게 한다.
    """
    if not isinstance(raw, dict):
        return raw
    legacy = raw.get("watcher")
    if not isinstance(legacy, dict):
        legacy = {}
    merged = dict(legacy)
    for alt in ("disclosure", "events"):
        block = raw.get(alt)
        if isinstance(block, dict):
            for k, v in block.items():
                merged.setdefault(k, v)
    if merged:
        raw["watcher"] = merged
    return raw
