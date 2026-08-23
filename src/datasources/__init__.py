"""데이터소스 어댑터: 각자 부분 market state dict 를 생산한다."""
from .base import DataSource, SourceContext
from .toss_info import TossInfoSource
from .breadth import BreadthSource
from .edgar import EdgarSource
from .dart import DartSource
from .sector import SectorSource
from .news import NewsSource, DartNewsSource
from .sentiment import SentimentSource
from .markets import MarketsSource
from .flows import FlowsSource
from .fred import MacroSource
from .ecos import EcosMacroSource
from .finnhub import FinnhubNewsSource
from .flows_market import FlowsMarketSource
from .positioning import PositioningSource
from .program_flows import ProgramFlowsSource
from .krx_flows import KrxFlowsMarketSource, KrxFlowsSource
from .foreign_exhaustion import ForeignExhaustionSource
from .krx_alerts import KrxAlertsSource
from .krx_market import VkospiSource, KrxBreadthSource, IndexConstituentsSource
from .fear_greed import (fetch_cnn_fear_greed, kr_fear_proxy, rating_of,
                         assess as assess_fear)

__all__ = ["DataSource", "SourceContext", "TossInfoSource", "BreadthSource",
           "EdgarSource", "DartSource", "SectorSource", "NewsSource",
           "DartNewsSource", "SentimentSource", "MarketsSource", "FlowsSource",
           "FlowsMarketSource", "PositioningSource", "MacroSource", "EcosMacroSource",
           "FinnhubNewsSource",
           "ProgramFlowsSource", "KrxFlowsMarketSource", "KrxFlowsSource",
           "ForeignExhaustionSource", "KrxAlertsSource",
           "VkospiSource", "KrxBreadthSource", "IndexConstituentsSource",
           "fetch_cnn_fear_greed", "kr_fear_proxy", "rating_of", "assess_fear"]
