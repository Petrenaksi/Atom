from __future__ import annotations

from decimal import Decimal

import pytest

from atomtrader.core.models import (
    AccountKind,
    Chain,
    ExecutionResult,
    OrderRequest,
    OrderStatus,
    Side,
)
from atomtrader.execution.engine import ExecutionEngine, UnreconciledOrderError
from atomtrader.execution.paper import (
    DEFAULT_COST_MODELS,
    ChainCostModel,
    PaperExecutionProvider,
    amm_buy_impact,
    amm_sell_impact,
)
from atomtrader.persistence.repository import Repository
from atomtrader.portfolio.ledger import Ledger, LedgerError
from tests.conftest import OTHER, TOKEN

NO_FAIL = {
    chain: ChainCostModel(m.network_fee_eur, m.pool_fee_bps, m.aggregator_fee_bps, Decimal(0), Decimal(0))
    for chain, m in DEFAULT_COST_MODELS.items()
}
ALWAYS_FAIL = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(1), Decimal(0)) for c in Chain}


def buy(cid: str = "o1", eur: str = "100", slip: int = 300, pos: str = "p1", token=TOKEN) -> OrderRequest:
    return OrderRequest(cid, pos, token, Side.BUY, slip, "test entry", notional_eur=Decimal(eur))


def sell(qty: Decimal, cid: str = "s1", slip: int = 300, pos: str = "p1") -> OrderRequest:
    return OrderRequest(cid, pos, TOKEN, Side.SELL, slip, "test exit", quantity=qty)


# ------------------------------------------------------------------- AMM model


def test_amm_impact_formulas() -> None:
    assert amm_buy_impact(Decimal(100), Decimal(20000)) == Decimal("0.01")
    # Selling the same value has a smaller average impact than buying (x*y=k convexity).
    assert amm_sell_impact(Decimal(100), Decimal(20000)) < Decimal("0.01")


# ------------------------------------------------------------------- paper provider


async def test_paper_buy_prices_include_impact_and_fees(quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    r = await p.execute(buy(eur="200"))
    assert r.status is OrderStatus.FILLED and r.account_kind is AccountKind.PAPER
    f = r.fill
    assert f is not None
    spot_eur = Decimal("0.002") * Decimal("0.92")
    liq_eur = Decimal("400000") * Decimal("0.92")
    expected_price = spot_eur * (1 + Decimal(200) / (liq_eur / 2))
    assert abs(f.gross_eur / f.quantity - expected_price) < Decimal("1e-12")
    assert f.fee_eur == Decimal(200) * Decimal("0.0035") + Decimal("0.05")
    assert f.cash_delta_eur == -(Decimal(200) + f.fee_eur)


async def test_paper_is_idempotent_per_client_order_id(quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    a = await p.execute(buy())
    b = await p.execute(buy())
    assert a is b
    assert await p.lookup("o1") is a and await p.lookup("nope") is None


async def test_slippage_protection_reverts_but_burns_network_fee(quotes, fx, clock) -> None:
    quotes.set(TOKEN, "0.002", "30000")  # thin pool
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    r = await p.execute(buy(eur="500", slip=100))
    assert r.status is OrderStatus.FAILED and r.error == "slippage_exceeded"
    assert r.failed_fee_eur == Decimal("0.05") and r.fill is None
    assert r.realized_slippage_bps is not None and r.realized_slippage_bps > 100


async def test_missing_quote_is_rejected_not_fabricated(fx, clock) -> None:
    from tests.conftest import StaticQuotes

    p = PaperExecutionProvider(StaticQuotes(), fx, NO_FAIL, seed=1, clock=clock)
    r = await p.execute(buy())
    assert r.status is OrderStatus.REJECTED and "missing_market_data" in (r.error or "")
    assert r.failed_fee_eur == 0


async def test_random_failures_are_seeded_and_reproducible(quotes, fx, clock) -> None:
    models = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal("0.5"), Decimal(40)) for c in Chain}
    runs = []
    for _ in range(2):
        p = PaperExecutionProvider(quotes, fx, models, seed=42, clock=clock)
        runs.append([(await p.execute(buy(cid=f"o{i}"))).status for i in range(20)])
    assert runs[0] == runs[1]
    assert OrderStatus.FAILED in runs[0] and OrderStatus.FILLED in runs[0]


# ------------------------------------------------------------------- ledger


