"""Paper execution with realistic frictions.

Two fill bases, recorded on every result as ``details["fill_basis"]``:

``exact_quote`` (Solana, when a SizedQuoter is configured)
    The fill uses an executable quote for the exact size (Jupiter), which already contains
    price impact and pool fees across all routes. On top: adverse latency drift (half-normal,
    sd from calibration), platform/aggregator fee and network fee. No route -> REJECTED
    (nothing sent). If the quote service is unavailable: buys are REJECTED; sells fall back
    to the model below, labelled ``model_fallback``, so positions can still be closed.

``model`` / ``model_fallback``
    Constant-product AMM impact against the main pool + calibrated extra cost (Solana) +
    pool fee + aggregator fee + network fee + latency noise. Uncalibrated on EVM chains.

Common to both: slippage protection (adverse drift beyond the order's cap reverts the swap
but burns the network fee) and random non-slippage transaction failures.
Fills are tagged ``AccountKind.PAPER``. No wallet, key or transaction is involved.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from atomtrader.core.models import (
    AccountKind,
    Chain,
    ExecutionResult,
    Fill,
    MarketQuote,
    OrderRequest,
    OrderStatus,
    Side,
)
from atomtrader.core.money import BPS, ONE, ZERO, FxRate, FxTable, MissingMarketData, bps
from atomtrader.execution.base import NoRoute, QuoteSource, SizedQuoter

SOLANA_MODEL_FILE = Path(__file__).resolve().parent.parent / "data" / "slippage_model_solana.json"


@dataclass(frozen=True, slots=True)
class ChainCostModel:
    network_fee_eur: Decimal
    pool_fee_bps: Decimal  # model mode only; inside the price in exact-quote mode
    aggregator_fee_bps: Decimal  # platform/aggregator fee, charged in both modes
    failure_rate: Decimal  # non-slippage failures (dropped/expired/reverted tx)
    adverse_noise_bps_sd: Decimal  # std-dev of adverse latency drift
    extra_cost_bps: Decimal = ZERO  # model mode: calibrated token-level cost beyond AMM + pool fee
    calibration: str = "uncalibrated"


def load_solana_cost_model(path: Path = SOLANA_MODEL_FILE) -> ChainCostModel:
    """Solana costs from the Jupiter-quote calibration (docs/CALIBRATION.md)."""
    base = ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal("0.03"), Decimal(40))
    if not path.exists():
        return base
    m = json.loads(path.read_text())
    return ChainCostModel(
        network_fee_eur=base.network_fee_eur,
        pool_fee_bps=base.pool_fee_bps,
        aggregator_fee_bps=base.aggregator_fee_bps,
        failure_rate=base.failure_rate,
        adverse_noise_bps_sd=Decimal(str(round(m["noise_bps_sd"], 2))),
        extra_cost_bps=Decimal(str(round(m["fallback_extra_cost_bps_per_side"], 2))),
        calibration=f"jupiter_quotes:{m['fitted_at']}",
    )


DEFAULT_COST_MODELS: dict[Chain, ChainCostModel] = {
    Chain.SOLANA: load_solana_cost_model(),
    # EVM chains: Phase 1 research estimates, not yet calibrated against live quotes.
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
        sized_quoters: dict[Chain, SizedQuoter] | None = None,
    ) -> None:
        self._quotes = quotes
        self._fx = fx
        self._costs = cost_models or DEFAULT_COST_MODELS
        self._quoters = sized_quoters or {}
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

    # ------------------------------------------------------------------ helpers
    def _result(self, order: OrderRequest, status: OrderStatus, **kw: object) -> ExecutionResult:
        return ExecutionResult(client_order_id=order.client_order_id, status=status,
                               account_kind=self.account_kind, **kw)  # type: ignore[arg-type]

    def _failed(self, order: OrderRequest, fee: Decimal, error: str, slip: Decimal | None, basis: str) -> ExecutionResult:
        return self._result(order, OrderStatus.FAILED, failed_fee_eur=fee, realized_slippage_bps=slip,
                            error=error, details={"fill_basis": basis})

    def _drift(self, cost: ChainCostModel) -> Decimal:
        """Adverse latency drift as a fraction (half-normal)."""
        return Decimal(str(abs(self._rng.gauss(0.0, 1.0)))) * bps(cost.adverse_noise_bps_sd)

    def _fill(self, order: OrderRequest, quantity: Decimal, gross_eur: Decimal, fee_eur: Decimal,
              usd: FxRate, now: datetime, slip_bps: Decimal, details: dict[str, str]) -> ExecutionResult:
        self._seq += 1
        exec_price_eur = gross_eur / quantity
        fill = Fill(
            client_order_id=order.client_order_id, position_id=order.position_id, token=order.token,
            side=order.side, quantity=quantity, price_usd=usd.from_eur(exec_price_eur), gross_eur=gross_eur,
            fee_eur=fee_eur, fx=usd, executed_at=now, tx_ref=f"paper-{self._seq}", account_kind=self.account_kind,
        )
        return self._result(order, OrderStatus.FILLED, fill=fill, realized_slippage_bps=slip_bps, details=details)

    # ------------------------------------------------------------------ main path
    async def _execute_once(self, order: OrderRequest) -> ExecutionResult:
        now = self._clock()
        cost = self._costs[order.token.chain]
        try:
            quote = await self._quotes.get_quote(order.token)
            usd = self._fx.get("USD", now)
        except MissingMarketData as exc:
            # Nothing was sent; no fee. Rejected, not failed.
            return self._result(order, OrderStatus.REJECTED, error=f"missing_market_data: {exc}")
        if quote.price_usd <= 0 or quote.liquidity_usd <= 0:
            return self._result(order, OrderStatus.REJECTED, error="invalid_quote")

        quoter = self._quoters.get(order.token.chain)
        basis = "model"
        if quoter is not None:
            basis = "exact_quote"
            try:
                sized = await self._sized(order, quoter, usd)
            except NoRoute as exc:
                return self._result(order, OrderStatus.REJECTED, error=f"no_route: {exc}",
                                    details={"fill_basis": basis})
            except MissingMarketData as exc:
                if order.side is Side.BUY:
                    return self._result(order, OrderStatus.REJECTED, error=f"quote_unavailable: {exc}",
                                        details={"fill_basis": basis})
                sized = None
                basis = "model_fallback"
        else:
            sized = None

        if Decimal(str(self._rng.random())) < cost.failure_rate:
            return self._failed(order, cost.network_fee_eur, "simulated_tx_failure", None, basis)

        spot_eur = usd.to_eur(quote.price_usd)
        if sized is not None:
            return self._exact_quote_fill(order, cost, usd, spot_eur, now, *sized)
        return self._model_fill(order, cost, usd, spot_eur, quote, now, basis)

    async def _sized(self, order: OrderRequest, quoter: SizedQuoter, usd: FxRate) -> tuple[Decimal, Decimal, dict[str, str]]:
        """Returns (quantity, gross_eur, details) at the exact executable quote."""
        if order.side is Side.BUY:
            assert order.notional_eur is not None
            qty, details = await quoter.buy(order.token.address, usd.from_eur(order.notional_eur), order.max_slippage_bps)
            return qty, order.notional_eur, details
        assert order.quantity is not None
        usd_out, details = await quoter.sell(order.token.address, order.quantity, order.max_slippage_bps)
        return order.quantity, usd.to_eur(usd_out), details

    def _exact_quote_fill(self, order: OrderRequest, cost: ChainCostModel, usd: FxRate, spot_eur: Decimal,
                          now: datetime, quantity: Decimal, gross_eur: Decimal, qdetails: dict[str, str]) -> ExecutionResult:
        drift = self._drift(cost)
        if drift * BPS > order.max_slippage_bps:
            # Landed amount below the quote's minimum-out threshold: swap reverts, fee burned.
            return self._failed(order, cost.network_fee_eur, "slippage_exceeded", drift * BPS, "exact_quote")
        if order.side is Side.BUY:
            quantity = quantity * (ONE - drift)
            slip = (gross_eur / quantity) / spot_eur - ONE
        else:
            gross_eur = gross_eur * (ONE - drift)
            slip = ONE - (gross_eur / quantity) / spot_eur
        fee_eur = gross_eur * bps(cost.aggregator_fee_bps) + cost.network_fee_eur
        details = {"fill_basis": "exact_quote", "drift_bps": f"{drift * BPS:.2f}", **qdetails}
        return self._fill(order, quantity, gross_eur, fee_eur, usd, now, slip * BPS, details)

    def _model_fill(self, order: OrderRequest, cost: ChainCostModel, usd: FxRate, spot_eur: Decimal,
                    quote: MarketQuote, now: datetime, basis: str) -> ExecutionResult:
        liquidity_eur = usd.to_eur(quote.liquidity_usd)
        noise = self._drift(cost)
        extra = bps(cost.extra_cost_bps)
        if order.side is Side.BUY:
            assert order.notional_eur is not None
            gross_eur = order.notional_eur
            slip = amm_buy_impact(gross_eur, liquidity_eur) + extra + noise
            quantity = gross_eur / (spot_eur * (ONE + slip))
        else:
            assert order.quantity is not None
            quantity = order.quantity
            slip = min(amm_sell_impact(quantity * spot_eur, liquidity_eur) + extra + noise, Decimal("0.99"))
            gross_eur = quantity * spot_eur * (ONE - slip)

        slip_bps = slip * BPS
        if slip_bps > order.max_slippage_bps:
            # Slippage protection: on-chain the swap reverts, but the network fee is burned.
            return self._failed(order, cost.network_fee_eur, "slippage_exceeded", slip_bps, basis)
        fee_eur = gross_eur * bps(cost.pool_fee_bps + cost.aggregator_fee_bps) + cost.network_fee_eur
        return self._fill(order, quantity, gross_eur, fee_eur, usd, now, slip_bps,
                          {"fill_basis": basis, "calibration": cost.calibration})
