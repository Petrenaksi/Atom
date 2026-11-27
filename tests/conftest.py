from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atomtrader.core.models import Chain, MarketQuote, TokenRef
from atomtrader.core.money import FxRate, FxTable, MissingMarketData

T0 = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
TOKEN = TokenRef(Chain.SOLANA, "So1anaMemeMint1111111111111111111111111111", "MEME")
OTHER = TokenRef(Chain.BASE, "0x" + "ab" * 20, "OTHR")


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class StaticQuotes:
    def __init__(self) -> None:
        self.quotes: dict[str, MarketQuote] = {}

    def set(self, token: TokenRef, price_usd: str, liquidity_usd: str, at: datetime = T0) -> None:
        self.quotes[token.key] = MarketQuote(token, Decimal(price_usd), Decimal(liquidity_usd), at, "test")

    async def get_quote(self, token: TokenRef) -> MarketQuote:
        if token.key not in self.quotes:
            raise MissingMarketData(token.key)
        return self.quotes[token.key]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fx(clock: FakeClock) -> FxTable:
    table = FxTable(max_age_seconds=10**9)
    table.update(FxRate("USD", Decimal("0.92"), clock.now, "test"))
    return table


@pytest.fixture
def quotes() -> StaticQuotes:
    q = StaticQuotes()
    q.set(TOKEN, "0.002", "400000")
    return q