async def test_ledger_round_trip_partial_exits_and_fees(quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    r_buy = await p.execute(buy(eur="100"))
    ledger.apply(r_buy)
    pos = ledger.positions["p1"]
    qty = pos.quantity
    assert ledger.cash_eur == Decimal(1000) - Decimal(100) - r_buy.fill.fee_eur

    quotes.set(TOKEN, "0.003", "400000")  # +50%
    r1 = await p.execute(sell(qty / 4, cid="s1"))
    assert ledger.apply(r1) is None  # partial exit
    r2 = await p.execute(sell(qty - qty / 4, cid="s2"))
    closed = ledger.apply(r2)
    assert closed is not None and not pos.is_open
    fees = r_buy.fill.fee_eur + r1.fill.fee_eur + r2.fill.fee_eur
    expected = r1.fill.gross_eur + r2.fill.gross_eur - Decimal(100) - fees
    assert abs(closed.net_pnl_eur - expected) < Decimal("1e-20")
    assert closed.fees_eur == fees == ledger.total_fees_eur
    assert abs(ledger.cash_eur - (Decimal(1000) + expected)) < Decimal("1e-20")
    assert ledger.equity_eur({}) == ledger.cash_eur


async def test_ledger_guards(quotes, fx, clock) -> None:
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    failed = ExecutionResult("f1", OrderStatus.FAILED, AccountKind.PAPER, failed_fee_eur=Decimal("0.05"))
    ledger.apply(failed)
    ledger.apply(failed)  # applied once only
    assert ledger.cash_eur == Decimal("999.95") and ledger.realized_pnl_eur() == Decimal("-0.05")
    with pytest.raises(LedgerError):
        ledger.apply(ExecutionResult("x", OrderStatus.FILLED, AccountKind.LIVE))
    with pytest.raises(LedgerError):
        ledger.apply(ExecutionResult("y", OrderStatus.SUBMITTED, AccountKind.PAPER))
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    ledger.apply(await p.execute(buy()))
    with pytest.raises(LedgerError):  # unpriced open position is not silently valued
        ledger.equity_eur({})
    with pytest.raises(LedgerError):
        ledger.apply(await p.execute(sell(ledger.positions["p1"].quantity * 2, cid="too-much")))


# ------------------------------------------------------------------- engine + persistence


@pytest.fixture
async def repo(tmp_path):
    r = Repository(f"sqlite+aiosqlite:///{tmp_path / 'atom.db'}")
    await r.init()
    yield r
    await r.close()


async def test_engine_persists_and_restart_rebuilds_identical_ledger(repo, quotes, fx, clock, tmp_path) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=7, clock=clock)
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    eng = ExecutionEngine(p, repo, ledger, clock)
    out = await eng.submit(buy(eur="150"))
    assert out.result.status is OrderStatus.FILLED
    qty = ledger.positions["p1"].quantity
    clock.advance(minutes=12)
    quotes.set(TOKEN, "0.0021", "400000")
    out2 = await eng.submit(sell(qty / 2))
    assert out2.closed_trade is None

    # "Restart": new repository handle and ledger replayed from the database.
    repo2 = Repository(f"sqlite+aiosqlite:///{tmp_path / 'atom.db'}")
    replayed = Ledger.replay(AccountKind.PAPER, Decimal(1000), await repo2.load_final_results(AccountKind.PAPER))
    await repo2.close()
    assert replayed.cash_eur == ledger.cash_eur
    assert replayed.total_fees_eur == ledger.total_fees_eur
    rp, lp = replayed.positions["p1"], ledger.positions["p1"]
    assert (rp.quantity, rp.cost_basis_eur, rp.realized_pnl_eur) == (lp.quantity, lp.cost_basis_eur, lp.realized_pnl_eur)


async def test_resubmitting_same_order_never_trades_twice(repo, quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=7, clock=clock)
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    eng = ExecutionEngine(p, repo, ledger, clock)
    first = await eng.submit(buy())
    cash_after = ledger.cash_eur
    again = await eng.submit(buy())
    assert again.result.client_order_id == first.result.client_order_id
    assert ledger.cash_eur == cash_after
    assert len(await repo.load_final_results(AccountKind.PAPER)) == 1


