"""DexScreener adapter (keyless). Docs: https://docs.dexscreener.com/api/reference

Limits: pairs/tokens endpoints 300 req/min, profiles/boosts 60 req/min. We stay well
below. No OHLCV and no unique-wallet counts are available from this source.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from atomtrader.core.models import Chain, MarketQuote, TokenRef, norm_address
from atomtrader.core.money import MissingMarketData
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.http import JsonHttpClient
from atomtrader.marketdata.models import WINDOWS, AttentionSignal, PairSnapshot, WindowStats

BASE_URL = "https://api.dexscreener.com"
CHAIN_IDS: dict[Chain, str] = {
    Chain.SOLANA: "solana",
    Chain.ETHEREUM: "ethereum",
    Chain.BASE: "base",
    Chain.BSC: "bsc",
}
_FROM_ID = {v: k for k, v in CHAIN_IDS.items()}
MAX_TOKENS_PER_CALL = 30


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _int(v: Any) -> int | None:
    return None if v is None else int(v)


def parse_pair(raw: dict[str, Any], as_of: datetime) -> PairSnapshot | None:
    """Parse one DexScreener pair. Returns None for unsupported chains or unpriced pairs."""
    chain = _FROM_ID.get(raw.get("chainId", ""))
    price = _dec(raw.get("priceUsd"))
    if chain is None or price is None or price <= 0:
        return None
    base = raw["baseToken"]
    quote = raw.get("quoteToken") or {}
    txns = raw.get("txns") or {}
    vol = raw.get("volume") or {}
    chg = raw.get("priceChange") or {}
    windows = {
        w: WindowStats(
            buys=_int((txns.get(w) or {}).get("buys")),
            sells=_int((txns.get(w) or {}).get("sells")),
            buyers=None,  # not provided by DexScreener
            sellers=None,
            volume_usd=_dec(vol.get(w)),
            price_change_pct=_dec(chg.get(w)),
        )
        for w in WINDOWS
        if w in txns or w in vol or w in chg
    }
    created = raw.get("pairCreatedAt")
    liquidity = raw.get("liquidity")
    reported = _dec(liquidity.get("usd")) if isinstance(liquidity, dict) else None
    verified = None
    if isinstance(liquidity, dict):
        quote_amt = _dec(liquidity.get("quote"))
        native = _dec(raw.get("priceNative"))
        if quote_amt is not None and native is not None and native > 0:
            quote_side_usd = quote_amt * price / native  # quote-asset units x USD per quote unit
            verified = min(reported, 2 * quote_side_usd) if reported is not None else 2 * quote_side_usd
    return PairSnapshot(
        chain=chain,
        dex=raw.get("dexId", ""),
        pair_address=norm_address(chain, raw["pairAddress"]),
        base=TokenRef(chain, base["address"], base.get("symbol", "")),
        quote_symbol=quote.get("symbol", ""),
        quote_address=norm_address(chain, quote.get("address", "")),
        price_usd=price,
        liquidity_usd=verified,
        fdv_usd=_dec(raw.get("fdv")),
        market_cap_usd=_dec(raw.get("marketCap")),
        pair_created_at=datetime.fromtimestamp(int(created) / 1000, UTC) if created else None,
        as_of=as_of,
        source="dexscreener",
        windows=windows,
        reported_liquidity_usd=reported,
    )


class DexScreenerClient:
    def __init__(
        self,
        http: JsonHttpClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http or JsonHttpClient(BASE_URL, per_minute=120)
        self.clock = clock

    async def aclose(self) -> None:
        await self.http.aclose()

    async def token_pairs(self, chain: Chain, addresses: Iterable[str]) -> list[PairSnapshot]:
        """Main pair for each token (≤30 per call)."""
        addrs = list(dict.fromkeys(addresses))
        out: list[PairSnapshot] = []
        for i in range(0, len(addrs), MAX_TOKENS_PER_CALL):
            batch = ",".join(addrs[i : i + MAX_TOKENS_PER_CALL])
            raw = await self.http.get_json(f"/tokens/v1/{CHAIN_IDS[chain]}/{batch}")
            now = self.clock()
            out.extend(p for p in (parse_pair(r, now) for r in raw or []) if p is not None)
        return out

    async def all_pairs_for_token(self, address: str) -> list[PairSnapshot]:
        raw = await self.http.get_json(f"/latest/dex/tokens/{address}")
        now = self.clock()
        return [p for p in (parse_pair(r, now) for r in raw.get("pairs") or []) if p is not None]

    async def _attention(self, path: str, kind: str) -> list[AttentionSignal]:
        raw = await self.http.get_json(path)
        now = self.clock()
        items = raw if isinstance(raw, list) else [raw]
        return [
            AttentionSignal(
                chain_id=i.get("chainId", ""),
                token_address=i.get("tokenAddress", ""),
                kind=kind,
                boost_amount=_dec(i.get("totalAmount")) if kind == "boost" else None,
                links=tuple(link.get("url", "") for link in i.get("links") or [] if isinstance(link, dict)),
                as_of=now,
                description=str(i.get("description") or "")[:1000],
            )
            for i in items
            if i.get("tokenAddress")
        ]

    async def latest_profiles(self) -> list[AttentionSignal]:
        return await self._attention("/token-profiles/latest/v1", "profile")

    async def latest_boosts(self) -> list[AttentionSignal]:
        return await self._attention("/token-boosts/latest/v1", "boost")


class DexScreenerQuoteSource:
    """QuoteSource for the paper execution provider: prices a token from its main pool.

    Raises MissingMarketData when the token has no priced pair with reported liquidity that
    is quoted in a recognized base asset — paper fills are never simulated against an
    unknown or fake pool depth."""

    def __init__(self, client: DexScreenerClient, base_assets: BaseAssetRegistry, ttl_seconds: float = 5.0) -> None:
        self.client = client
        self.base_assets = base_assets
        self.ttl = ttl_seconds
        self._cache: dict[str, MarketQuote] = {}

    async def get_quote(self, token: TokenRef) -> MarketQuote:
        cached = self._cache.get(token.key)
        now = self.client.clock()
        if cached and (now - cached.as_of).total_seconds() <= self.ttl:
            return cached
        pairs = [p for p in await self.client.token_pairs(token.chain, [token.address])
                 if p.base.address == token.address]
        priced = [p for p in pairs if p.liquidity_usd is not None and p.liquidity_usd > 0
                  and self.base_assets.is_base_asset(p.chain, p.quote_address)]
        if not priced:
            raise MissingMarketData(f"no base-asset-quoted pair with liquidity for {token.key}")
        best = max(priced, key=lambda p: p.liquidity_usd or Decimal(0))
        quote = MarketQuote(token, best.price_usd, best.liquidity_usd or Decimal(0), best.as_of, "dexscreener")
        self._cache[token.key] = quote
        return quote
