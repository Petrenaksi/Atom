"""Execution provider interface. Market-data providers are separate and independently
replaceable; an execution provider only needs a quote source to price paper fills."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from atomtrader.core.models import AccountKind, ExecutionResult, MarketQuote, OrderRequest, TokenRef
from atomtrader.core.money import MissingMarketData


class QuoteSource(Protocol):
    async def get_quote(self, token: TokenRef) -> MarketQuote:
        """Return the latest quote or raise MissingMarketData. Never fabricate."""
        ...


class ExecutionProvider(Protocol):
    account_kind: AccountKind

    async def execute(self, order: OrderRequest) -> ExecutionResult:
        """Execute one attempt. Must be idempotent per ``client_order_id``: calling twice with
        the same id returns the original result and never trades twice."""
        ...

    async def lookup(self, client_order_id: str) -> ExecutionResult | None:
        """Return the outcome of a previously submitted order (for restart reconciliation),
        or None if the provider has no record of it."""
        ...


class NoRoute(MissingMarketData):
    """The venue/aggregator has no executable route for this token and size. Token-specific
    (e.g. no liquidity, sell blocked) — not an outage. Nothing is sent."""


class QuoteUnavailable(MissingMarketData):
    """A sized quote could not be obtained (rate limit, network, provider error)."""


class SizedQuoter(Protocol):
    """Executable prices for an exact trade size, in USD terms (stablecoin base asset)."""

    async def buy(self, address: str, spend_usd: Decimal, slippage_bps: int) -> tuple[Decimal, dict[str, str]]:
        """Tokens received for ``spend_usd``; plus details (route etc.)."""
        ...

    async def sell(self, address: str, quantity: Decimal, slippage_bps: int) -> tuple[Decimal, dict[str, str]]:
        """USD received for ``quantity`` tokens; plus details."""
        ...