async def test_retries_are_strictly_bounded_and_fees_booked(repo, quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, ALWAYS_FAIL, seed=1, clock=clock)
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    eng = ExecutionEngine(p, repo, ledger, clock)
    out = await eng.submit(buy(), max_attempts=3)
    assert out.result.status is OrderStatus.FAILED and out.attempts == 3
    assert ledger.cash_eur == Decimal(1000) - 3 * Decimal("0.05")
    assert not ledger.open_positions()
    with pytest.raises(ValueError):
        await eng.submit(buy(cid="o2"), max_attempts=10)


async def test_restart_reconciliation(repo, quotes, fx, clock) -> None:
    p = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=1, clock=clock)
    ledger = Ledger(AccountKind.PAPER, Decimal(1000))

    # Crash before send: PENDING row -> provably not sent -> rejected.
    await repo.insert_order(buy(cid="pending.a1"), AccountKind.PAPER, clock())
    # Crash after send, provider knows the outcome -> reconciled into the ledger.
    known = buy(cid="known.a1", pos="p2", token=TOKEN)
    await repo.insert_order(known, AccountKind.PAPER, clock())
    await repo.mark_submitted("known.a1", clock())
    await p.execute(known)
    # Crash after send, outcome unknown -> token blocked.
    lost = buy(cid="lost.a1", pos="p3", token=OTHER)
    await repo.insert_order(lost, AccountKind.PAPER, clock())
    await repo.mark_submitted("lost.a1", clock())

    eng = ExecutionEngine(p, repo, ledger, clock)
    unresolved = await eng.reconcile_on_startup()
    # Paper: a simulated fill lost in a crash never happened -> resolved, not blocking.
    assert unresolved == []
    assert await repo.get_order_status("pending.a1") is OrderStatus.REJECTED
    assert await repo.get_order_status("known.a1") is OrderStatus.FILLED
    assert await repo.get_order_status("lost.a1") is OrderStatus.REJECTED
    assert "p2" in ledger.positions
    assert [e.detail for e in await repo.events("paper_order_lost_in_restart")] == ["lost.a1"]


async def test_unknown_outcome_from_non_paper_provider_blocks_token(repo, clock) -> None:
    """Generic path a future live provider relies on: if the venue cannot tell us what
    happened, the token is blocked. (Test double only — no execution capability.)"""

    class UnknownOutcomeVenue:
        account_kind = AccountKind.LIVE

        async def execute(self, order):
            raise AssertionError("must not be called")

        async def lookup(self, cid):
            return None

    lost = buy(cid="lost.a1", pos="p3", token=OTHER)
    await repo.insert_order(lost, AccountKind.LIVE, clock())
    await repo.mark_submitted("lost.a1", clock())
    eng = ExecutionEngine(UnknownOutcomeVenue(), repo, Ledger(AccountKind.LIVE, Decimal(0)), clock)
    assert await eng.reconcile_on_startup() == ["lost.a1"]
    with pytest.raises(UnreconciledOrderError):
        await eng.submit(OrderRequest("new", "p4", OTHER, Side.BUY, 300, "x", notional_eur=Decimal(10)))
    assert [e.detail for e in await repo.events("unreconciled_order")] == ["lost.a1"]


async def test_provider_exception_marks_outcome_unknown(repo, fx, clock) -> None:
    class Exploding:
        account_kind = AccountKind.PAPER

        async def execute(self, order):
            raise TimeoutError("rpc timeout")

        async def lookup(self, cid):
            return None

    ledger = Ledger(AccountKind.PAPER, Decimal(1000))
    eng = ExecutionEngine(Exploding(), repo, ledger, clock)
    with pytest.raises(UnreconciledOrderError):
        await eng.submit(buy())
    assert await repo.get_order_status("o1.a1") is OrderStatus.SUBMITTED
    assert ledger.cash_eur == Decimal(1000)  # nothing assumed
    with pytest.raises(UnreconciledOrderError):
        await eng.submit(buy(cid="o9"))


async def test_decision_log_round_trip(repo, clock) -> None:
    await repo.log_decision(
        at=clock(),
        token=TOKEN,
        strategy="narrative_breakout",
        approved=False,
        notional_eur=Decimal(0),
        risk_eur=Decimal(0),
        reasons=["liquidity_below_minimum"],
        evidence={"liquidity_eur": Decimal("12000"), "unique_buyers_5m": None},
        account_kind=AccountKind.PAPER,
    )
    [row] = await repo.decisions()
    assert row.reasons == ["liquidity_below_minimum"]
    assert row.evidence == {"liquidity_eur": "12000", "unique_buyers_5m": None}
    assert row.at == clock()
