from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr

from atomtrader.config import Settings
from atomtrader.core.models import AccountKind, Chain, OrderStatus
from atomtrader.execution.paper import DEFAULT_COST_MODELS, ChainCostModel, PaperExecutionProvider
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.trading_store import TradingStore
from atomtrader.risk import TradeIntent
from tests.conftest import OTHER, TOKEN

NO_FAIL = {
    c: ChainCostModel(m.network_fee_eur, m.pool_fee_bps, m.aggregator_fee_bps, Decimal(0), Decimal(0))
    for c, m in DEFAULT_COST_MODELS.items()
}


def intent(token=TOKEN, **kw) -> TradeIntent:
    base = dict(
        token=token, strategy="volume_breakout",
        entry_price_usd=Decimal("0.002"), stop_price_usd=Decimal("0.0018"),
        pool_liquidity_eur=Decimal("368000"), est_entry_slippage_bps=Decimal(50),
        est_exit_slippage_bps=Decimal(50), est_fee_pct_per_side=Decimal("0.35"),
        est_network_fee_eur=Decimal("0.05"), confidence=Decimal("0.5"), narrative="test-narrative",
    )
    base.update(kw)
    return TradeIntent(**base)


async def make_session(db: str, quotes, fx, clock) -> PaperTradingSession:
    settings = Settings(_env_file=None, database_url=db)
    repo = Repository(db)
    provider = PaperExecutionProvider(quotes, fx, NO_FAIL, seed=3, clock=clock)
    s = PaperTradingSession(settings, repo, quotes, fx, provider=provider, clock=clock)
    assert await s.start() == []
    return s


@pytest.fixture
def db(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'paper.db'}"


async def test_refuses_live_mode(db, quotes, fx) -> None:
    live = Settings(_env_file=None, mode="live", live_authorization=SecretStr("x"))
    with pytest.raises(RuntimeError):
        PaperTradingSession(live, Repository(db), quotes, fx)


async def test_snapshot_is_labelled_paper_and_starts_at_capital(db, quotes, fx, clock) -> None:
    s = await make_session(db, quotes, fx, clock)
    snap = await s.snapshot()
    assert snap.account_kind is AccountKind.PAPER and snap.label.startswith("PAPER")
    assert snap.equity_eur == snap.cash_eur == Decimal(1000) and snap.open_positions == 0
    rows = await s.store.equity_snapshots()
    assert len(rows) == 1 and rows[0].account_kind == "paper"
    await s.repo.close()


async def test_non_autonomous_entries_wait_for_confirmation(db, quotes, fx, clock) -> None:
    s = await make_session(db, quotes, fx, clock)
    r = await s.enter(intent(), {"why": "test"})
    assert r.status == "awaiting_confirmation" and r.decision.approved and r.outcome is None
    assert s.ledger.cash_eur == Decimal(1000)
    [d] = await s.repo.decisions()
    assert d.approved and d.evidence["why"] == "test" and d.evidence["binding"] == r.decision.binding_constraint
    await s.repo.close()


async def test_full_round_trip_updates_ledger_risk_state_and_survives_restart(db, quotes, fx, clock) -> None:
    s = await make_session(db, quotes, fx, clock)
    r = await s.enter(intent(), {"why": "test"}, confirm=True)
    assert r.status == "filled" and r.position_id
    assert s.state.entries_today == 1
    snap = await s.snapshot()
    assert snap.open_positions == 1 and snap.equity_eur is not None and snap.equity_eur < Decimal(1000)  # costs

    clock.advance(minutes=15)
    quotes.set(TOKEN, "0.0017", "400000")  # stop breached -> losing exit
    out = await s.exit(r.position_id, "stop_hit")
    assert out.result.status is OrderStatus.FILLED and out.closed_trade is not None
    loss = out.closed_trade.net_pnl_eur
    assert loss < 0 and s.state.realized_net_pnl_eur == loss and s.state.consecutive_losses == 1
    await s.repo.close()

    # Restart: ledger rebuilt from fills, risk state restored from storage.
    s2 = await make_session(db, quotes, fx, clock)
    assert s2.ledger.cash_eur == s.ledger.cash_eur
    assert s2.state.entries_today == 1 and s2.state.realized_net_pnl_eur == loss
    assert s2.state.consecutive_losses == 1
    assert r.position_id in s2.meta and s2.meta[r.position_id].strategy == "volume_breakout"
    await s2.repo.close()


async def test_emergency_stop_persists_and_still_allows_exits(db, quotes, fx, clock) -> None:
    s = await make_session(db, quotes, fx, clock)
    r = await s.enter(intent(), {}, confirm=True)
    await s.emergency_stop("operator test")
    await s.repo.close()

    s2 = await make_session(db, quotes, fx, clock)
    assert s2.controls.emergency_stop and not s2.controls.autonomous
    blocked = await s2.enter(intent(token=OTHER), {}, confirm=True)
    assert blocked.status == "rejected" and "emergency_stop" in blocked.decision.reasons
    out = await s2.exit(r.position_id, "emergency flatten")
    assert out.result.status is OrderStatus.FILLED and out.closed_trade is not None
    kinds = [e.kind for e in await s2.repo.events()]
    assert "control:emergency_stop" in kinds
    await s2.repo.close()


async def test_unpriced_open_position_blocks_entries_and_is_not_valued(db, quotes, fx, clock) -> None:
    s = await make_session(db, quotes, fx, clock)
    r = await s.enter(intent(), {}, confirm=True)
    del quotes.quotes[TOKEN.key]  # data source loses the token
    snap = await s.snapshot()
    assert snap.equity_eur is None and snap.unpriced == (r.position_id,)
    quotes.set(OTHER, "1.0", "500000")
    blocked = await s.enter(intent(token=OTHER, entry_price_usd=Decimal("1"), stop_price_usd=Decimal("0.9")), {}, confirm=True)
    assert blocked.status == "rejected" and blocked.decision.reasons == ("open_position_unpriced",)
    await s.repo.close()


async def test_failed_entry_fee_counts_toward_daily_loss(db, quotes, fx, clock) -> None:
    always_fail = {c: ChainCostModel(Decimal("0.05"), Decimal(25), Decimal(10), Decimal(1), Decimal(0)) for c in Chain}
    settings = Settings(_env_file=None, database_url=db)
    repo = Repository(db)
    s = PaperTradingSession(settings, repo, quotes, fx,
                            provider=PaperExecutionProvider(quotes, fx, always_fail, seed=1, clock=clock), clock=clock)
    await s.start()
    r = await s.enter(intent(), {}, confirm=True)
    assert r.status == "failed"
    assert s.state.realized_net_pnl_eur == Decimal("-0.05") and s.state.entries_today == 0
    stored = await TradingStore(repo.sessionmaker).load_state(s.RISK_KEY)
    assert stored is not None and stored["realized_net_pnl_eur"] == "-0.05"
    await repo.close()
