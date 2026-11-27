"""GeckoTerminal adapter (keyless public API, ~30 calls/min).

Provides what DexScreener lacks: OHLCV candles and unique buyers/sellers per window.
Docs: https://apiguide.geckoterminal.com
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from atomtrader.core.models import Chain, TokenRef, norm_address
from atomtrader.marketdata.http import JsonHttpClient
from atomtrader.marketdata.models import Candle, PairSnapshot, WindowStats

BASE_URL = "https://api.geckoterminal.com/api/v2"
NETWORKS: dict[Chain, str] = {
    Chain.SOLANA: "solana",
    Chain.ETHEREUM: "eth",
    Chain.BASE: "base",
    Chain.BSC: "bsc",
}
TIMEFRAMES: dict[str, tuple[str, int]] = {
    # name -> (GeckoTerminal timeframe, aggregate)
    "1m": ("minute", 1),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "4h": ("hour", 4),
    "1d": ("day", 1),
}
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def parse_ohlcv(raw: dict[str, Any]) -> list[Candle]:
    """Rows are [ts, o, h, l, c, v] newest-first; return oldest-first."""
    rows = raw["data"]["attributes"]["ohlcv_list"]
    candles = [
        Candle(
            ts=datetime.fromtimestamp(int(r[0]), UTC),
            open=_dec(r[1]),  # type: ignore[arg-type]
            high=_dec(r[2]),  # type: ignore[arg-type]
            low=_dec(r[3]),  # type: ignore[arg-type]
            close=_dec(r[4]),  # type: ignore[arg-type]
            volume_usd=_dec(r[5]),  # type: ignore[arg-type]
        )
        for r in rows
    ]
    return sorted(candles, key=lambda c: c.ts)


def closed_candles(candles: list[Candle], timeframe: str, now: datetime) -> list[Candle]:
    """Drop the still-forming bar so stored candles are final."""
    span = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return [c for c in candles if c.ts + span <= now]


def _token_from_rel(rel: dict[str, Any], chain: Chain, symbol: str = "") -> TokenRef:
    # ids look like "solana_<address>"
    ident = rel["data"]["id"]
    return TokenRef(chain, ident.split("_", 1)[1], symbol)


def parse_pool(item: dict[str, Any], chain: Chain, as_of: datetime) -> PairSnapshot | None:
    a = item["attributes"]
    price = _dec(a.get("base_token_price_usd"))
    if price is None or price <= 0:
        return None
    rel = item.get("relationships") or {}
    name = a.get("name", "")
    base_sym, _, quote_sym = name.partition(" / ")
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    windows = {}
    for w in set(tx) | set(vol) | set(chg):
        t = tx.get(w) or {}
        windows[w] = WindowStats(
            buys=t.get("buys"),
            sells=t.get("sells"),
            buyers=t.get("buyers"),
            sellers=t.get("sellers"),
            volume_usd=_dec(vol.get(w)),
            price_change_pct=_dec(chg.get(w)),
        )
    created = a.get("pool_created_at")
    quote = _token_from_rel(rel["quote_token"], chain) if "quote_token" in rel else None
    return PairSnapshot(
        chain=chain,
        dex=((rel.get("dex") or {}).get("data") or {}).get("id", ""),
        pair_address=norm_address(chain, a["address"]),
        base=_token_from_rel(rel["base_token"], chain, base_sym.strip()),
        quote_symbol=quote_sym.strip(),
        quote_address=quote.address if quote else "",
        price_usd=price,
        liquidity_usd=None,  # reserve_in_usd values the token side at its own price: unverified
        reported_liquidity_usd=_dec(a.get("reserve_in_usd")),
        fdv_usd=_dec(a.get("fdv_usd")),
        market_cap_usd=_dec(a.get("market_cap_usd")),
        pair_created_at=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
        as_of=as_of,
        source="geckoterminal",
        windows=windows,
    )


class GeckoTerminalClient:
    def __init__(
        self,
        http: JsonHttpClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # Docs disagree (30/min vs ~10/min) and 20/min drew live 429s: stay at 8/min.
        self.http = http or JsonHttpClient(BASE_URL, per_minute=8)
        self.clock = clock

    async def aclose(self) -> None:
        await self.http.aclose()

    async def ohlcv(self, chain: Chain, pool_address: str, timeframe: str = "1m", limit: int = 100) -> list[Candle]:
        tf, agg = TIMEFRAMES[timeframe]
        raw = await self.http.get_json(
            f"/networks/{NETWORKS[chain]}/pools/{pool_address}/ohlcv/{tf}",
            {"aggregate": agg, "limit": min(limit, 1000), "currency": "usd", "token": "base"},
        )
        return parse_ohlcv(raw)

    async def _pools(self, path: str, chain: Chain) -> list[PairSnapshot]:
        raw = await self.http.get_json(path)
        now = self.clock()
        return [p for p in (parse_pool(i, chain, now) for i in raw.get("data") or []) if p is not None]

    async def new_pools(self, chain: Chain) -> list[PairSnapshot]:
        return await self._pools(f"/networks/{NETWORKS[chain]}/new_pools", chain)

    async def trending_pools(self, chain: Chain) -> list[PairSnapshot]:
        return await self._pools(f"/networks/{NETWORKS[chain]}/trending_pools", chain)
