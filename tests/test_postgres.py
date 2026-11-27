"""Persistence on PostgreSQL (production backend). Skipped unless ATOM_TEST_PG_URL is set, e.g.
ATOM_TEST_PG_URL=postgresql+asyncpg://atom_test:atom_test_pw@localhost:5432/atom_test"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

import pytest

PG = os.environ.get("ATOM_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG, reason="ATOM_TEST_PG_URL not set")


@pytest.fixture
async def pg():
    from atomtrader.persistence.repository import Base, Repository

    repo = Repository(PG)
    import atomtrader.persistence.marketdata_store  # noqa: F401
    import atomtrader.persistence.security_store  # noqa: F401
    import atomtrader.persistence.trading_store  # noqa: F401

    async with repo.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await repo.init()
    yield repo
    await repo.close()


async def test_paper_session_lifecycle_on_postgres(pg, quotes, fx, clock) -> None:
    from atomtrader.config import Settings
    from atomtrader.paper.session import PaperTradingSession
    from atomtrader.persistence.repository import Repository
    from tests.test_paper_session import NO_FAIL, intent
    from atomtrader.execution.paper import PaperExecutionProvider
    from tests.conftest import TOKEN

    settings = Settings(_env_file=None, database_url=PG)
    s = PaperTradingSession(settings, pg, quotes, fx, provider=PaperExecutionProvider(quotes, fx, NO_FAIL, seed=3, clock=clock), clock=clock)
    await s.start()
    r = await s.enter(intent(), {"why": "pg"}, confirm=True)
    assert r.status == "filled"
    clock.advance(minutes=10)
    quotes.set(TOKEN, "0.0022", "400000")
    out = await s.exit(r.position_id, "test")
    assert out.closed_trade is not None
    await s.emergency_stop("pg test")

    # restart on a fresh connection pool: Decimal precision, tz-aware datetimes, controls
    repo2 = Repository(PG)
    s2 = PaperTradingSession(settings, repo2, quotes, fx, clock=clock)
    await s2.start()
    assert s2.ledger.cash_eur == s.ledger.cash_eur  # exact Decimal round trip
    assert s2.controls.emergency_stop and s2.state.entries_today == 1
    [d] = [x for x in await repo2.decisions() if x.approved]
    assert d.at.tzinfo is not None and d.evidence["why"] == "pg"
    await repo2.close()


async def test_marketdata_and_security_stores_on_postgres(pg) -> None:
    from atomtrader.core.models import Chain
    from atomtrader.marketdata.geckoterminal import parse_ohlcv
    from atomtrader.marketdata.http import parse_json
    from atomtrader.persistence.marketdata_store import MarketDataStore
    from atomtrader.persistence.security_store import SecurityStore
    from tests.test_marketdata import fixture
    from tests.test_security import BONK, NOW, fx as sfx
    from atomtrader.security.models import SecurityReport
    from atomtrader.security.sources import apply_rugcheck

    store = MarketDataStore(pg.sessionmaker)
    candles = parse_ohlcv(parse_json(fixture("geckoterminal_ohlcv_minute.json")))
    assert await store.save_candles(Chain.SOLANA, "p", "1m", candles, "gt") == 30
    assert await store.save_candles(Chain.SOLANA, "p", "1m", candles, "gt") == 0
    assert await store.load_candles(Chain.SOLANA, "p", "1m") == candles  # micro-cap precision preserved

    r = SecurityReport(BONK, NOW)
    apply_rugcheck(r, sfx("rugcheck_report_bonk.json"))
    ss = SecurityStore(pg.sessionmaker)
    await ss.save(r)
    [h] = await ss.holder_history(BONK.key, NOW - timedelta(hours=1))
    assert h.holders[0].pct == r.holders[0].pct and isinstance(h.holders[0].pct, Decimal)
