"""Paper-trading session: the full decision -> risk -> execution -> accounting path with
simulated fills on live market data.

Invariants:
  * refuses to run unless ``settings.mode`` is PAPER; every figure it reports is tagged
    ``AccountKind.PAPER``
  * every entry candidate is risk-evaluated and logged, approved or not
  * with autonomous mode off, approved entries wait for explicit operator confirmation
  * exits are never blocked by controls (an emergency stop still lets positions close)
  * new entries are refused while any open position cannot be priced
  * controls and the daily risk state are persisted after every change and restored on
    start, so an emergency stop or a daily-loss halt survives a restart
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from atomtrader.config.settings import Settings, TradingMode
from atomtrader.core.models import AccountKind, Chain, OrderRequest, OrderStatus, Side, TokenRef
from atomtrader.core.money import FxTable, MissingMarketData
from atomtrader.execution.base import ExecutionProvider, QuoteSource
from atomtrader.execution.engine import ExecutionEngine, SubmitOutcome
from atomtrader.execution.paper import PaperExecutionProvider
from atomtrader.logging import get_logger
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.trading_store import EquitySnapshotRow, PositionMetaRow, TradingStore
from atomtrader.portfolio.ledger import Ledger
from atomtrader.risk.controls import TradingControls
from atomtrader.risk.engine import OpenPositionView, PortfolioView, RiskDecision, RiskEngine, TradeIntent
from atomtrader.risk.state import DailyRiskState

log = get_logger("paper")
ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    at: datetime
    account_kind: AccountKind
    cash_eur: Decimal
    invested_eur: Decimal | None  # None when an open position could not be priced
    equity_eur: Decimal | None
    realized_pnl_eur: Decimal
    unrealized_pnl_eur: Decimal | None
    total_fees_eur: Decimal
    open_positions: int
    unpriced: tuple[str, ...]

    @property
    def label(self) -> str:
        return "PAPER — simulated funds" if self.account_kind is AccountKind.PAPER else "LIVE"


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: str  # "rejected" | "awaiting_confirmation" | "filled" | "failed" | "not_executed"
    decision: RiskDecision
    position_id: str | None = None
    outcome: SubmitOutcome | None = None


class PaperTradingSession:
    CONTROLS_KEY = "controls:paper"
    RISK_KEY = "risk_state:paper"

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        quotes: QuoteSource,
        fx: FxTable,
        provider: ExecutionProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        seed: int | None = None,
    ) -> None:
        if settings.mode is not TradingMode.PAPER:
            raise RuntimeError("PaperTradingSession only runs in paper mode")
        self.settings = settings
        self.repo = repo
        self.store = TradingStore(repo.sessionmaker)
        self.quotes = quotes
        self.fx = fx
        self.clock = clock
        self.provider = provider or PaperExecutionProvider(quotes, fx, seed=seed, clock=clock)
        if self.provider.account_kind is not AccountKind.PAPER:
            raise RuntimeError("paper session requires a paper execution provider")
        self.ledger = Ledger(AccountKind.PAPER, settings.starting_capital_eur)
        self.controls = TradingControls()
        self.state = DailyRiskState(timezone=settings.timezone)
        self.risk: RiskEngine | None = None
        self.exec: ExecutionEngine | None = None
        self.meta: dict[str, PositionMetaRow] = {}
        self._risk_events_flushed = 0
        self._control_events_flushed = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> list[str]:
        """Init storage, rebuild the ledger from fills, restore controls/risk state and
        reconcile in-flight orders. Returns order ids whose outcome is still unknown."""
        await self.repo.init()
        results = await self.repo.load_final_results(AccountKind.PAPER)
        self.ledger = Ledger.replay(AccountKind.PAPER, self.settings.starting_capital_eur, results)
        if (c := await self.store.load_state(self.CONTROLS_KEY)) is not None:
            self.controls = TradingControls.from_dict(c)
        if (r := await self.store.load_state(self.RISK_KEY)) is not None:
            self.state = DailyRiskState.from_dict(r)
        self.meta = await self.store.position_meta()
        self.risk = RiskEngine(self.settings.risk, self.controls, self.state, self.settings.enabled_chains)
        self.exec = ExecutionEngine(self.provider, self.repo, self.ledger, self.clock)
        unresolved = await self.exec.reconcile_on_startup()
        await self.repo.log_event(self.clock(), "session_start",
                                  f"paper; open_positions={len(self.ledger.open_positions())}; unresolved={unresolved}")
        await self._persist()
        return unresolved

    async def _persist(self) -> None:
        now = self.clock()
        await self.store.save_state(self.CONTROLS_KEY, self.controls.to_dict(), now)
        await self.store.save_state(self.RISK_KEY, self.state.to_dict(), now)
        for ev in self.state.events[self._risk_events_flushed:]:
            await self.repo.log_event(ev.at, f"risk:{ev.kind}", ev.detail)
        self._risk_events_flushed = len(self.state.events)
        for ev in self.controls.events[self._control_events_flushed:]:
            await self.repo.log_event(ev.at, f"control:{ev.action}", ev.detail, ev.actor)
        self._control_events_flushed = len(self.controls.events)

    def _require_started(self) -> tuple[RiskEngine, ExecutionEngine]:
        if self.risk is None or self.exec is None:
            raise RuntimeError("call start() first")
        return self.risk, self.exec

    # ------------------------------------------------------------ pricing
    async def _price_eur(self, token: TokenRef, now: datetime) -> Decimal:
        quote = await self.quotes.get_quote(token)
        return self.fx.get("USD", now).to_eur(quote.price_usd)

    async def _priced_positions(self, now: datetime) -> tuple[dict[str, Decimal], list[str]]:
        prices: dict[str, Decimal] = {}
        unpriced: list[str] = []
        for p in self.ledger.open_positions():
            try:
                prices[p.position_id] = await self._price_eur(p.token, now)
            except MissingMarketData:
                unpriced.append(p.position_id)
        return prices, unpriced

    async def portfolio_view(self, now: datetime) -> PortfolioView:
        prices, unpriced = await self._priced_positions(now)
        if unpriced:
            raise MissingMarketData(f"open positions without price: {unpriced}")
        usd = self.fx.get("USD", now)
        views: list[OpenPositionView] = []
        invested = ZERO
        for p in self.ledger.open_positions():
            px = prices[p.position_id]
            value = p.quantity * px
            invested += value
            m = self.meta.get(p.position_id)
            if m is None:
                # Unknown stop: treat the whole position as at risk (conservative).
                risk_to_stop = value
                narrative = None
            else:
                stop_eur = usd.to_eur(m.stop_price_usd)
                exit_cost = value * m.est_fee_pct_per_side / HUNDRED + m.est_network_fee_eur
                risk_to_stop = p.quantity * max(px - stop_eur, ZERO) + exit_cost
                narrative = m.narrative
            views.append(OpenPositionView(p.token, narrative, value, risk_to_stop))
        return PortfolioView(self.ledger.cash_eur + invested, self.ledger.cash_eur, tuple(views))

    async def snapshot(self, record: bool = True) -> AccountSnapshot:
        now = self.clock()
        prices, unpriced = await self._priced_positions(now)
        open_pos = self.ledger.open_positions()
        if unpriced:
            invested = equity = unrealized = None
        else:
            invested = sum((p.quantity * prices[p.position_id] for p in open_pos), ZERO)
            equity = self.ledger.cash_eur + invested
            unrealized = sum((p.quantity * prices[p.position_id] - p.cost_basis_eur for p in open_pos), ZERO)
        snap = AccountSnapshot(
            at=now,
            account_kind=AccountKind.PAPER,
            cash_eur=self.ledger.cash_eur,
            invested_eur=invested,
            equity_eur=equity,
            realized_pnl_eur=self.ledger.realized_pnl_eur(),
            unrealized_pnl_eur=unrealized,
            total_fees_eur=self.ledger.total_fees_eur,
            open_positions=len(open_pos),
            unpriced=tuple(unpriced),
        )
        if record:
            await self.store.save_equity_snapshot(EquitySnapshotRow(
                at=now, account_kind=AccountKind.PAPER.value, cash_eur=snap.cash_eur, invested_eur=invested,
                equity_eur=equity, realized_pnl_eur=snap.realized_pnl_eur, unrealized_pnl_eur=unrealized,
                total_fees_eur=snap.total_fees_eur, open_positions=snap.open_positions,
                fully_priced=not unpriced, unpriced=list(unpriced)))
        return snap

    # ------------------------------------------------------------ trading
    async def _log_decision(self, intent: TradeIntent, d: RiskDecision, evidence: dict[str, Any], now: datetime) -> None:
        await self.repo.log_decision(
            at=now, token=intent.token, strategy=intent.strategy, approved=d.approved,
            notional_eur=d.notional_eur, risk_eur=d.risk_eur, reasons=list(d.reasons),
            evidence={**evidence, "caps": d.caps, "binding": d.binding_constraint,
                      "confidence": intent.confidence, "opportunity_score": intent.opportunity_score},
            account_kind=AccountKind.PAPER,
        )

    async def enter(self, intent: TradeIntent, evidence: dict[str, Any], confirm: bool = False) -> EntryResult:
        risk, ex = self._require_started()
        now = self.clock()
        try:
            pv = await self.portfolio_view(now)
        except MissingMarketData as exc:
            d = RiskDecision.reject("open_position_unpriced", caps={"detail": str(exc)[:200]})
            await self._log_decision(intent, d, evidence, now)
            return EntryResult("rejected", d)

        d = risk.evaluate(intent, pv, now)
        await self._log_decision(intent, d, evidence, now)
        if not d.approved:
            await self._persist()
            return EntryResult("rejected", d)
        if not (self.controls.autonomous or confirm):
            await self._persist()
            return EntryResult("awaiting_confirmation", d)

        position_id = f"pp-{uuid.uuid4().hex[:16]}"
        order = OrderRequest(
            client_order_id=f"{position_id}-entry", position_id=position_id, token=intent.token,
            side=Side.BUY, max_slippage_bps=self.settings.risk.max_slippage_bps,
            reason=f"{intent.strategy}: entry", notional_eur=d.notional_eur,
        )
        failed_before = self.ledger.failed_tx_fees_eur
        outcome = await ex.submit(order, max_attempts=1)  # entries don't chase price
        self._book_failed_fees(failed_before, now)
        status = {OrderStatus.FILLED: "filled", OrderStatus.FAILED: "failed"}.get(outcome.result.status, "not_executed")
        if outcome.result.status is OrderStatus.FILLED:
            self.state.record_entry()
            meta = PositionMetaRow(
                position_id=position_id, account_kind=AccountKind.PAPER.value, chain=intent.token.chain.value,
                token_address=intent.token.address, symbol=intent.token.symbol, strategy=intent.strategy,
                narrative=intent.narrative, stop_price_usd=intent.stop_price_usd,
                est_fee_pct_per_side=intent.est_fee_pct_per_side, est_network_fee_eur=intent.est_network_fee_eur,
                thesis={k: str(v) for k, v in evidence.items()}, opened_at=now,
            )
            await self.store.save_position_meta(meta)
            self.meta[position_id] = meta
        log.info("paper_entry", status=status, token=intent.token.key, notional_eur=str(d.notional_eur),
                 position_id=position_id, error=outcome.result.error)
        await self._persist()
        return EntryResult(status, d, position_id, outcome)

    async def exit(self, position_id: str, reason: str, fraction: Decimal = Decimal(1)) -> SubmitOutcome:
        _, ex = self._require_started()
        if not (ZERO < fraction <= 1):
            raise ValueError("fraction must be in (0, 1]")
        pos = self.ledger.positions.get(position_id)
        if pos is None or not pos.is_open:
            raise ValueError(f"no open position {position_id}")
        now = self.clock()
        qty = pos.quantity if fraction == 1 else pos.quantity * fraction
        prior_exits = {oid.rsplit(".a", 1)[0] for oid in self.ledger.applied_order_ids
                       if oid.startswith(f"{position_id}-x")}
        order = OrderRequest(
            client_order_id=f"{position_id}-x{len(prior_exits) + 1}", position_id=position_id, token=pos.token,
            side=Side.SELL, max_slippage_bps=self.settings.risk.max_slippage_bps, reason=reason, quantity=qty,
        )
        failed_before = self.ledger.failed_tx_fees_eur
        outcome = await ex.submit(order, max_attempts=3)  # exits retry (bounded)
        self._book_failed_fees(failed_before, now)
        if outcome.closed_trade is not None:
            L = self.settings.risk
            self.state.record_closed_trade(outcome.closed_trade.net_pnl_eur, now, L.max_daily_loss_pct,
                                           L.consecutive_losses_for_cooldown, L.cooldown_minutes)
        if outcome.result.status is not OrderStatus.FILLED:
            await self.repo.log_event(now, "exit_failed", f"{position_id}: {outcome.result.error}")
        log.info("paper_exit", position_id=position_id, status=outcome.result.status.value, reason=reason,
                 closed=outcome.closed_trade is not None)
        await self._persist()
        return outcome

    def _book_failed_fees(self, before: Decimal, now: datetime) -> None:
        burned = self.ledger.failed_tx_fees_eur - before
        if burned > 0:
            self.state.record_failed_tx_fee(burned, now, self.settings.risk.max_daily_loss_pct)

    # ------------------------------------------------------------ controls (persisted)
    async def emergency_stop(self, reason: str, actor: str = "operator") -> None:
        self.controls.trigger_emergency_stop(self.clock(), reason, actor)
        await self._persist()

    async def clear_emergency_stop(self, actor: str = "operator") -> None:
        self.controls.clear_emergency_stop(self.clock(), actor)
        await self._persist()

    async def pause(self, actor: str = "operator") -> None:
        self.controls.pause(self.clock(), actor)
        await self._persist()

    async def resume(self, actor: str = "operator") -> None:
        self.controls.resume(self.clock(), actor)
        await self._persist()

    async def set_autonomous(self, enabled: bool, actor: str = "operator") -> None:
        self.controls.set_autonomous(enabled, self.clock(), actor)
        await self._persist()

    async def pause_chain(self, chain: Chain, actor: str = "operator") -> None:
        self.controls.pause_chain(chain, self.clock(), actor)
        await self._persist()

    async def resume_chain(self, chain: Chain, actor: str = "operator") -> None:
        self.controls.resume_chain(chain, self.clock(), actor)
        await self._persist()

    async def blacklist(self, token: TokenRef, reason: str, actor: str = "operator") -> None:
        self.controls.blacklist_token(token, self.clock(), reason, actor)
        await self._persist()
