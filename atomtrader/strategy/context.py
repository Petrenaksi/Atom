"""Inputs shared by scoring, entry strategies and the exit engine."""

from __future__ import annotations

from dataclasses import dataclass, field

from atomtrader.analysis.indicators import TechnicalSnapshot
from atomtrader.marketdata.models import Candle
from atomtrader.narrative.engine import Narrative, TokenLink
from atomtrader.narrative.rotation import RotationAlert
from atomtrader.scanner.features import TokenFeatures
from atomtrader.scanner.moonshot import MoonshotScore
from atomtrader.security.insider import InsiderRisk
from atomtrader.security.models import SecurityReport


@dataclass(frozen=True)
class MarketContext:
    features: TokenFeatures
    candles_1m: tuple[Candle, ...] = ()
    candles_5m: tuple[Candle, ...] = ()
    moonshot: MoonshotScore | None = None
    security: SecurityReport | None = None
    insider: InsiderRisk | None = None
    narrative: Narrative | None = None
    narrative_link: TokenLink | None = None
    narrative_strength: float | None = None  # narrative strength x link strength
    rotation: RotationAlert | None = None
    round_trip_cost_bps: float | None = None  # exact pre-trade estimate (Jupiter) or model
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def ta1(self) -> TechnicalSnapshot | None:
        return self.features.ta_1m

    @property
    def ta5(self) -> TechnicalSnapshot | None:
        return self.features.ta_5m

    @property
    def price(self) -> float:
        return self.features.price_usd
