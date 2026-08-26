"""LLM 사이클 빌더 — 하위호환 shim (Phase 1).

정본: CycleRunner → cycle_runner, 배선 헬퍼 → wiring.
구 import `from src.agents.pipeline import CycleRunner, build_paper_core, …` 유지.
"""
from __future__ import annotations

from .cycle_runner import CycleRunner
from .wiring import (
    DATA,
    LLMFactory,
    FetchCandles,
    resolve_execution_mode,
    sector_map_from_universe,
    build_paper_core,
    select_backend,
    synth_candles,
    dry_llm_factory,
    earnings_near,
    resolve_strategy,
    entry_stop_target,
    position_plan,
    build_cursor_bridge,
    build_live_llm,
)

__all__ = [
    "CycleRunner",
    "DATA",
    "LLMFactory",
    "FetchCandles",
    "resolve_execution_mode",
    "sector_map_from_universe",
    "build_paper_core",
    "select_backend",
    "synth_candles",
    "dry_llm_factory",
    "earnings_near",
    "resolve_strategy",
    "entry_stop_target",
    "position_plan",
    "build_cursor_bridge",
    "build_live_llm",
]
