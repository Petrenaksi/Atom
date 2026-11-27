"""Jupiter (Solana) read-only adapter: exact-size swap quotes, prices, token metadata.

Quotes are *executable-size* prices across all Jupiter routes, so they capture price impact
and pool fees for the actual trade size — far better than a single-pool formula. They do
not include Jupiter's platform fee on managed execution (``platformFee`` is null on the
keyless endpoint), so callers must add that separately.

The trading base asset on Solana is USDC (not SOL): positions are quoted USDC<->token so
the account carries no incidental SOL price exposure.

Keyless endpoint: ~0.5-1 request/second. No wallet, key or transaction is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atomtrader.core.money import MissingMarketData
from atomtrader.execution.base import NoRoute, QuoteUnavailable
from atomtrader.marketdata.http import ApiError, ErrorKind, JsonHttpClient

__all__ = ["JupiterClient", "JupiterQuote", "JupiterPrice", "JupiterSizedQuoter", "NoRoute", "QuoteUnavailable"]

BASE_URL = "https://lite-api.jup.ag"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class JupiterQuote:
    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    price_impact_pct: Decimal
    route: tuple[str, ...]
    context_slot: int | None
    swap_usd_value: Decimal | None


@dataclass(frozen=True, slots=True)
class JupiterPrice:
    mint: str
    usd_price: Decimal
    decimals: int
    liquidity_usd: Decimal | None  # aggregated across Jupiter-routable pools
    block_id: int | None


def parse_quote(raw: dict[str, Any]) -> JupiterQuote:
    return JupiterQuote(
        input_mint=raw["inputMint"],
        output_mint=raw["outputMint"],
        in_amount_raw=int(raw["inAmount"]),
        out_amount_raw=int(raw["outAmount"]),
        price_impact_pct=Decimal(str(raw.get("priceImpactPct") or "0")),
        route=tuple(r["swapInfo"].get("label", "") for r in raw.get("routePlan", [])),
        context_slot=raw.get("contextSlot"),
        swap_usd_value=Decimal(str(raw["swapUsdValue"])) if raw.get("swapUsdValue") else None,
    )


def parse_prices(raw: dict[str, Any]) -> dict[str, JupiterPrice]:
    out: dict[str, JupiterPrice] = {}
    for mint, p in (raw or {}).items():
        if not isinstance(p, dict) or p.get("usdPrice") is None:
            continue
        out[mint] = JupiterPrice(
            mint=mint,
            usd_price=Decimal(str(p["usdPrice"])),
            decimals=int(p["decimals"]),
            liquidity_usd=Decimal(str(p["liquidity"])) if p.get("liquidity") is not None else None,
            block_id=p.get("blockId"),
        )
    return out


class JupiterClient:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient(BASE_URL, per_minute=30, max_retries=1)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int = 300) -> JupiterQuote:
        try:
            raw = await self.http.get_json(
                "/swap/v1/quote",
                {"inputMint": input_mint, "outputMint": output_mint, "amount": amount_raw,
                 "slippageBps": slippage_bps, "swapMode": "ExactIn"},
            )
        except ApiError as err:
            if err.kind is ErrorKind.API_REJECTION:
                raise NoRoute(f"{input_mint}->{output_mint}: {err.message}") from err
            raise
        q = parse_quote(raw)
        if q.out_amount_raw <= 0:
            raise NoRoute(f"{input_mint}->{output_mint}: zero output")
        return q

    async def prices(self, mints: list[str]) -> dict[str, JupiterPrice]:
        out: dict[str, JupiterPrice] = {}
        for i in range(0, len(mints), 50):
            out.update(parse_prices(await self.http.get_json("/price/v3", {"ids": ",".join(mints[i : i + 50])})))
        return out


class JupiterSizedQuoter:
    """Exact-size executable prices for paper fills on Solana, USDC as the base asset.

    Implements ``execution.base.SizedQuoter``. USDC is treated as 1 USD for sizing; the
    measured USDC/USD deviation is a few bps (see calibration)."""

    def __init__(self, client: JupiterClient, usdc_mint: str) -> None:
        self.client = client
        self.usdc = usdc_mint
        self._decimals: dict[str, int] = {}

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            try:
                prices = await self.client.prices([mint, self.usdc])
            except ApiError as err:
                raise QuoteUnavailable(f"jupiter price: {err}") from err
            if self.usdc not in prices:
                raise QuoteUnavailable("jupiter price: USDC missing from response")
            if mint not in prices:
                # Token-specific: Jupiter does not price/route this token.
                raise NoRoute(f"no Jupiter price/decimals for {mint}")
            self._decimals[mint] = prices[mint].decimals
            self._decimals[self.usdc] = prices[self.usdc].decimals
        return self._decimals[mint]

    async def _quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> JupiterQuote:
        if amount_raw <= 0:
            raise NoRoute("amount rounds to zero base units")
        try:
            return await self.client.quote(input_mint, output_mint, amount_raw, slippage_bps)
        except ApiError as err:  # NoRoute (token-specific) propagates unchanged
            raise QuoteUnavailable(f"jupiter quote: {err}") from err

    @staticmethod
    def _details(q: JupiterQuote) -> dict[str, str]:
        return {"route": "+".join(q.route), "context_slot": str(q.context_slot),
                "jup_price_impact_pct": str(q.price_impact_pct)}

    async def buy(self, address: str, spend_usd: Decimal, slippage_bps: int) -> tuple[Decimal, dict[str, str]]:
        """Tokens received for spending ``spend_usd`` USDC."""
        tok_dec = await self.decimals(address)  # also caches USDC decimals
        usdc_dec = self._decimals[self.usdc]
        q = await self._quote(self.usdc, address, int(spend_usd * 10**usdc_dec), slippage_bps)
        return Decimal(q.out_amount_raw) / Decimal(10**tok_dec), self._details(q)

    async def sell(self, address: str, quantity: Decimal, slippage_bps: int) -> tuple[Decimal, dict[str, str]]:
        """USDC received for selling ``quantity`` tokens."""
        tok_dec = await self.decimals(address)
        usdc_dec = self._decimals[self.usdc]
        q = await self._quote(address, self.usdc, int(quantity * 10**tok_dec), slippage_bps)
        return Decimal(q.out_amount_raw) / Decimal(10**usdc_dec), self._details(q)

    async def round_trip_cost_bps(self, address: str, size_usd: Decimal, slippage_bps: int = 300) -> Decimal:
        """Pre-trade estimate: buy ``size_usd`` then sell exactly what was received.
        Independent of any mid price; excludes platform and network fees."""
        qty, _ = await self.buy(address, size_usd, slippage_bps)
        usd_back, _ = await self.sell(address, qty, slippage_bps)
        return (1 - usd_back / size_usd) * 10_000
