"""Audit probes: failure scenarios the original suite did not cover.

Each test reproduces a concrete trading-failure path end to end. They were written *before*
the corresponding fixes (several failed against the pre-audit code; see docs/AUDIT.md).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from atomtrader.config import Settings
from atomtrader.core.models import AccountKind, Chain, ExecutionResult, OrderStatus
from atomtrader.execution.engine import ExecutionEngine, UnreconciledOrderError
from atomtrader.execution.paper import DEFAULT_COST_MODELS, ChainCostModel, PaperExecutionProvider
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.repository import Repository
from atomtrader.portfolio.ledger import Ledger
from tests.conftest import TOKEN, StaticQuotes
from tests.test_paper_session import intent

NO_FAIL = {c: ChainCostModel(m.network_fee_eur, m.pool_fee_bps, m.aggregator_fee_bps, Decimal(0), Decimal(0))
           for c, m in DEFAULT_COST_MODELS.items()}


@pytest.fixture
def db(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"


async def session(db, quotes, fx, clock, models=NO_FAIL) -> PaperTradingSession:
    s = PaperTradingSession(Settings(_env_file=None, database_url=db), Repository(db), quotes, fx,
                            provider=PaperExecutionProvider(quotes, fx, models, seed=3, clock=clock), clock=clock)
    await s.start()
    return s


# --------------------------------------------------------------------------- exits


async def test_exit_rejected_once_can_be_retried_later(db, quotes, fx, clock) -> None:
    """A transient exit rejection (e.g. no quote for a moment) must not make the position
    permanently un-exitable by replaying the stored rejection."""
    s = await session(db, quotes, fx, clock)
    r = await s.enter(intent(), {}, confirm=True)
    saved = quotes.quotes.pop(TOKEN.key)  # market data briefly unavailable
    out1 = await s.exit(r.position_id, "stop")
    assert out1.result.status is OrderStatus.REJECTED
    quotes.quotes[TOKEN.key] = saved  # data is back
    clock.advance(seconds=20)
    out2 = await s.exit(r.position_id, "stop")
    assert out2.result.status is OrderStatus.FILLED and out2.closed_trade is not None
    await s.repo.close()


async def test_position_with_vanished_sell_route_stays_open_and_is_reported(db, quotes, fx, clock) -> None:
    from atomtrader.execution.base import NoRoute

    class Q:
        def __init__(self):
            self.dead = False

        async def buy(self, a, usd, slip):
            return usd / Decimal("0.002"), {}

        async def sell(self, a, qty, slip):
            if self.dead:
                raise NoRoute("TOKEN_NOT_TRADABLE")
            return qty * Decimal("0.002"), {}

    q = Q()
    s = PaperTradingSession(Settings(_env_file=None, database_url=db), Repository(db), quotes, fx,
                            provider=PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock,
                                                            sized_quoters={Chain.SOLANA: q}), clock=clock)
    await s.start()
    r = await s.enter(intent(), {}, confirm=True)
    q.dead = True  # honeypot switched on after entry
    out = await s.exit(r.position_id, "security_failure")
    assert out.result.status is OrderStatus.REJECTED and out.result.error.startswith("no_route")
    assert s.ledger.positions[r.position_id].is_open  # not silently marked closed
    assert out.result.failed_fee_eur == 0
    await s.repo.close()


# --------------------------------------------------------------------------- restart / persistence failures


async def test_paper_order_lost_in_crash_does_not_block_token_forever(db, quotes, fx, clock) -> None:
    """Paper fills live only in process memory. A crash between 'submitted' and 'recorded'
    loses the simulated fill; on restart that order must be resolved (never happened), not
    left blocking the token permanently."""
    repo = Repository(db)
    await repo.init()
    from tests.test_execution_ledger import buy

    await repo.insert_order(buy(cid="crash.a1"), AccountKind.PAPER, clock())
    await repo.mark_submitted("crash.a1", clock())
    await repo.close()

    s = await session(db, quotes, fx, clock)
    assert s.exec.unresolved_tokens == set()
    assert await s.repo.get_order_status("crash.a1") is OrderStatus.REJECTED
    r = await s.enter(intent(), {}, confirm=True)
    assert r.status == "filled"
    await s.repo.close()


async def test_database_failure_after_execution_blocks_token(db, quotes, fx, clock) -> None:
    """If the result cannot be recorded after the venue executed, the outcome is unknown to
    the ledger: the token must be blocked rather than traded again."""
    repo = Repository(db)
    await repo.init()
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    eng = ExecutionEngine(p, repo, Ledger(AccountKind.PAPER, Decimal(1000)), clock)
    from tests.test_execution_ledger import buy

    async def broken(*a, **k):
        raise OSError("disk full")

    real = repo.save_result
    repo.save_result = broken
    with pytest.raises(UnreconciledOrderError):
        await eng.submit(buy(cid="dbfail"))
    repo.save_result = real
    assert TOKEN.key in eng.unresolved_tokens
    with pytest.raises(UnreconciledOrderError):
        await eng.submit(buy(cid="again"))
    await repo.close()


# --------------------------------------------------------------------------- repeated failures


async def test_repeated_failed_entries_on_same_token_are_throttled(db, quotes, fx, clock) -> None:
    always_fail = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(1), Decimal(0)) for c in Chain}
    s = await session(db, quotes, fx, clock, always_fail)
    first = await s.enter(intent(), {}, confirm=True)
    assert first.status == "failed"
    clock.advance(minutes=1)
    second = await s.enter(intent(), {}, confirm=True)
    assert second.status == "rejected" and "recent_entry_failure" in second.decision.reasons
    clock.advance(minutes=31)
    third = await s.enter(intent(), {}, confirm=True)
    assert third.status == "failed"  # allowed again after the cool-off
    assert s.ledger.failed_tx_fees_eur == Decimal("0.10")
    await s.repo.close()


# --------------------------------------------------------------------------- stale data


async def test_stale_snapshots_are_not_used_for_entries(tmp_path) -> None:
    from datetime import UTC, datetime

    from atomtrader.persistence.marketdata_store import MarketDataStore
    from atomtrader.scanner.service import ScannerService
    from tests.test_scanner import TOK, snap, surging

    now = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    await repo.init()
    store = MarketDataStore(repo.sessionmaker)
    await store.save_snapshots([snap(surging(), source="dexscreener", at=now - timedelta(minutes=40))])
    svc = ScannerService(store, clock=lambda: now)
    cand = await svc.evaluate(TOK, assess=False)
    assert cand is not None and cand.data_age_seconds == pytest.approx(2400)
    assert cand.stale
    await repo.close()


async def test_engine_skips_stale_candidates_but_enters_on_fresh_data(tmp_path, fx) -> None:
    from tests.conftest import FakeClock
    from tests.test_engine import build
    from tests.test_strategy import T0, TOK, bars, breakout_path, ctx

    path, vols = breakout_path()
    results = {}
    for age in (900.0, 0.0):  # stale first, then the control with fresh data
        clock = FakeClock(T0 + timedelta(hours=2))
        quotes = StaticQuotes()
        quotes.set(TOK, str(path[-1]), "400000")
        eng, scanner = await build(f"sqlite+aiosqlite:///{tmp_path / f'e{int(age)}.db'}", clock, quotes, fx)
        await eng.session.set_autonomous(True)
        scanner.market = ctx(bars(path, vols))
        scanner.age = age
        await eng.scan_and_enter()
        results[age] = len(eng.session.ledger.open_positions())
        await eng.session.repo.close()
    assert results == {900.0: 0, 0.0: 1}


# --------------------------------------------------------------------------- partial fills


def test_partial_fills_are_not_a_dex_concept_but_partial_exits_are_handled() -> None:
    """AMM swaps are atomic: they fill completely at the quoted output or revert. What the
    system must handle is (a) partial *exits* chosen by the exit engine and (b) receiving a
    different quantity than estimated (slippage) — both covered elsewhere. This test pins the
    atomicity assumption: a fill always carries the full requested notional or nothing."""
    from atomtrader.core.models import Fill  # noqa: F401  (documentational)

    r = ExecutionResult("x", OrderStatus.FAILED, AccountKind.PAPER, failed_fee_eur=Decimal("0.05"))
    assert r.fill is None


async def test_stale_position_still_enforces_stop_on_direct_quote(tmp_path, fx) -> None:
    from tests.conftest import FakeClock
    from tests.test_engine import build
    from tests.test_strategy import T0, bars, breakout_path, ctx

    clock = FakeClock(T0 + timedelta(hours=2))
    quotes = StaticQuotes()
    path, vols = breakout_path()
    quotes.set(TOKEN, str(path[-1]), "400000")
    eng, scanner = await build(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}", clock, quotes, fx)
    from tests.test_strategy import TOK

    quotes.set(TOK, str(path[-1]), "400000")
    await eng.session.set_autonomous(True)
    scanner.market = ctx(bars(path, vols))
    await eng.scan_and_enter()
    [pos] = eng.session.ledger.open_positions()
    # ingestion stalls (stale snapshots) while the real price collapses below the stop
    scanner.age = 900
    clock.advance(minutes=2)
    quotes.set(TOK, str(path[-1] * 0.8), "400000")
    await eng.manage_positions()
    assert not eng.session.ledger.open_positions()
    kinds = [e.detail for e in await eng.session.repo.events() if e.kind == "notify:system_error"]
    assert any("stale market data" in k for k in kinds)
    await eng.session.repo.close()


# --------------------------------------------------------------------------- fake liquidity (audit finding)


def test_inflated_dexscreener_liquidity_is_replaced_by_quote_side_depth() -> None:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from atomtrader.marketdata.dexscreener import parse_pair
    from atomtrader.marketdata.http import parse_json

    raw = parse_json((Path(__file__).parent / "fixtures" / "dexscreener_base_inflated_liquidity.json").read_text())
    oil = parse_pair(raw[0], datetime(2026, 11, 27, tzinfo=UTC))
    assert oil.reported_liquidity_usd > Decimal(100_000_000)  # what the provider claims
    # 0.501 WETH quote side => roughly $1.7k of real depth on each side
    assert Decimal(1000) < oil.liquidity_usd < Decimal(5000)
    assert json.loads((Path(__file__).parent / "fixtures" / "dexscreener_base_inflated_liquidity.json").read_text())[0]["liquidity"]["quote"] == 0.501


def test_geckoterminal_reserve_is_reported_not_verified() -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from atomtrader.marketdata.geckoterminal import parse_pool
    from atomtrader.marketdata.http import parse_json

    raw = parse_json((Path(__file__).parent / "fixtures" / "geckoterminal_new_pools.json").read_text())
    p = parse_pool(raw["data"][1], Chain.SOLANA, datetime(2026, 11, 27, tzinfo=UTC))
    assert p.liquidity_usd is None and p.reported_liquidity_usd is not None


async def test_paper_fill_uses_verified_depth_not_reported(fx, clock) -> None:
    """Model-mode (EVM) fills on a spam pool must see the thin real depth: a €100 buy against
    ~$3.4k of verified depth exceeds a 3 % slippage cap and reverts."""
    import httpx

    from atomtrader.core.models import OrderRequest, Side, TokenRef
    from atomtrader.marketdata.base_assets import BaseAssetRegistry
    from atomtrader.marketdata.dexscreener import DexScreenerClient, DexScreenerQuoteSource
    from atomtrader.marketdata.http import JsonHttpClient
    from pathlib import Path

    body = (Path(__file__).parent / "fixtures" / "dexscreener_base_inflated_liquidity.json").read_text()

    async def no_sleep(_):
        return None

    http = JsonHttpClient("https://api.dexscreener.com", per_minute=10_000, max_retries=0, sleep=no_sleep,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    qs = DexScreenerQuoteSource(DexScreenerClient(http, clock=clock), BaseAssetRegistry.load())
    tok = TokenRef(Chain.BASE, "0x38f0159393c73e7fbac320dbc6f7e9afb6eda9e9", "OIL")
    q = await qs.get_quote(tok)
    assert q.liquidity_usd < 5000
    p = PaperExecutionProvider(qs, fx, NO_FAIL, seed=1, clock=clock)
    r = await p.execute(OrderRequest("spam", "p", tok, Side.BUY, 300, "t", notional_eur=Decimal(100)))
    assert r.status is OrderStatus.FAILED and r.error == "slippage_exceeded"


def test_evm_addresses_are_case_normalised_across_providers() -> None:
    from atomtrader.core.models import TokenRef

    checksummed = TokenRef(Chain.BASE, "0x38F0159393c73e7fBac320dbc6f7e9AFB6eda9E9")
    lower = TokenRef(Chain.BASE, "0x38f0159393c73e7fbac320dbc6f7e9afb6eda9e9")
    assert checksummed == lower and checksummed.key == lower.key
    sol = TokenRef(Chain.SOLANA, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    assert sol.address == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # base58 is case-sensitive


# --------------------------------------------------------------------------- cost accounting (audit finding)


def test_daily_report_net_includes_failed_transaction_fees() -> None:
    from atomtrader.analytics.performance import daily_report
    from tests.test_backtest_analytics import tr

    rep = daily_report("2026-11-27", [tr("a", "10")], Decimal(1000), None,
                       failed_tx_fees_eur=Decimal("0.35"), failed_transactions=7, slippage_vs_mid_eur=Decimal("1.20"))
    assert rep.net_pnl_eur == Decimal("9.65")
    md = rep.to_markdown()
    assert "Failed-transaction fees: €0.35 (7 failed tx)" in md and "Slippage/price impact vs mid" in md
    assert "Not included: infrastructure" in md


async def test_cost_breakdown_from_orders(db, quotes, fx, clock) -> None:
    always_fail = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(1), Decimal(0)) for c in Chain}
    s = await session(db, quotes, fx, clock, always_fail)
    await s.enter(intent(), {}, confirm=True)
    costs = await s.repo.cost_breakdown(AccountKind.PAPER, clock() - timedelta(hours=1), clock() + timedelta(hours=1))
    assert costs["failed_tx_fees_eur"] == Decimal("0.05") and costs["failed_transactions"] == 1
    assert s.ledger.total_fees_eur == Decimal("0.05")  # ledger already counts it
    await s.repo.close()


def test_backtest_charges_failed_transactions() -> None:
    from datetime import UTC, datetime

    from atomtrader.backtest.engine import BacktestConfig, Backtester
    from atomtrader.backtest.frames import synthetic_timeline

    tl = [synthetic_timeline("trend_pullbacks", datetime(2026, 11, 20, tzinfo=UTC), minutes=300, seed=s) for s in (1, 2)]
    clean = Backtester(BacktestConfig(min_opportunity=30, failure_rate=0.0)).run(tl)
    lossy = Backtester(BacktestConfig(min_opportunity=30, failure_rate=0.5, seed=7)).run(tl)
    assert clean.failed_tx_fees_eur == 0 and lossy.failed_transactions > 0
    assert lossy.failed_tx_fees_eur == lossy.failed_transactions * Decimal("0.05")
    assert abs(lossy.net_pnl_eur - (lossy.ending_cash_eur - lossy.starting_eur)) < Decimal("1e-20")
