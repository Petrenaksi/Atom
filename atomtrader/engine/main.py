"""Entry point for the autonomous paper-trading engine.

    python -m atomtrader.engine.main                 # run until SIGINT/SIGTERM
    python -m atomtrader.engine.main --scans 3       # bounded run (smoke test)

Autonomous mode is OFF by default and is switched on/off by the operator (dashboard command
or ``--autonomous`` flag, paper mode only). Live mode is refused here: there is no live
execution provider, and live trading requires the owner's explicit authorization.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from atomtrader.config.settings import Settings, TradingMode
from atomtrader.core.models import Chain
from atomtrader.core.money import FxTable
from atomtrader.engine.orchestrator import EngineConfig, TradingEngine
from atomtrader.execution.paper import PaperExecutionProvider
from atomtrader.logging import configure_logging, get_logger
from atomtrader.marketdata.base_assets import BaseAssetRegistry
from atomtrader.marketdata.dexscreener import DexScreenerClient, DexScreenerQuoteSource
from atomtrader.marketdata.fx import FxService
from atomtrader.marketdata.geckoterminal import GeckoTerminalClient
from atomtrader.marketdata.ingestion import IngestionConfig, IngestionService
from atomtrader.marketdata.jupiter import JupiterClient, JupiterSizedQuoter
from atomtrader.narrative.sources import NewsSources
from atomtrader.notify import Notifier, WebhookChannel
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.marketdata_store import MarketDataStore
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.security_store import SecurityStore
from atomtrader.scanner.service import ScannerService
from atomtrader.security.assessor import SecurityAssessor
from atomtrader.security.sources import SecuritySources

log = get_logger("main")


async def run(settings: Settings, scans: int | None, autonomous: bool) -> None:
    if settings.mode is not TradingMode.PAPER:
        raise SystemExit("refusing to start: only paper mode is implemented; live trading needs explicit authorization")
    repo = Repository(settings.database_url)
    registry = BaseAssetRegistry.load()
    usdc = next(a.address for a in registry.assets if a.chain is Chain.SOLANA and a.symbol == "USDC")
    fx_table = FxTable()
    dex, gt, jup = DexScreenerClient(), GeckoTerminalClient(), JupiterClient()
    news, sec = NewsSources(), SecuritySources()
    fx = FxService(fx_table)
    quoter = JupiterSizedQuoter(jup, usdc)
    quotes = DexScreenerQuoteSource(dex, registry)
    provider = PaperExecutionProvider(quotes, fx_table, sized_quoters={Chain.SOLANA: quoter})
    session = PaperTradingSession(settings, repo, quotes, fx_table, provider=provider)
    store = MarketDataStore(repo.sessionmaker)
    ingestion = IngestionService(dex, gt, store, repo, registry,
                                 IngestionConfig(chains=tuple(sorted(settings.enabled_chains))))
    scanner = ScannerService(store, SecurityStore(repo.sessionmaker), SecurityAssessor(sec, quoter))
    channels = [WebhookChannel(url)] if (url := os.environ.get("ATOM_WEBHOOK_URL")) else []
    engine = TradingEngine(session, ingestion, scanner, Notifier(repo, channels), fx=fx, news=news, quoter=quoter,
                           config=EngineConfig())
    await repo.init()
    await fx.refresh()
    await engine.start()
    if autonomous:
        await session.set_autonomous(True, actor="cli")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await engine.run(stop, max_scans=scans)
    finally:
        for c in (dex, gt, jup, news, sec, fx):
            await c.aclose()
        await repo.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scans", type=int, default=None)
    ap.add_argument("--autonomous", action="store_true", help="paper mode only: enable autonomous entries at start")
    args = ap.parse_args()
    settings = Settings()
    configure_logging(json=settings.log_json)
    asyncio.run(run(settings, args.scans, args.autonomous))


if __name__ == "__main__":
    main()
