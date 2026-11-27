"""Performance analytics, post-trade review, parameter proposals and the daily report.

All money figures come from reconciled fills (net of every fee). Paper and live figures are
never mixed: every report carries its ``account_kind`` label.

Self-improvement is *proposal-only*: ``propose_adjustments`` suggests strategy/parameter
changes with the evidence behind them; it never edits configuration, and risk limits are
explicitly outside its scope.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TradeRecord:
    position_id: str
    token: str
    chain: str
    strategy: str
    opened_at: datetime
    closed_at: datetime
    net_pnl_eur: Decimal
    fees_eur: Decimal
    cost_basis_eur: Decimal  # EUR committed at entry incl. entry fees
    exit_reason: str = ""
    max_mode: str = "normal"  # highest mode reached: normal | momentum | runner
    expected_move_pct: float | None = None
    max_favorable_pct: float | None = None  # best unrealised return while held
    realized_return_pct: float | None = None
    est_round_trip_cost_bps: float | None = None
    realized_slippage_bps: float | None = None
    entry_h1_change_pct: float | None = None
    account_kind: str = "paper"

    @property
    def hold_minutes(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 60

    @property
    def won(self) -> bool:
        return self.net_pnl_eur > 0


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    net_pnl_eur: Decimal
    fees_eur: Decimal
    avg_win_eur: Decimal | None
    avg_loss_eur: Decimal | None
    profit_factor: float | None
    expectancy_eur: Decimal | None
    avg_hold_minutes: float | None
    best_eur: Decimal | None
    worst_eur: Decimal | None


def metrics(trades: Sequence[TradeRecord]) -> Metrics:
    wins = [t.net_pnl_eur for t in trades if t.net_pnl_eur > 0]
    losses = [t.net_pnl_eur for t in trades if t.net_pnl_eur <= 0]
    gross_w = sum(wins, Decimal(0))
    gross_l = -sum(losses, Decimal(0))
    n = len(trades)
    net = sum((t.net_pnl_eur for t in trades), Decimal(0))
    return Metrics(
        trades=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n if n else None,
        net_pnl_eur=net, fees_eur=sum((t.fees_eur for t in trades), Decimal(0)),
        avg_win_eur=gross_w / len(wins) if wins else None,
        avg_loss_eur=-gross_l / len(losses) if losses else None,
        profit_factor=float(gross_w / gross_l) if gross_l > 0 else (None if not wins else float("inf")),
        expectancy_eur=net / n if n else None,
        avg_hold_minutes=sum(t.hold_minutes for t in trades) / n if n else None,
        best_eur=max((t.net_pnl_eur for t in trades), default=None),
        worst_eur=min((t.net_pnl_eur for t in trades), default=None),
    )


def max_drawdown(equity: Sequence[Decimal]) -> tuple[Decimal, float]:
    """(max drawdown in EUR, as % of the running peak)."""
    peak = None
    dd_eur, dd_pct = Decimal(0), 0.0
    for e in equity:
        peak = e if peak is None or e > peak else peak
        d = peak - e
        if d > dd_eur:
            dd_eur = d
        if peak > 0:
            dd_pct = max(dd_pct, float(d / peak * 100))
    return dd_eur, dd_pct


def equity_from_trades(start: Decimal, trades: Iterable[TradeRecord]) -> list[Decimal]:
    eq = [start]
    for t in sorted(trades, key=lambda t: t.closed_at):
        eq.append(eq[-1] + t.net_pnl_eur)
    return eq


def breakdown(trades: Sequence[TradeRecord], key: str) -> dict[str, Metrics]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[str(getattr(t, key))].append(t)
    return {k: metrics(v) for k, v in sorted(groups.items())}


# --------------------------------------------------------------------------- post-trade review


@dataclass(frozen=True)
class TradeReview:
    position_id: str
    findings: tuple[str, ...]
    expected_move_pct: float | None
    realized_return_pct: float | None
    max_favorable_pct: float | None


def review_trade(t: TradeRecord) -> TradeReview:
    f: list[str] = []
    if t.max_favorable_pct is not None and t.realized_return_pct is not None:
        giveback = t.max_favorable_pct - t.realized_return_pct
        if t.max_favorable_pct >= 10 and giveback >= 0.5 * t.max_favorable_pct and t.net_pnl_eur > 0:
            f.append(f"gave back {giveback:.1f} pts of a {t.max_favorable_pct:.1f}% peak (trail too loose?)")
        if t.realized_return_pct > 0 and t.max_favorable_pct >= 3 * max(t.realized_return_pct, 0.1) \
                and t.exit_reason in ("time_stop", "target_reached_momentum_weak"):
            f.append("possible premature exit: peak far above realised return")
    if t.net_pnl_eur < 0 and t.hold_minutes < 5 and "stop" in t.exit_reason:
        f.append("false positive: stopped out within 5 minutes")
    if t.entry_h1_change_pct is not None and t.entry_h1_change_pct > 80:
        f.append(f"late entry: token already +{t.entry_h1_change_pct:.0f}% in the hour before entry")
    if t.realized_slippage_bps is not None and t.est_round_trip_cost_bps:
        if t.realized_slippage_bps > t.est_round_trip_cost_bps:  # one leg exceeded the whole round-trip estimate
            f.append(f"slippage problem: {t.realized_slippage_bps:.0f} bps vs {t.est_round_trip_cost_bps:.0f} bps estimated round trip")
    if t.expected_move_pct and t.realized_return_pct is not None and t.net_pnl_eur > 0 \
            and t.realized_return_pct >= 2 * t.expected_move_pct:
        f.append("move far exceeded expectation (runner)")
    return TradeReview(t.position_id, tuple(f), t.expected_move_pct, t.realized_return_pct, t.max_favorable_pct)


# --------------------------------------------------------------------------- proposals


@dataclass(frozen=True)
class Proposal:
    kind: str
    target: str
    suggestion: str
    evidence: str
    status: str = "proposed"  # never auto-applied


def propose_adjustments(trades: Sequence[TradeRecord], min_trades: int = 15) -> list[Proposal]:
    out: list[Proposal] = []
    for strat, m in breakdown(trades, "strategy").items():
        if m.trades < min_trades:
            continue
        if m.expectancy_eur is not None and m.expectancy_eur < 0 and (m.profit_factor or 0) < 0.8:
            out.append(Proposal("disable_strategy", strat, "pause this strategy pending review",
                                f"{m.trades} trades, expectancy €{m.expectancy_eur:.2f}, PF {m.profit_factor:.2f}"))
    reviews = [review_trade(t) for t in trades]
    premature = sum(any("premature" in x for x in r.findings) for r in reviews)
    giveback = sum(any("gave back" in x for x in r.findings) for r in reviews)
    late = sum(any("late entry" in x for x in r.findings) for r in reviews)
    n = len(trades)
    if n >= min_trades and premature / n > 0.2:
        out.append(Proposal("exit_parameter", "time_stop / momentum threshold", "consider longer hold or lower momentum bar",
                            f"{premature}/{n} trades flagged as premature exits"))
    if n >= min_trades and giveback / n > 0.2:
        out.append(Proposal("exit_parameter", "momentum/runner trail", "consider a tighter trail",
                            f"{giveback}/{n} trades gave back over half their peak"))
    if n >= min_trades and late / n > 0.25:
        out.append(Proposal("entry_filter", "extension filter", "tighten the 'not extended' filter",
                            f"{late}/{n} entries came after >80% hourly moves"))
    return out


# --------------------------------------------------------------------------- daily report


@dataclass
class DailyReport:
    day: str
    account_kind: str
    starting_equity_eur: Decimal
    ending_equity_eur: Decimal | None
    net_pnl_eur: Decimal
    fees_eur: Decimal
    overall: Metrics
    max_drawdown_eur: Decimal
    max_drawdown_pct: float
    by_strategy: dict[str, Metrics]
    by_chain: dict[str, Metrics]
    by_mode: dict[str, Metrics]
    reviews: list[TradeReview]
    anomalies: list[str] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    failed_tx_fees_eur: Decimal = Decimal(0)
    failed_transactions: int = 0
    slippage_vs_mid_eur: Decimal | None = None

    def to_markdown(self) -> str:
        label = "PAPER (simulated funds)" if self.account_kind == "paper" else "LIVE"
        o = self.overall

        def fmt(x: object, pct: bool = False) -> str:
            if x is None:
                return "—"
            if isinstance(x, float):
                return f"{x * 100:.0f}%" if pct else f"{x:.2f}"
            if isinstance(x, Decimal):
                return f"€{x:,.2f}"
            return str(x)

        def table(title: str, rows: dict[str, Metrics]) -> list[str]:
            if not rows:
                return []
            out = [f"### {title}", "| | trades | win rate | net | PF |", "|---|---|---|---|---|"]
            out += [f"| {k} | {m.trades} | {fmt(m.win_rate, True)} | {fmt(m.net_pnl_eur)} | {fmt(m.profit_factor)} |"
                    for k, m in rows.items()]
            return out + [""]

        lines = [
            f"# Daily report {self.day} — {label}", "",
            f"- Starting equity: {fmt(self.starting_equity_eur)}",
            f"- Ending equity: {fmt(self.ending_equity_eur)}",
            f"- Net P&L (after all trading costs, incl. failed transactions): {fmt(self.net_pnl_eur)}",
            f"- Fees on fills: {fmt(self.fees_eur)}",
            f"- Failed-transaction fees: {fmt(self.failed_tx_fees_eur)} ({self.failed_transactions} failed tx)",
            f"- Slippage/price impact vs mid (already inside fill prices): {fmt(self.slippage_vs_mid_eur)}",
            f"- Total explicit costs: {fmt(self.fees_eur + self.failed_tx_fees_eur)}",
            "- Not included: infrastructure and data subscriptions (see RUNBOOK cost table)",
            f"- Trades: {o.trades} (wins {o.wins}, losses {o.losses}, win rate {fmt(o.win_rate, True)})",
            f"- Best / worst trade: {fmt(o.best_eur)} / {fmt(o.worst_eur)}",
            f"- Max drawdown: {fmt(self.max_drawdown_eur)} ({self.max_drawdown_pct:.1f}%)",
            f"- Avg holding time: {fmt(o.avg_hold_minutes)} min", "",
        ]
        lines += table("By strategy", self.by_strategy) + table("By chain", self.by_chain) + table("By exit mode (runner performance)", self.by_mode)
        flagged = [r for r in self.reviews if r.findings]
        if flagged:
            lines += ["### Trade reviews (mistakes & notable outcomes)"] + [f"- `{r.position_id}`: " + "; ".join(r.findings) for r in flagged] + [""]
        if self.anomalies:
            lines += ["### Anomalies"] + [f"- {a}" for a in self.anomalies] + [""]
        if self.proposals:
            lines += ["### Proposed adjustments (not applied)"] + [f"- [{p.kind}] {p.target}: {p.suggestion} — {p.evidence}" for p in self.proposals] + [""]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def daily_report(day: str, trades: Sequence[TradeRecord], starting_equity: Decimal,
                 ending_equity: Decimal | None, anomalies: Sequence[str] = (), account_kind: str = "paper",
                 failed_tx_fees_eur: Decimal = Decimal(0), failed_transactions: int = 0,
                 slippage_vs_mid_eur: Decimal | None = None) -> DailyReport:
    """Net P&L = closed-trade net P&L (fills incl. their fees) minus network fees burned by
    failed transactions, which belong to no trade but are a real trading cost."""
    if any(t.account_kind != account_kind for t in trades):
        raise ValueError("paper and live trades must not be mixed in one report")
    dd_eur, dd_pct = max_drawdown(equity_from_trades(starting_equity, trades))
    m = metrics(trades)
    return DailyReport(
        day=day, account_kind=account_kind, starting_equity_eur=starting_equity, ending_equity_eur=ending_equity,
        net_pnl_eur=m.net_pnl_eur - failed_tx_fees_eur, fees_eur=m.fees_eur,
        failed_tx_fees_eur=failed_tx_fees_eur, failed_transactions=failed_transactions,
        slippage_vs_mid_eur=slippage_vs_mid_eur, overall=m, max_drawdown_eur=dd_eur, max_drawdown_pct=dd_pct,
        by_strategy=breakdown(trades, "strategy"), by_chain=breakdown(trades, "chain"), by_mode=breakdown(trades, "max_mode"),
        reviews=[review_trade(t) for t in trades], anomalies=list(anomalies), proposals=propose_adjustments(trades),
    )
