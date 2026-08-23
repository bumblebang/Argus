"""MarketState — '뇌'(판단 에이전트)가 읽는 구조화된 시장 스냅샷.

여러 데이터소스가 부분 dict 를 만들어 merge() 로 합친다. data/market_state.json 에 저장.
Phase 1 에서 채워지는 슬롯: fx(환율), regime(시장국면/브레드스), sessions(개장 캘린더),
warnings(투자유의), sectors(주도섹터, 추후), news(뉴스, 추후).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class MarketState:
    asof: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fx: dict = field(default_factory=dict)        # {"USDKRW": 1380.5}
    regime: dict = field(default_factory=dict)    # {"KR": {"label": "risk_on", ...}, "US": {...}}
    sessions: dict = field(default_factory=dict)  # {"KR": {...calendar...}, "US": {...}}
    warnings: dict = field(default_factory=dict)  # {symbol: [codes]}
    fundamentals: dict = field(default_factory=dict)  # {symbol: {revenue, net_income, net_margin}}
    sectors: dict = field(default_factory=dict)   # 주도/소외 섹터
    sentiment: dict = field(default_factory=dict)  # VIX(공포지수) 등 심리·변동성
    markets: dict = field(default_factory=dict)   # 지수·원자재·크립토·환율 레벨
    macro: dict = field(default_factory=dict)     # 미국 매크로(FRED: 금리·금리차·CPI·달러)
    macro_kr: dict = field(default_factory=dict)  # 한국 매크로(ECOS: 기준금리·국고채·물가·고용·심리)
    flows: dict = field(default_factory=dict)     # 국내 수급(외국인/기관/개인) — 종목별
    flows_market: dict = field(default_factory=dict)  # 코스피/코스닥 시장 전체 수급
    positioning: dict = field(default_factory=dict)   # 종목별 신용·공매도
    short_market: dict = field(default_factory=dict)  # 시장 공매도 집계(KRX)
    program_flows: dict = field(default_factory=dict)  # 프로그램매매
    foreign_exhaustion: dict = field(default_factory=dict)  # 외국인 한도·소진
    vkospi: dict = field(default_factory=dict)        # VKOSPI 스칼라
    index_constituents: dict = field(default_factory=dict)  # 지수 구성종목
    news: list = field(default_factory=list)      # 뉴스/공시 헤드라인

    def merge(self, partial: dict) -> "MarketState":
        for k, v in (partial or {}).items():
            cur = getattr(self, k, None)
            if isinstance(cur, dict) and isinstance(v, dict):
                # 1-level deep merge — regime.KR 보강이 기존 breadth 를 지우지 않게
                for kk, vv in v.items():
                    if isinstance(cur.get(kk), dict) and isinstance(vv, dict):
                        cur[kk].update(vv)
                    else:
                        cur[kk] = vv
            elif isinstance(cur, list) and isinstance(v, list):
                cur.extend(v)
            else:
                setattr(self, k, v)
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path = "data/market_state.json") -> None:
        # 원자적 쓰기: 같은 폴더에 *.tmp 를 쓴 뒤 os.replace 로 교체 → 소비자(감시 루프·뇌)가
        # 매번 파일을 다시 읽는데, 장중 갱신기와 배치가 부분갱신할 때 torn-read(반쪽 파일)를 방지.
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def load(cls, path: str | Path = "data/market_state.json") -> "MarketState":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
