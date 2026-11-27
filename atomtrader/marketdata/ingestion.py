"""Market-data ingestion loop (read-only).

Per cycle, within provider rate budgets:
  1. discover new pools per enabled chain (GeckoTerminal; includes unique buyers/sellers)
  2. record DexScreener profiles/boosts (attention; boosts flagged as paid promotion)
  3. refresh snapshots for the watchlist (DexScreener, batched ≤30 tokens/call)
  4. fetch closed 1-minute candles for a rotating subset of watched pools (GeckoTerminal)

Every provider error is classified and logged as an event; one failing source does not
stop the cycle, and nothing is filled in for data that could not be fetched.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from atomtrader.core.models import Chain, TokenRef
from atomtrader.logging import get_logger
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.dexscreener import DexScreenerClient
from atomtrader.marketdata.geckoterminal import GeckoTerminalClient, closed_candles
from atomtrader.marketdata.http import ApiError
from atomtrader.persistence.marketdata_store import MarketDataStore
from atomtrader.persistence.repository import Repository

log = get_logger("ingestion")


@dataclass(frozen=True)
class IngestionConfig:
    chains: tuple[Chain, ...] = (Chain.SOLANA, Chain.BASE, Chain.BSC)
    min_liquidity_usd_to_watch: Decimal = Decimal("20000")
    max_watchlist: int = 60
    candle_pools_per_cycle: int = 4
    candle_timeframe: str = "1m"
    candle_limit: int = 60
    record_attention: bool = True


@dataclass
class WatchItem:
    token: TokenRef
    pool_address: str
    reason: str
    added_at: datetime


@dataclass
class IngestionReport:
    started_at: datetime
    new_pools: int = 0
    snapshots: int = 0
    candles: int = 0
    attention: int = 0
    watchlist_size: int = 0
    rejected_non_base_quote: int = 0
    errors: list[str] = field(default_factory=list)


class IngestionService:
    def __init__(
        self,
        dex: DexScreenerClient,
        gt: GeckoTerminalClient,
        store: MarketDataStore,
        repo: Repository,
        base_assets: BaseAssetRegistry,
        config: IngestionConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.dex = dex
        self.gt = gt
        self.store = store
        self.repo = repo
        self.base_assets = base_assets
        self.cfg = config or IngestionConfig()
        self.clock = clock
        self.watchlist: OrderedDict[str, WatchItem] = OrderedDict()
        self._candle_cursor = 0

    # ------------------------------------------------------------ watchlist
    def watch(self, token: TokenRef, pool_address: str, reason: str) -> None:
        if token.key in self.watchlist:
            self.watchlist[token.key].pool_address = pool_address
            return
        self.watchlist[token.key] = WatchItem(token, pool_address, reason, self.clock())
        while len(self.watchlist) > self.cfg.max_watchlist:
            self.watchlist.popitem(last=False)  # evict oldest

    def unwatch(self, token: TokenRef) -> None:
        self.watchlist.pop(token.key, None)

    async def _error(self, report: IngestionReport, where: str, err: Exception) -> None:
        msg = f"{where}: {err}"
        report.errors.append(msg)
        kind = err.kind.value if isinstance(err, ApiError) else type(err).__name__
        log.warning("ingestion_error", where=where, kind=kind, error=str(err)[:300])
        await self.repo.log_event(self.clock(), "ingestion_error", f"[{kind}] {msg}"[:2048])

    # ------------------------------------------------------------ cycle
    async def run_once(self) -> IngestionReport:
        report = IngestionReport(started_at=self.clock())

        for chain in self.cfg.chains:
            try:
                pools = await self.gt.new_pools(chain)
            except ApiError as err:
                await self._error(report, f"new_pools:{chain.value}", err)
                continue
            report.new_pools += len(pools)
            report.snapshots += await self.store.save_snapshots(pools)
            for p in pools:
                if not self.base_assets.is_base_asset(p.chain, p.quote_address):
                    # Liquidity of pools quoted in unknown tokens is not trustworthy.
                    report.rejected_non_base_quote += 1
                    continue
                if p.liquidity_usd is not None and p.liquidity_usd >= self.cfg.min_liquidity_usd_to_watch:
                    self.watch(p.base, p.pair_address, "new_pool")

        if self.cfg.record_attention:
            for fetch, name in ((self.dex.latest_boosts, "boosts"), (self.dex.latest_profiles, "profiles")):
                try:
                    report.attention += await self.store.save_attention(await fetch())
                except ApiError as err:
                    await self._error(report, f"attention:{name}", err)

        by_chain: dict[Chain, list[str]] = {}
        for item in self.watchlist.values():
            by_chain.setdefault(item.token.chain, []).append(item.token.address)
        for chain, addrs in by_chain.items():
            try:
                snaps = await self.dex.token_pairs(chain, addrs)
            except ApiError as err:
                await self._error(report, f"watchlist:{chain.value}", err)
                continue
            report.snapshots += await self.store.save_snapshots(snaps)
            for s in snaps:
                if s.base.key in self.watchlist:
                    self.watchlist[s.base.key].pool_address = s.pair_address  # track main pool

        items = list(self.watchlist.values())
        if items:
            n = min(self.cfg.candle_pools_per_cycle, len(items))
            for i in range(n):
                item = items[(self._candle_cursor + i) % len(items)]
                try:
                    candles = await self.gt.ohlcv(item.token.chain, item.pool_address,
                                                  self.cfg.candle_timeframe, self.cfg.candle_limit)
                except ApiError as err:
                    await self._error(report, f"ohlcv:{item.token.key}", err)
                    continue
                final = closed_candles(candles, self.cfg.candle_timeframe, self.clock())
                report.candles += await self.store.save_candles(
                    item.token.chain, item.pool_address, self.cfg.candle_timeframe, final, "geckoterminal"
                )
            self._candle_cursor = (self._candle_cursor + n) % len(items)

        report.watchlist_size = len(self.watchlist)
        log.info("ingestion_cycle", new_pools=report.new_pools, snapshots=report.snapshots,
                 candles=report.candles, attention=report.attention,
                 watchlist=report.watchlist_size, rejected_non_base_quote=report.rejected_non_base_quote,
                 errors=len(report.errors))
        return report

    async def refresh_tokens(self, tokens: list[TokenRef]) -> int:
        """Fresh DexScreener snapshots for specific tokens (e.g. open positions)."""
        by_chain: dict[Chain, list[str]] = {}
        for t in tokens:
            by_chain.setdefault(t.chain, []).append(t.address)
        n = 0
        for chain, addrs in by_chain.items():
            try:
                n += await self.store.save_snapshots(await self.dex.token_pairs(chain, addrs))
            except ApiError as err:
                await self._error(IngestionReport(self.clock()), f"refresh:{chain.value}", err)
        return n

    async def run_forever(self, interval_seconds: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # never die silently; storage errors etc.
                log.error("ingestion_cycle_failed", error=repr(exc)[:300])
                await self.repo.log_event(self.clock(), "ingestion_cycle_failed", repr(exc)[:2048])
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
