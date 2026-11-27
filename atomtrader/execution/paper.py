"""Paper execution with realistic frictions.

Models, per chain:
  * constant-product AMM price impact against the pool's quote-side reserve
  * pool fee + aggregator fee
  * network fee (also charged when a transaction fails)
  * adverse latency drift / extra slippage (half-normal, seeded RNG)
  * slippage protection: if realized slippage exceeds the order's cap the swap reverts
  * random transaction failures
Fills are tagged ``AccountKind.PAPER``. No wallet, key or network call is involved.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from atomtrader.core.models import (
    AccountKind,
    Chain,
    ExecutionResult,
    Fill,
    OrderRequest,
    OrderStatus,
    Side,
)
from atomtrader.core.money import BPS, ONE, FxTable, MissingMarketData, bps
from atomtrader.execution.base import QuoteSource


@dataclass(frozen=True, slots=True)
class ChainCostModel:
    network_fee_eur: Decimal
    pool_fee_bps: Decimal
    aggregator_fee_bps: Decimal
    failure_rate: Decimal  # non-slippage failures (dropped/expired/reverted tx)
    adverse_noise_bps_sd: Decimal  # std-dev of extra adverse slippage from latency


DEFAULT_COST_MODELS: dict[Chain, ChainCostModel] = {
    # Conservative defaults from Phase 1 research; calibrate from live paper observations.
    Chain.SOLANA: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal("0.03"), Decimal(40)),
    Chain.BASE: ChainCostModel(Decimal("0.10"), Decimal(30), Decimal(15), Decimal("0.02"), Decimal(30)),
    Chain.BSC: ChainCostModel(Decimal("0.15"), Decimal(25), Decimal(15), Decimal("0.02"), Decimal(30)),
    Chain.ETHEREUM: ChainCostModel(Decimal("6.00"), Decimal(30), Decimal(15), Decimal("0.02"), Decimal(25)),
}


def amm_buy_impact(spend_eur: Decimal, pool_liquidity_eur: Decimal) -> Decimal:
    """Average-price impact fraction of a buy in an x*y=k pool (quote reserve = liq/2)."""
    reserve = pool_liquidity_eur / 2
    return spend_eur / reserve


def amm_sell_impact(value_eur: Decimal, pool_liquidity_eur: Decimal) -> Decimal:
    """Average-price impact fraction of a sell of tokens worth ``value_eur`` at spot."""
    reserve = pool_liquidity_eur / 2
    return ONE - ONE / (ONE + value_eur / reserve)


class PaperExecutionProvider:
    account_kind = AccountKind.PAPER

    def __init__(
        self,
        quotes: QuoteSource,
        fx: FxTable,
        cost_models: dict[Chain, ChainCostModel] | None = None,
        seed: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._quotes = quotes
        self._fx = fx
        self._costs = cost_models or DEFAULT_COST_MODELS
        self._rng = random.Random(seed)
        self._clock = clock
        self._results: dict[str, ExecutionResult] = {}
        self._seq = 0

    async def lookup(self, client_order_id: str) -> ExecutionResult | None:
        return self._results.get(client_order_id)

    async def execute(self, order: OrderRequest) -> ExecutionResult:
        if order.client_order_id in self._results:
            return self._results[order.client_order_id]
        result = await self._execute_once(order)
        self._results[order.client_order_id] = result
        return result

    def _failed(self, order: OrderRequest, fee: Decimal, error: str, slip: Decimal | None = None) -> ExecutionResult:
        return ExecutionResult(
            client_order_id=order.client_order_id,
            status=OrderStatus.FAILED,
            account_kind=self.account_kind,
            failed_fee_eur=fee,
            realized_slippage_bps=slip,
            error=error,
        )

    async def _execute_once(self, order: OrderRequest) -> ExecutionResult:
        now = self._clock()
        cost = self._costs[order.token.chain]
        try:
            quote = await self._quotes.get_quote(order.token)
            usd = self._fx.get("USD", now)
        except MissingMarketData as exc:
            # Nothing was sent; no fee. Rejected, not failed.
            return ExecutionResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                account_kind=self.account_kind,
                error=f"missing_market_data: {exc}",
            )
        if quote.price_usd <= 0 or quote.liquidity_usd <= 0:
            return ExecutionResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                account_kind=self.account_kind,
                error="invalid_quote",
            )

        if Decimal(str(self._rng.random())) < cost.failure_rate:
            return self._failed(order, cost.network_fee_eur, "simulated_tx_failure")

        spot_eur = usd.to_eur(quote.price_usd)
        liquidity_eur = usd.to_eur(quote.liquidity_usd)
        noise = Decimal(str(abs(self._rng.gauss(0.0, 1.0)))) * bps(cost.adverse_noise_bps_sd)

        if order.side is Side.BUY:
            assert order.notional_eur is not None
            gross_eur = order.notional_eur
            impact = amm_buy_impact(gross_eur, liquidity_eur)
            slip = impact + noise
            exec_price_eur = spot_eur * (ONE + slip)
            quantity = gross_eur / exec_price_eur
        else:
            assert order.quantity is not None
            quantity = order.quantity
            impact = amm_sell_impact(quantity * spot_eur, liquidity_eur)
            slip = min(impact + noise, Decimal("0.99"))
            exec_price_eur = spot_eur * (ONE - slip)
            gross_eur = quantity * exec_price_eur

        slip_bps = slip * BPS
        if slip_bps > order.max_slippage_bps:
            # Slippage protection: on-chain the swap reverts, but the network fee is burned.
            return self._failed(order, cost.network_fee_eur, "slippage_exceeded", slip_bps)

        fee_eur = gross_eur * bps(cost.pool_fee_bps + cost.aggregator_fee_bps) + cost.network_fee_eur
        self._seq += 1
        fill = Fill(
            client_order_id=order.client_order_id,
            position_id=order.position_id,
            token=order.token,
            side=order.side,
            quantity=quantity,
            price_usd=usd.from_eur(exec_price_eur),
            gross_eur=gross_eur,
            fee_eur=fee_eur,
            fx=usd,
            executed_at=now,
            tx_ref=f"paper-{self._seq}",
            account_kind=self.account_kind,
        )
        return ExecutionResult(
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED,
            account_kind=self.account_kind,
            fill=fill,
            realized_slippage_bps=slip_bps,
        )
