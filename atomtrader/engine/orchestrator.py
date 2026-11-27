"""Autonomous trading engine (paper mode).

Two cadences:
  * fast loop (``position_interval_s``): apply operator commands, then manage every open
    position — fresh snapshot, security/insider re-check, exit engine, execute partials and
    exits. Position management always runs first and never waits for scanning.
  * slow loop (``scan_interval_s``): FX, ingestion, narratives, cheap preliminary ranking,
    full security assessment of the best candidates, strategies, exact round-trip cost,
    Opportunity Score, then the risk engine via the paper session.

Every candidate that reaches the strategy stage is logged with its decision and evidence,
whether it was entered or rejected. Exceptions inside a loop are caught, reported as
``system_error`` notifications and the loop continues (fault tolerance); persistent state
(ledger, controls, risk state, exit states) means a restart resumes where it left off.

The engine cannot enable live trading: it only drives a ``PaperTradingSession``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from atomtrader.analytics.performance import TradeRecord, daily_report
from atomtrader.config.settings import RiskLimits
from atomtrader.core.models import AccountKind, Chain, OrderStatus, Side, TokenRef
from atomtrader.core.money import MissingMarketData
from atomtrader.execution.base import NoRoute, QuoteUnavailable
from atomtrader.logging import get_logger
from atomtrader.marketdata.fx import FxService
from atomtrader.marketdata.ingestion import IngestionService
from atomtrader.narrative.engine import MentionBuffer, Narrative, NarrativeEngine, TokenMeta
from atomtrader.narrative.rotation import RotationAlert, detect_rotations
from atomtrader.narrative.sources import NewsSources
from atomtrader.notify import Notifier
from atomtrader.paper.session import PaperTradingSession
from atomtrader.persistence.trading_store import (
    TradingStore,
    complete_command,
    load_trade_records,
    pending_commands,
    save_trade_record,
)
from atomtrader.risk.engine import TradeIntent
from atomtrader.scanner.service import Candidate, ScannerService
from atomtrader.strategy.context import MarketContext
from atomtrader.strategy.entries import EntryConfig, Signal, evaluate_all
from atomtrader.strategy.exits import ExitConfig, ExitEngine, Mode, PositionState
from atomtrader.strategy.scoring import OpportunityWeights, opportunity_score

log = get_logger("engine")
D = Decimal


@dataclass(frozen=True)
class EngineConfig:
    scan_interval_s: float = 60.0
    position_interval_s: float = 20.0
    fx_every_scans: int = 5
    narrative_every_scans: int = 5
    min_moonshot: float = 30.0
    min_opportunity: float = 55.0
    high_confidence_opportunity: float = 70.0
    max_full_evaluations: int = 6
    probe_size_usd: Decimal = D(100)
    platform_fee_pct: Decimal = D("0.10")
    network_fee_eur: Decimal = D("0.05")
    gdelt_queries_per_refresh: int = 2
    # Held tokens are re-assessed more often than candidates so holder snapshots (insider
    # distribution) refresh faster. RugCheck caches holder data on its side as well.
    held_security_max_age_s: float = 120.0
    entry: EntryConfig = field(default_factory=EntryConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    weights: OpportunityWeights = field(default_factory=OpportunityWeights)


class TradingEngine:
    def __init__(
        self,
        session: PaperTradingSession,
        ingestion: IngestionService,
        scanner: ScannerService,
        notifier: Notifier,
        fx: FxService | None = None,
        news: NewsSources | None = None,
        narrative_engine: NarrativeEngine | None = None,
        quoter: Any | None = None,  # JupiterSizedQuoter (round_trip_cost_bps) for Solana
        config: EngineConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.ingestion = ingestion
        self.scanner = scanner
        self.notifier = notifier
        self.fx = fx
        self.news = news
        self.narrative_engine = narrative_engine or NarrativeEngine()
        self.quoter = quoter
        self.cfg = config or EngineConfig()
        self.clock = clock
        self.store = TradingStore(session.repo.sessionmaker)
        self.exits = ExitEngine(self.cfg.exit)
        self.exit_states: dict[str, PositionState] = {}
        self.mentions = MentionBuffer()
        self.narratives: list[Narrative] = []
        self.observed_since: datetime | None = None
        self.profiles: dict[str, Any] = {}
        self.rotations: dict[str, RotationAlert] = {}
        self.scans = 0
        self._alerted: set[str] = set()
        self._last_report_day: str | None = None
        self._hn_since: datetime | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        unresolved = await self.session.start()
        if unresolved:
            await self.notifier.notify("execution_failure", "orders with unknown outcome at start",
                                       ", ".join(unresolved), "critical")
        override = await self.store.load_state("risk_limits_override")
        if override:
            self._apply_limits(RiskLimits(**override))
        for pos in self.session.ledger.open_positions():
            raw = await self.store.load_state(f"exit:{pos.position_id}")
            if raw:
                self.exit_states[pos.position_id] = PositionState.from_dict(raw)
            else:
                self.exit_states[pos.position_id] = self._fallback_state(pos.position_id)
            self.ingestion.watch(pos.token, "", "open_position")
        self._last_report_day = self._local_day(self.clock())
        await self.notifier.notify("system", "engine started",
                                   f"paper mode; open positions: {len(self.exit_states)}; autonomous: {self.session.controls.autonomous}")

    def _fallback_state(self, pid: str) -> PositionState:
        """Positions without a stored exit state (should not happen) get a conservative one
        from their metadata: original stop, normal mode."""
        pos = self.session.ledger.positions[pid]
        meta = self.session.meta.get(pid)
        entry = float(pos.entry_price_usd)
        stop = float(meta.stop_price_usd) if meta else entry * 0.9
        return PositionState(pid, meta.strategy if meta else "unknown", entry, stop, stop, 8.0, entry, Mode.NORMAL,
                             pos.opened_at, 1.2, None, None, ())

    def _local_day(self, t: datetime) -> str:
        return t.astimezone(ZoneInfo(self.session.settings.timezone)).date().isoformat()

    def _apply_limits(self, limits: RiskLimits) -> None:
        self.session.settings = self.session.settings.model_copy(update={"risk": limits})
        if self.session.risk is not None:
            self.session.risk.limits = limits

    async def _safe(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            await fn()
        except Exception as exc:  # keep running; report loudly
            log.error("loop_error", where=name, error=repr(exc)[:500])
            await self.notifier.notify("system_error", f"{name} failed", repr(exc)[:500], "critical")

    # ------------------------------------------------------------------ commands
    async def process_commands(self) -> None:
        s = self.session
        for c in await pending_commands(self.store):
            a, args, actor = c.action, c.args or {}, c.actor
            status, result = "done", "ok"
            try:
                if a == "emergency_stop":
                    await s.emergency_stop(args.get("reason", "dashboard"), actor)
                    await self.notifier.notify("system", "EMERGENCY STOP", f"by {actor}", "critical")
                elif a == "clear_emergency_stop":
                    await s.clear_emergency_stop(actor)
                elif a == "pause":
                    await s.pause(actor)
                elif a == "resume":
                    await s.resume(actor)
                elif a == "autonomous":
                    await s.set_autonomous(bool(args["enabled"]), actor)
                elif a == "pause_chain":
                    await s.pause_chain(Chain(args["chain"]), actor)
                elif a == "resume_chain":
                    await s.resume_chain(Chain(args["chain"]), actor)
                elif a == "blacklist":
                    chain, addr = str(args["token"]).split(":", 1)
                    await s.blacklist(TokenRef(Chain(chain), addr), args.get("reason", "operator"), actor)
                elif a == "close_position":
                    await self._close(args["position_id"], "operator_close")
                elif a == "set_risk":
                    new = RiskLimits(**{**self.session.settings.risk.model_dump(), **args})  # validates ceilings
                    self._apply_limits(new)
                    await self.store.save_state("risk_limits_override", new.model_dump(mode="json"), self.clock())
                    await s.repo.log_event(self.clock(), "control:set_risk", str(args)[:2000], actor)
                else:
                    status, result = "rejected", f"unknown action {a}"
            except Exception as exc:  # invalid arguments, validation errors
                status, result = "rejected", repr(exc)[:500]
            await complete_command(self.store, c.id, status, result, self.clock())

    # ------------------------------------------------------------------ context helpers
    def _narrative_for(self, token: TokenRef) -> tuple[float | None, Narrative | None, Any]:
        strength, narr = NarrativeEngine.token_strength(token, self.narratives)
        link = next((lk for lk in narr.tokens if lk.token == token), None) if narr else None
        return strength, narr, link

    def _context(self, cand: Candidate, cost_bps: float | None = None) -> MarketContext:
        strength, narr, link = self._narrative_for(cand.token)
        from atomtrader.analysis.indicators import resample

        return MarketContext(
            features=cand.features, candles_1m=cand.candles_1m, candles_5m=tuple(resample(cand.candles_1m, 5)),
            moonshot=cand.moonshot, security=cand.security, insider=cand.insider, narrative=narr,
            narrative_link=link, narrative_strength=strength, rotation=self.rotations.get(cand.token.key),
            round_trip_cost_bps=cost_bps,
        )

    # ------------------------------------------------------------------ positions
    async def manage_positions(self) -> None:
        open_pos = self.session.ledger.open_positions()
        if not open_pos:
            return
        await self.ingestion.refresh_tokens([p.token for p in open_pos])
        now = self.clock()
        for pos in open_pos:
            pid = pos.position_id
            cand = await self.scanner.evaluate(pos.token, security_max_age=self.cfg.held_security_max_age_s)
            if cand is None:
                if f"nodata:{pid}" not in self._alerted:
                    self._alerted.add(f"nodata:{pid}")
                    await self.notifier.notify("system_error", f"no market data for open position {pos.token.symbol}", pid, "warning")
                continue
            if cand.stale:
                await self._protect_stale_position(pos, cand.data_age_seconds)
                continue
            self._alerted.discard(f"stale:{pid}")
            ctx = self._context(cand)
            if cand.insider is not None and cand.insider.situation in ("large_holder_selling", "liquidity_deterioration"):
                key = f"insider:{pid}:{cand.insider.situation}"
                if key not in self._alerted:
                    self._alerted.add(key)
                    kind = "liquidity_alert" if cand.insider.situation == "liquidity_deterioration" else "insider_alert"
                    await self.notifier.notify(kind, f"{pos.token.symbol}: {cand.insider.situation}",
                                               "; ".join(cand.insider.evidence), "warning")
            state = self.exit_states.get(pid) or self._fallback_state(pid)
            d = self.exits.evaluate(state, ctx, now)
            self.exit_states[pid] = d.state
            await self.store.save_state(f"exit:{pid}", d.state.to_dict(), now)
            if d.action == "hold":
                continue
            frac = D(1) if d.action == "exit" else D(str(round(d.fraction_of_current, 6)))
            await self._close(pid, d.reason, frac)

    async def _protect_stale_position(self, pos: Any, age_s: float) -> None:
        """Snapshot data is stale (ingestion stalled): no mode/target decisions on old data,
        but the stop is still enforced against a fresh direct quote."""
        pid = pos.position_id
        if f"stale:{pid}" not in self._alerted:
            self._alerted.add(f"stale:{pid}")
            await self.notifier.notify("system_error", f"stale market data for open position {pos.token.symbol}",
                                       f"newest snapshot {age_s:.0f}s old; enforcing stop on direct quotes only", "warning")
        state = self.exit_states.get(pid) or self._fallback_state(pid)
        try:
            q = await self.session.quotes.get_quote(pos.token)
        except MissingMarketData as exc:
            await self.notifier.notify("system_error", f"open position {pos.token.symbol} cannot be priced",
                                       str(exc)[:300], "critical")
            return
        if float(q.price_usd) <= state.stop:
            await self._close(pid, "stop_loss_hit_on_direct_quote")

    async def _close(self, pid: str, reason: str, fraction: Decimal = D(1)) -> None:
        pos = self.session.ledger.positions.get(pid)
        if pos is None or not pos.is_open:
            return
        out = await self.session.exit(pid, reason, fraction)
        sym = pos.token.symbol or pos.token.address[:8]
        if out.result.status is not OrderStatus.FILLED:
            await self.notifier.notify("execution_failure", f"exit failed for {sym}", f"{reason}: {out.result.error}",
                                       "critical")
            return
        f = out.result.fill
        if out.closed_trade is None:
            await self.notifier.notify("partial_exit", f"partial exit {sym} ({reason})", f"€{f.gross_eur:.2f} @ ${f.price_usd:.10g}")
            return
        ct = out.closed_trade
        await self.notifier.notify("exit", f"closed {sym}: net €{ct.net_pnl_eur:.2f}", reason,
                                   "info" if ct.net_pnl_eur >= 0 else "warning")
        await self._record_trade(pid, reason)
        self.exit_states.pop(pid, None)
        await self.store.save_state(f"exit:{pid}", {}, self.clock())

    async def _record_trade(self, pid: str, reason: str) -> None:
        trade = next(t for t in self.session.ledger.closed_trades if t.position_id == pid)
        results = [r for r in await self.session.repo.load_final_results(AccountKind.PAPER)
                   if r.fill is not None and r.fill.position_id == pid]
        buys = [r.fill for r in results if r.fill.side is Side.BUY]
        committed = sum((b.gross_eur + b.fee_eur for b in buys), D(0))
        slips = [float(r.realized_slippage_bps) for r in results if r.realized_slippage_bps is not None]
        st = self.exit_states.get(pid)
        meta = self.session.meta.get(pid)
        thesis = dict(meta.thesis) if meta else {}
        rec = TradeRecord(
            position_id=pid, token=trade.token.key, chain=trade.token.chain.value,
            strategy=meta.strategy if meta else "unknown", opened_at=trade.opened_at, closed_at=trade.closed_at,
            net_pnl_eur=trade.net_pnl_eur, fees_eur=trade.fees_eur, cost_basis_eur=committed, exit_reason=reason,
            max_mode=_max_mode(st), expected_move_pct=_f(thesis.get("expected_move_pct")),
            max_favorable_pct=(st.highest / st.entry_price - 1) * 100 if st else None,
            realized_return_pct=float(trade.net_pnl_eur / committed * 100) if committed else None,
            est_round_trip_cost_bps=_f(thesis.get("round_trip_cost_bps")),
            realized_slippage_bps=max(slips) if slips else None, entry_h1_change_pct=_f(thesis.get("chg_h1")),
            account_kind="paper",
        )
        data = {k: (str(v) if isinstance(v, (Decimal, datetime)) else v) for k, v in rec.__dict__.items()}
        await save_trade_record(self.store, pid, "paper", trade.closed_at, data)

    # ------------------------------------------------------------------ narratives
    async def refresh_narratives(self) -> None:
        if self.news is None:
            return
        now = self.clock()
        ms, err = await self.news.hn_latest(since=self._hn_since)
        if err is None:
            self._hn_since = now - timedelta(minutes=5)
            if self.observed_since is None:
                self.observed_since = now
        self.mentions.add(ms, now)
        for n in self.narratives[: self.cfg.gdelt_queries_per_refresh]:
            g, gerr = await self.news.gdelt_articles(f'"{n.term}"', timespan="6h")
            if gerr is None:
                self.mentions.add(g, now)
        try:
            for p in await self.ingestion.dex.latest_profiles():
                self.profiles[f"{p.chain_id}:{p.token_address}"] = p
        except Exception as exc:  # attention data is optional
            log.warning("profiles_unavailable", error=str(exc)[:200])
        metas = []
        for item in self.ingestion.watchlist.values():
            prof = self.profiles.get(item.token.key)
            metas.append(TokenMeta(item.token, item.token.symbol, prof.description if prof else "",
                                   prof.links if prof else (), None, False))
        self.narratives = self.narrative_engine.analyse(self.mentions.all(), metas, now, self.observed_since)
        for n in self.narratives[:3]:
            if n.status == "emerging" and n.tokens and f"narr:{n.term}" not in self._alerted:
                self._alerted.add(f"narr:{n.term}")
                await self.notifier.notify("narrative_breakout", f"emerging narrative '{n.term}'",
                                           f"{n.mentions_recent} mentions, {n.unique_authors} authors, x{n.acceleration}; "
                                           f"linked tokens: {[lk.token.symbol for lk in n.tokens][:5]}")

    # ------------------------------------------------------------------ entries
    async def _round_trip_cost_bps(self, token: TokenRef) -> float | None:
        if self.quoter is None or token.chain is not Chain.SOLANA:
            return None
        try:
            rt = float(await self.quoter.round_trip_cost_bps(token.address, self.cfg.probe_size_usd))
        except (NoRoute, QuoteUnavailable, MissingMarketData):
            return None
        probe_eur = float(self.cfg.probe_size_usd) * 0.92
        return rt + 2 * float(self.cfg.platform_fee_pct) * 100 + 2 * float(self.cfg.network_fee_eur) / probe_eur * 10_000

    async def _reject(self, cand: Candidate, strategy: str, reasons: list[str], evidence: dict[str, Any]) -> None:
        await self.session.repo.log_decision(
            at=self.clock(), token=cand.token, strategy=strategy, approved=False, notional_eur=D(0), risk_eur=D(0),
            reasons=reasons, evidence=evidence, account_kind=AccountKind.PAPER)

    async def scan_and_enter(self) -> None:
        self.scans += 1
        if self.fx is not None and (self.scans - 1) % self.cfg.fx_every_scans == 0:
            rep = await self.fx.refresh()
            if rep.errors:
                log.warning("fx_errors", errors=list(rep.errors))
        await self.ingestion.run_once()
        if (self.scans - 1) % self.cfg.narrative_every_scans == 0:
            await self._safe("narratives", self.refresh_narratives)

        c = self.session.controls
        if c.emergency_stop or c.global_pause:
            return
        held = {p.token.key for p in self.session.ledger.open_positions()}
        prelim: list[Candidate] = []
        for item in list(self.ingestion.watchlist.values()):
            if item.token.key in held or item.token.key in c.blacklist:
                continue
            strength, _, _ = self._narrative_for(item.token)
            cand = await self.scanner.evaluate(item.token, item.pool_address or None, strength, assess=False)
            if cand is not None and cand.stale:
                continue  # never rank or enter on stale data
            if cand is not None and cand.moonshot.score >= self.cfg.min_moonshot:
                prelim.append(cand)
        self.rotations = {a.follower: a for a in detect_rotations(self.narratives, {x.token.key: x.features for x in prelim})}
        for a in list(self.rotations.values())[:3]:
            if f"rot:{a.follower}" not in self._alerted:
                self._alerted.add(f"rot:{a.follower}")
                await self.notifier.notify("rotation", f"possible second wave in '{a.narrative}'",
                                           f"leader {a.leader} cooling, follower {a.follower} accelerating "
                                           f"(score {a.score}, {a.evidence_type})")

        await self._publish_opportunities(prelim)
        for pre in sorted(prelim, key=lambda x: -x.moonshot.score)[: self.cfg.max_full_evaluations]:
            try:
                await self._consider(pre)
            except Exception as exc:  # one bad candidate must not abort the scan
                log.warning("candidate_failed", token=pre.token.key, error=repr(exc)[:300])
                await self.session.repo.log_event(self.clock(), "candidate_failed", f"{pre.token.key}: {exc!r}"[:2048])

    async def _consider(self, pre: Candidate) -> None:
        strength, narr, _ = self._narrative_for(pre.token)
        cand = await self.scanner.evaluate(pre.token, pre.features.pair_address, strength, assess=True)
        if cand is None:
            return
        if cand.stale:
            await self._reject(cand, "data", [f"stale_market_data:{cand.data_age_seconds:.0f}s"], {})
            return
        base_ev: dict[str, Any] = {
            "moonshot": cand.moonshot.score, "moonshot_components": cand.moonshot.components,
            "moonshot_penalties": cand.moonshot.penalties, "moonshot_flags": list(cand.moonshot.flags),
            "narrative": narr.term if narr else None, "narrative_status": narr.status if narr else None,
            "insider": cand.insider.situation if cand.insider else None,
            "insider_score": cand.insider.score if cand.insider else None,
            "security_warnings": cand.security.warnings if cand.security else None,
            "liquidity_usd": cand.features.liquidity_usd, "chg_h1": cand.features.chg_h1,
        }
        if cand.security is None:
            await self._reject(cand, "security", ["security_not_assessed"], base_ev)
            return
        if not cand.security.entry_allowed:
            await self._reject(cand, "security", cand.security.hard_fail + cand.security.unverified, base_ev)
            return
        ctx = self._context(cand)
        sigs = [r.signal for r in evaluate_all(ctx, self.cfg.entry) if r.signal is not None]
        if not sigs:
            return  # no strategy setup: not logged per-cycle (would flood the audit log)
        cost = await self._round_trip_cost_bps(cand.token)
        if cost is not None:
            ctx = replace(ctx, round_trip_cost_bps=cost)
            results = evaluate_all(ctx, self.cfg.entry)
            sigs = [r.signal for r in results if r.signal is not None]
            if not sigs:
                await self._reject(cand, "cost_gate", ["not_worth_costs_at_exact_quote"],
                                   {**base_ev, "round_trip_cost_bps": cost,
                                    "failed": {r.strategy: r.failed for r in results}})
                return
        sig: Signal = max(sigs, key=lambda s: s.confidence * s.reward_to_risk)
        opp = opportunity_score(ctx, sig.reward_to_risk, self.cfg.weights)
        ev = {**base_ev, "strategy": sig.strategy, "thesis": sig.thesis, "conditions": list(sig.conditions),
              "confirmations": list(sig.confirmations), "invalidations": list(sig.invalidations),
              "expected_move_pct": sig.expected_move_pct, "reward_to_risk": sig.reward_to_risk,
              "round_trip_cost_bps": sig.round_trip_cost_bps, "cost_source": "jupiter_exact" if cost is not None else "default",
              "opportunity": opp.score, "opportunity_confidence": opp.confidence,
              "opportunity_components": opp.components, "missing": list(opp.missing)}
        if opp.score < self.cfg.min_opportunity:
            await self._reject(cand, sig.strategy, [f"opportunity_below_min:{opp.score}"], ev)
            return
        if opp.score >= self.cfg.high_confidence_opportunity:
            await self.notifier.notify("opportunity", f"high-confidence setup {cand.token.symbol} ({sig.strategy})",
                                       f"opportunity {opp.score}, R/R {sig.reward_to_risk}")
        usd = self.session.fx.get("USD", self.clock())
        intent = TradeIntent(
            token=cand.token, strategy=sig.strategy, entry_price_usd=D(str(sig.entry_price)),
            stop_price_usd=D(str(sig.stop_price)),
            pool_liquidity_eur=usd.to_eur(D(str(cand.features.liquidity_usd or 0))),
            est_entry_slippage_bps=D(str(round(sig.round_trip_cost_bps / 2, 2))),
            est_exit_slippage_bps=D(str(round(sig.round_trip_cost_bps / 2, 2))),
            est_fee_pct_per_side=self.cfg.platform_fee_pct, est_network_fee_eur=self.cfg.network_fee_eur,
            confidence=D(str(round(sig.confidence * opp.score / 100, 4))),
            narrative=narr.term if narr else None, security_hard_fail=not cand.security.entry_allowed,
            insider_risk_score=D(cand.insider.score) if cand.insider else None,
            opportunity_score=D(str(opp.score)),
        )
        res = await self.session.enter(intent, ev)
        if res.status == "filled" and res.position_id:
            fill = res.outcome.result.fill  # type: ignore[union-attr]
            atr = cand.features.ta_1m.atr_pct if cand.features.ta_1m else None
            st = self.exits.initial_state(sig, res.position_id, fill.executed_at, float(fill.price_usd),
                                          cand.features.liquidity_usd, atr, strength)
            self.exit_states[res.position_id] = st
            await self.store.save_state(f"exit:{res.position_id}", st.to_dict(), self.clock())
            self.ingestion.watch(cand.token, cand.features.pair_address, "open_position")
            await self.notifier.notify("entry", f"entered {cand.token.symbol} ({sig.strategy})",
                                       f"€{fill.gross_eur:.2f} @ ${fill.price_usd:.10g}; stop ${sig.stop_price:.10g}; "
                                       f"opportunity {opp.score}; thesis: {sig.thesis}")
        elif res.status == "awaiting_confirmation":
            await self.notifier.notify("awaiting_confirmation", f"{cand.token.symbol}: approved by risk, autonomous mode off",
                                       f"{sig.strategy}, size €{res.decision.notional_eur}")
        elif res.status == "failed":
            await self.notifier.notify("execution_failure", f"entry failed {cand.token.symbol}",
                                       str(res.outcome.result.error if res.outcome else ""), "warning")

    # ------------------------------------------------------------------ reporting
    async def maybe_daily_report(self, force_day: str | None = None) -> str | None:
        now = self.clock()
        today = self._local_day(now)
        day = force_day or (self._last_report_day if self._last_report_day and self._last_report_day != today else None)
        if day is None:
            return None
        tz = ZoneInfo(self.session.settings.timezone)
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        rows = await load_trade_records(self.store, "paper", start.astimezone(UTC))
        trades = [_trade_from_row(r.data) for r in rows if _local(r.closed_at, tz) == day]
        snaps = [s for s in await self.store.equity_snapshots() if _local(s.at, tz) == day and s.equity_eur is not None]
        start_eq = snaps[0].equity_eur if snaps else self.session.settings.starting_capital_eur
        end_eq = snaps[-1].equity_eur if snaps else None
        anomalies = [f"{e.kind}: {e.detail[:160]}" for e in await self.session.repo.events()
                     if _local(e.at, tz) == day and e.kind in ("notify:execution_failure", "notify:system_error",
                                                               "unreconciled_order", "risk:daily_loss_limit")]
        costs = await self.session.repo.cost_breakdown(AccountKind.PAPER, start.astimezone(UTC),
                                                       (start + timedelta(days=1)).astimezone(UTC))
        rep = daily_report(day, trades, start_eq, end_eq, anomalies[:50],
                           failed_tx_fees_eur=costs["failed_tx_fees_eur"],
                           failed_transactions=int(costs["failed_transactions"]),
                           slippage_vs_mid_eur=costs["slippage_vs_mid_eur"])
        md = rep.to_markdown()
        await self.store.save_state(f"report:{day}", {"markdown": md}, now)
        await self.notifier.notify("daily_report", f"daily report {day}",
                                   f"net €{rep.net_pnl_eur:.2f}, trades {rep.overall.trades}")
        self._last_report_day = today
        return md

    # ------------------------------------------------------------------ main loop
    async def run(self, stop: asyncio.Event, max_scans: int | None = None) -> None:
        next_scan = 0.0
        while not stop.is_set():
            await self._safe("commands", self.process_commands)
            await self._safe("positions", self.manage_positions)
            await self._safe("publish_positions", self._publish_positions)
            if time.monotonic() >= next_scan:
                await self._safe("scan", self.scan_and_enter)
                next_scan = time.monotonic() + self.cfg.scan_interval_s
                await self._safe("snapshot", self._snapshot)
                await self._safe("report", self.maybe_daily_report)
                if max_scans is not None and self.scans >= max_scans:
                    break
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.position_interval_s)
            except TimeoutError:
                pass

    async def _snapshot(self) -> None:
        try:
            snap = await self.session.snapshot()
        except MissingMarketData as exc:
            log.warning("snapshot_skipped", reason=str(exc)[:200])
            return
        st = self.session.state
        eq_hist = [s.equity_eur for s in await self.store.equity_snapshots() if s.equity_eur is not None]
        from atomtrader.analytics.performance import max_drawdown

        dd_eur, dd_pct = max_drawdown(eq_hist) if eq_hist else (D(0), 0.0)
        peak = max(eq_hist) if eq_hist else None
        await self.store.save_state("dash:account", {
            "label": snap.label, "account_kind": snap.account_kind.value, "at": snap.at.isoformat(),
            "equity_eur": _s(snap.equity_eur), "cash_eur": _s(snap.cash_eur), "invested_eur": _s(snap.invested_eur),
            "realized_pnl_eur": _s(snap.realized_pnl_eur), "unrealized_pnl_eur": _s(snap.unrealized_pnl_eur),
            "fees_eur": _s(snap.total_fees_eur), "daily_pnl_eur": _s(st.realized_net_pnl_eur),
            "failed_tx_fees_eur": _s(self.session.ledger.failed_tx_fees_eur),
            "daily_start_equity_eur": _s(st.start_equity_eur), "entries_today": st.entries_today,
            "daily_halt": st.daily_halt, "cooldown_until": st.cooldown_until.isoformat() if st.cooldown_until else None,
            "current_drawdown_pct": float((peak - eq_hist[-1]) / peak * 100) if peak else 0.0,
            "max_drawdown_eur": _s(dd_eur), "max_drawdown_pct": dd_pct, "unpriced": list(snap.unpriced),
            "equity_curve": [_s(x) for x in eq_hist[-300:]],
        }, self.clock())
        await self._publish_positions()

    async def _publish_positions(self) -> None:
        rows = []
        now = self.clock()
        usd = None
        try:
            usd = self.session.fx.get("USD", now)
        except MissingMarketData:
            pass
        for pos in self.session.ledger.open_positions():
            st = self.exit_states.get(pos.position_id)
            meta = self.session.meta.get(pos.position_id)
            price = None
            try:
                price = (await self.session.quotes.get_quote(pos.token)).price_usd
            except MissingMarketData:
                pass
            value = pos.quantity * usd.to_eur(price) if price is not None and usd else None
            rows.append({
                "position_id": pos.position_id, "token": pos.token.symbol or pos.token.address[:8],
                "address": pos.token.address, "chain": pos.token.chain.value,
                "strategy": meta.strategy if meta else None, "narrative": meta.narrative if meta else None,
                "entry_price_usd": _s(pos.entry_price_usd), "current_price_usd": _s(price),
                "value_eur": _s(value), "cost_basis_eur": _s(pos.cost_basis_eur),
                "unrealized_pnl_eur": _s(value - pos.cost_basis_eur) if value is not None else None,
                "return_pct": float((price / pos.entry_price_usd - 1) * 100) if price is not None and pos.entry_price_usd else None,
                "holding_minutes": round((now - pos.opened_at).total_seconds() / 60, 1),
                "mode": st.mode.value if st else None, "stop_usd": st.stop if st else None,
                "target_pct": st.target_pct if st else None, "highest_usd": st.highest if st else None,
                "partials_done": st.partial_levels_done if st else 0,
            })
        await self.store.save_state("dash:positions", {"at": now.isoformat(), "positions": rows}, now)

    async def _publish_opportunities(self, prelim: list[Candidate]) -> None:
        now = self.clock()
        top = sorted(prelim, key=lambda x: -x.moonshot.score)[:25]
        await self.store.save_state("dash:opportunities", {
            "at": now.isoformat(),
            "candidates": [{
                "token": c.token.symbol or c.token.address[:8], "key": c.token.key, "chain": c.token.chain.value,
                "moonshot": c.moonshot.score, "confidence": c.moonshot.confidence, "flags": list(c.moonshot.flags),
                "penalties": c.moonshot.penalties, "liquidity_usd": c.features.liquidity_usd,
                "volume_accel": c.features.volume_accel_m5_h1, "buyer_accel": c.features.buyer_accel_m5_h1,
                "chg_h1": c.features.chg_h1, "chg_h24": c.features.chg_h24,
                "narrative": (self._narrative_for(c.token)[1].term if self._narrative_for(c.token)[1] else None),
                "insider_score": c.insider.score if c.insider else None,
            } for c in top],
            "narratives": [{"term": n.term, "status": n.status, "strength": n.strength, "mentions": n.mentions_recent,
                            "authors": n.unique_authors, "acceleration": n.acceleration, "category": n.category,
                            "headline": n.headlines[0] if n.headlines else "", "tokens": [lk.token.symbol for lk in n.tokens][:6],
                            "flags": list(n.flags)} for n in self.narratives[:15]],
            "rotations": [{"narrative": a.narrative, "leader": a.leader, "follower": a.follower, "score": a.score,
                           "crowded": a.crowded, "evidence_type": a.evidence_type} for a in self.rotations.values()],
        }, now)


# ---------------------------------------------------------------------- helpers


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "None", "") else float(v)
    except (TypeError, ValueError):
        return None


def _max_mode(st: PositionState | None) -> str:
    if st is None:
        return "normal"
    if st.mode is Mode.RUNNER or "upgraded_to_runner" in st.notes:
        return "runner"
    if st.mode is Mode.MOMENTUM or "upgraded_to_momentum" in st.notes:
        return "momentum"
    return "normal"


def _local(t: datetime, tz: ZoneInfo) -> str:
    return t.astimezone(tz).date().isoformat()


def _trade_from_row(d: dict[str, Any]) -> TradeRecord:
    return TradeRecord(
        position_id=d["position_id"], token=d["token"], chain=d["chain"], strategy=d["strategy"],
        opened_at=datetime.fromisoformat(d["opened_at"]), closed_at=datetime.fromisoformat(d["closed_at"]),
        net_pnl_eur=D(d["net_pnl_eur"]), fees_eur=D(d["fees_eur"]), cost_basis_eur=D(d["cost_basis_eur"]),
        exit_reason=d.get("exit_reason", ""), max_mode=d.get("max_mode", "normal"),
        expected_move_pct=d.get("expected_move_pct"), max_favorable_pct=d.get("max_favorable_pct"),
        realized_return_pct=d.get("realized_return_pct"), est_round_trip_cost_bps=d.get("est_round_trip_cost_bps"),
        realized_slippage_bps=d.get("realized_slippage_bps"), entry_h1_change_pct=d.get("entry_h1_change_pct"),
        account_kind=d.get("account_kind", "paper"),
    )
