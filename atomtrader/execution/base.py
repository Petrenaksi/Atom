"""Execution provider interface. Market-data providers are separate and independently
replaceable; an execution provider only needs a quote source to price paper fills."""

from __future__ import annotations

from typing import Protocol

from atomtrader.core.models import AccountKind, ExecutionResult, MarketQuote, OrderRequest, TokenRef


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
