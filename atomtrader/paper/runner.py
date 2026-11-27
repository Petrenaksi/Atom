"""Long-running paper-mode service: FX refresh, market-data ingestion and equity snapshots.

No strategy is wired in yet (Phase 4), so this service never opens positions by itself; it
records market data and keeps the paper account marked to market.

    python -m atomtrader.paper.runner --cycles 3 --interval 60
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from atomtrader.config.settings import Settings, TradingMode
from atomtrader.core.models import Chain
from atomtrader.core.money import FxTable, MissingMarketData
from atomtrader.execution.paper import PaperExecutionProvider
from atomtrader.logging import configure_logging, get_logger
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.dexscreener import DexScreenerClient, DexScreenerQuoteSource
from atomtrader.marketdata.fx import FxService
from atomtrader.marketdata.geckoterminal import GeckoTerminalClient
from atomtrader.marketdata.ingestion import IngestionConfig, IngestionService
from atomtrader.marketdata.jupiter import JupiterClient, JupiterSizedQuoter
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.marketdata_store import MarketDataStore
from atomtrader.persistence.repository import Repository

log = get_logger("runner")
FX_REFRESH_EVERY_CYCLES = 5


async def run(settings: Settings, cycles: int | None, interval: float) -> None:
    if settings.mode is not TradingMode.PAPER:
        raise SystemExit("runner only supports paper mode")
    repo = Repository(settings.database_url)
    fx_table = FxTable()
    registry = BaseAssetRegistry.load()
    dex, gt, jup = DexScreenerClient(), GeckoTerminalClient(), JupiterClient()
    fx = FxService(fx_table)
    quotes = DexScreenerQuoteSource(dex, registry)
    usdc = next(a.address for a in registry.assets if a.chain is Chain.SOLANA and a.symbol == "USDC")
    # Solana paper fills at exact-size Jupiter quotes (see docs/CALIBRATION.md).
    provider = PaperExecutionProvider(quotes, fx_table, sized_quoters={Chain.SOLANA: JupiterSizedQuoter(jup, usdc)})
    session = PaperTradingSession(settings, repo, quotes, fx_table, provider=provider)
    unresolved = await session.start()
    if unresolved:
        log.warning("unresolved_orders_at_start", orders=unresolved)
    ingestion = IngestionService(dex, gt, MarketDataStore(repo.sessionmaker), repo, registry,
                                 IngestionConfig(chains=tuple(sorted(settings.enabled_chains))))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    n = 0
    try:
        while not stop.is_set() and (cycles is None or n < cycles):
            if n % FX_REFRESH_EVERY_CYCLES == 0:
                rep = await fx.refresh()
                log.info("fx_refresh", updated=list(rep.updated), errors=list(rep.errors))
            await ingestion.run_once()
            try:
                snap = await session.snapshot()
                log.info("paper_equity", label=snap.label, equity_eur=str(snap.equity_eur),
                         cash_eur=str(snap.cash_eur), open_positions=snap.open_positions, unpriced=list(snap.unpriced))
            except MissingMarketData as exc:
                log.warning("snapshot_skipped", reason=str(exc))
            n += 1
            if cycles is None or n < cycles:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
    finally:
        await dex.aclose()
        await gt.aclose()
        await jup.aclose()
        await fx.aclose()
        await repo.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()
    settings = Settings()
    configure_logging(json=settings.log_json)
    asyncio.run(run(settings, args.cycles, args.interval))


if __name__ == "__main__":
    main()
