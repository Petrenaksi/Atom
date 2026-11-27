"""End-to-end engine test: real session/ledger/risk/DB/notifier, fake data providers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from atomtrader.config import Settings
from atomtrader.core.models import Chain
from atomtrader.engine.orchestrator import EngineConfig, TradingEngine
from atomtrader.execution.paper import DEFAULT_COST_MODELS, ChainCostModel, PaperExecutionProvider
from atomtrader.notify import Notifier
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.trading_store import enqueue_command, load_trade_records
from atomtrader.scanner.moonshot import moonshot_score
from atomtrader.scanner.service import Candidate
from tests.conftest import FakeClock, StaticQuotes
from tests.test_strategy import TOK, T0, bars, breakout_path, ctx

NO_FAIL = {c: ChainCostModel(m.network_fee_eur, m.pool_fee_bps, m.aggregator_fee_bps, Decimal(0), Decimal(0))
           for c, m in DEFAULT_COST_MODELS.items()}


class FakeIngestion:
    def __init__(self):
        from atomtrader.marketdata.ingestion import WatchItem

        self.watchlist = {TOK.key: WatchItem(TOK, "Pool", "test", T0)}
        self.runs = 0

        class Dex:
            async def latest_profiles(self):
                return []

        self.dex = Dex()

    async def run_once(self):
        self.runs += 1

    async def refresh_tokens(self, tokens):
        return 0

    def watch(self, token, pool, reason):
        pass


class FakeScanner:
    """Serves a MarketContext-derived candidate for the current 'market' (set by the test)."""

    def __init__(self):
        self.market = None
        self.age = 0.0

    async def evaluate(self, token, pool_address=None, narrative_strength=None, assess=True, security_max_age=None):
        from atomtrader.security.models import SecurityReport

        c = self.market
        sec = SecurityReport(token, T0, sources={"rugcheck": "ok", "sell_route": "ok"}, sell_route_ok=True) if assess else None
        return Candidate(token, c.features, moonshot_score(c.features), sec, None, c.candles_1m, (),
                         self.age, self.age > 180)


class FakeQuoter:
    async def round_trip_cost_bps(self, address, size_usd):
        return Decimal(50)


async def build(db: str, clock: FakeClock, quotes: StaticQuotes, fx) -> tuple[TradingEngine, FakeScanner]:
    settings = Settings(_env_file=None, database_url=db)
    repo = Repository(db)
    provider = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    session = PaperTradingSession(settings, repo, quotes, fx, provider=provider, clock=clock)
    scanner = FakeScanner()
    eng = TradingEngine(session, FakeIngestion(), scanner, Notifier(repo, clock=clock), quoter=FakeQuoter(),
                        config=EngineConfig(min_opportunity=30, min_moonshot=0), clock=clock)
    await eng.start()
    return eng, scanner


@pytest.fixture
def db(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}"


async def test_full_lifecycle(db, fx) -> None:
    clock = FakeClock(T0 + timedelta(hours=2))
    quotes = StaticQuotes()
    path, vols = breakout_path()
    quotes.set(TOK, str(path[-1]), "400000")
    eng, scanner = await build(db, clock, quotes, fx)
    scanner.market = ctx(bars(path, vols))

    # Autonomous mode off: approved entries wait for confirmation, nothing trades.
    await eng.scan_and_enter()
    assert not eng.session.ledger.open_positions()
    assert any(e.kind == "notify:awaiting_confirmation" for e in await eng.session.repo.events())

    # Operator enables autonomous mode via the dashboard command queue; bad risk change rejected.
    await enqueue_command(eng.store, "autonomous", {"enabled": True}, "dashboard", clock())
    await enqueue_command(eng.store, "set_risk", {"max_risk_per_trade_pct": "5"}, "dashboard", clock())
    await enqueue_command(eng.store, "set_risk", {"max_open_positions": 2}, "dashboard", clock())
    await eng.process_commands()
    assert eng.session.controls.autonomous
    assert eng.session.settings.risk.max_open_positions == 2 and eng.session.risk.limits.max_open_positions == 2
    assert eng.session.settings.risk.max_risk_per_trade_pct == Decimal("1.0")  # 5% refused by hard ceiling

    await eng.scan_and_enter()
    [pos] = eng.session.ledger.open_positions()
    st = eng.exit_states[pos.position_id]
    assert st.strategy == "C_volume_breakout" and st.stop < st.entry_price
    assert (await eng.store.load_state(f"exit:{pos.position_id}"))["position_id"] == pos.position_id
    decisions = await eng.session.repo.decisions()
    approved = [d for d in decisions if d.approved][-1]
    assert approved.evidence["cost_source"] == "jupiter_exact" and approved.evidence["strategy"] == "C_volume_breakout"

    # Price collapses through the stop: the fast loop exits and records the trade.
    clock.advance(minutes=4)
    crash = [x * 0.85 for x in path]
    quotes.set(TOK, str(crash[-1]), "400000")
    scanner.market = ctx(bars(crash))
    await eng.manage_positions()
    assert not eng.session.ledger.open_positions()
    [rec] = await load_trade_records(eng.store, "paper")
    assert rec.data["exit_reason"] == "stop_loss_hit" and Decimal(rec.data["net_pnl_eur"]) < 0
    kinds = [e.kind for e in await eng.session.repo.events()]
    assert {"notify:entry", "notify:exit", "notify:awaiting_confirmation"} <= set(kinds)

    # Next local day: the daily report is produced from recorded trades.
    clock.advance(hours=24)
    md = await eng.maybe_daily_report()
    assert md is not None and "PAPER (simulated funds)" in md and "C_volume_breakout" in md
    await eng.session.repo.close()


async def test_emergency_stop_survives_restart_and_blocks_entries(db, fx) -> None:
    clock = FakeClock(T0 + timedelta(hours=2))
    quotes = StaticQuotes()
    path, vols = breakout_path()
    quotes.set(TOK, str(path[-1]), "400000")
    eng, scanner = await build(db, clock, quotes, fx)
    await enqueue_command(eng.store, "autonomous", {"enabled": True}, "dashboard", clock())
    await enqueue_command(eng.store, "emergency_stop", {"reason": "test"}, "dashboard", clock())
    await eng.process_commands()
    await eng.session.repo.close()

    eng2, scanner2 = await build(db, clock, quotes, fx)
    scanner2.market = ctx(bars(path, vols))
    assert eng2.session.controls.emergency_stop and not eng2.session.controls.autonomous
    await eng2.scan_and_enter()
    assert not eng2.session.ledger.open_positions()
    await eng2.session.repo.close()


async def test_loop_errors_are_reported_not_fatal(db, fx) -> None:
    clock = FakeClock(T0 + timedelta(hours=2))
    eng, _ = await build(db, clock, StaticQuotes(), fx)

    async def boom():
        raise RuntimeError("provider exploded")

    await eng._safe("scan", boom)
    assert any(e.kind == "notify:system_error" and "provider exploded" in e.detail for e in await eng.session.repo.events())
    await eng.session.repo.close()


async def test_unknown_and_malformed_commands_are_rejected(db, fx) -> None:
    from atomtrader.persistence.trading_store import CommandRow
    from sqlalchemy import select

    clock = FakeClock(T0)
    eng, _ = await build(db, clock, StaticQuotes(), fx)
    await enqueue_command(eng.store, "withdraw_funds", {}, "dashboard", clock())
    await enqueue_command(eng.store, "pause_chain", {"chain": "tron"}, "dashboard", clock())
    await enqueue_command(eng.store, "pause_chain", {"chain": "base"}, "dashboard", clock())
    await eng.process_commands()
    async with eng.store._session() as s:
        rows = list((await s.execute(select(CommandRow).order_by(CommandRow.id))).scalars())
    assert [r.status for r in rows] == ["rejected", "rejected", "done"]
    assert Chain.BASE in eng.session.controls.paused_chains
    await eng.session.repo.close()


async def test_one_failing_candidate_does_not_abort_scan(db, fx) -> None:
    clock = FakeClock(T0 + timedelta(hours=2))
    eng, scanner = await build(db, clock, StaticQuotes(), fx)
    path, vols = breakout_path()
    scanner.market = ctx(bars(path, vols))

    async def broken(pre):
        raise RuntimeError("bad token data")

    eng._consider = broken
    await eng.scan_and_enter()  # must not raise
    assert any(e.kind == "candidate_failed" for e in await eng.session.repo.events())
    await eng.session.repo.close()
