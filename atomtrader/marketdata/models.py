"""Normalized market-data records, independent of any provider.

Unavailable fields are ``None`` — never zero, never estimated. In particular
``WindowStats.buyers``/``sellers`` (unique wallets) are None for sources that only report
transaction counts (DexScreener).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from atomtrader.core.models import Chain, TokenRef

WINDOWS = ("m5", "m15", "m30", "h1", "h6", "h24")


@dataclass(frozen=True, slots=True)
class WindowStats:
    buys: int | None = None
    sells: int | None = None
    buyers: int | None = None  # unique wallets; None if the source doesn't report it
    sellers: int | None = None
    volume_usd: Decimal | None = None
    price_change_pct: Decimal | None = None

    @property
    def buy_sell_ratio(self) -> Decimal | None:
        if self.buys is None or self.sells is None or self.sells == 0:
            return None
        return Decimal(self.buys) / Decimal(self.sells)


@dataclass(frozen=True, slots=True)
class PairSnapshot:
    chain: Chain
    dex: str
    pair_address: str
    base: TokenRef
    quote_symbol: str
    quote_address: str
    price_usd: Decimal
    liquidity_usd: Decimal | None
    fdv_usd: Decimal | None
    market_cap_usd: Decimal | None
    pair_created_at: datetime | None
    as_of: datetime
    source: str
    windows: dict[str, WindowStats] = field(default_factory=dict)

    def window(self, name: str) -> WindowStats:
        return self.windows.get(name, WindowStats())


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLCV bar. ``ts`` is the bar's open time (UTC). Prices in USD."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_usd: Decimal

    def __post_init__(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"inconsistent candle at {self.ts.isoformat()}")


@dataclass(frozen=True, slots=True)
class AttentionSignal:
    """A DexScreener profile/boost listing. Boosts are *paid promotion* — evidence of
    marketing spend, not organic attention."""

    chain_id: str
    token_address: str
    kind: str  # "profile" | "boost"
    boost_amount: Decimal | None
    links: tuple[str, ...]
    as_of: datetime
