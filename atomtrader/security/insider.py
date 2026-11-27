"""Insider Exit Risk Score (0–100) with explicit situations.

Concentration alone is NOT a reason to reject a token. Four situations get different
responses:

  situation                 typical score  response
  ------------------------  -------------  ------------------------------------------
  low                       0–29           normal
  concentrated_inactive     30–49          normal size, monitor holders
  concentrated_suspicious   50–79          reduced size (risk engine haircut)
  large_holder_selling      ≥ 80           block entries; exit engine exits
  liquidity_deterioration   ≥ 85           block entries; exit engine exits

Components (points):
  concentration  0–35  top-10 share excluding pools/lockers, creator share
  suspicious     0–20  linked-wallet networks (RugCheck), insider-flagged holders
  selling        0–30  measured decrease of large holders between snapshots (confirmed
                       distribution, not an assumption)
  liquidity      0–30  drop of pool liquidity from its recent peak (market cap is NOT
                       withdrawable liquidity)

Missing holder data is never treated as "no concentration": the concentration component is
set to a cautious 15 points and the gap is listed in ``data_gaps``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from atomtrader.security.models import HolderSnapshot, SecurityReport

ZERO = Decimal(0)


@dataclass(frozen=True)
class InsiderRisk:
    score: int
    situation: str
    response: str
    components: dict[str, float]
    evidence: tuple[str, ...]
    data_gaps: tuple[str, ...] = field(default_factory=tuple)


def _interp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _concentration(r: SecurityReport, evidence: list[str], gaps: list[str]) -> float:
    top10 = r.top_pct(10, exclude_infrastructure=True)
    if top10 is None:
        gaps.append("holders")
        return 15.0
    pts = _interp(float(top10), 20, 50, 0, 25) + _interp(float(top10), 50, 80, 0, 10)
    if top10 >= 30:
        evidence.append(f"top-10 non-pool holders own {top10:.1f}%")
    if r.creator_pct is not None and r.creator_pct > 2:
        pts += _interp(float(r.creator_pct), 2, 10, 2, 8)
        evidence.append(f"creator holds {r.creator_pct:.1f}%")
    return min(35.0, pts)


def _suspicious(r: SecurityReport, evidence: list[str], gaps: list[str]) -> float:
    pts = 0.0
    if r.insider_network_pct is not None:
        pts += _interp(float(r.insider_network_pct), 2, 15, 0, 15)
        if r.insider_network_pct >= 2:
            evidence.append(f"linked-wallet networks hold {r.insider_network_pct:.1f}% ({r.insider_network_wallets} wallets)")
    elif r.insider_network_wallets is None:
        gaps.append("insider_networks")
    flagged = sum((h.pct for h in r.holders if h.insider), ZERO)
    if flagged > 0:
        pts += _interp(float(flagged), 1, 10, 2, 10)
        evidence.append(f"holders flagged as insiders own {flagged:.1f}%")
    return min(20.0, pts)


def _selling(history: Sequence[HolderSnapshot], now_holders: SecurityReport, evidence: list[str], gaps: list[str]) -> float:
    if not history:
        gaps.append("holder_history")
        return 0.0
    base = history[0]
    current = {h.address: h.pct for h in now_holders.holders}
    decrease = ZERO
    sellers = 0
    for h in base.holders:
        if h.kind.is_infrastructure or h.pct < 1:
            continue
        now_pct = current.get(h.address)
        if now_pct is None:
            # Dropped out of the top list: only counts if the list is complete enough to know
            # (fall back to the smallest listed share as an upper bound on its holding).
            if len(now_holders.holders) >= 10:
                now_pct = min(current.values()) if current else ZERO
            else:
                continue
        drop = h.pct - now_pct
        if drop >= Decimal("0.5"):
            decrease += drop
            sellers += 1
    if base.creator_pct is not None and now_holders.creator_pct is not None:
        cdrop = base.creator_pct - now_holders.creator_pct
        if cdrop >= Decimal("0.5"):
            decrease += cdrop
            evidence.append(f"creator reduced holding by {cdrop:.1f} pp")
    if decrease > 0:
        evidence.append(f"{sellers} large holders reduced by {decrease:.1f} pp of supply since {base.at:%H:%M}Z")
    return _interp(float(decrease), 0.5, 5, 5, 30) if decrease >= Decimal("0.5") else 0.0


def _liquidity(series: Sequence[tuple[datetime, Decimal]], now: datetime, window: timedelta,
               evidence: list[str], gaps: list[str]) -> float:
    recent = [(t, v) for t, v in series if now - t <= window and v is not None]
    if len(recent) < 2:
        gaps.append("liquidity_history")
        return 0.0
    peak = max(v for _, v in recent)
    cur = recent[-1][1]
    if peak <= 0:
        return 0.0
    drop = float((peak - cur) / peak * 100)
    if drop >= 10:
        evidence.append(f"liquidity down {drop:.0f}% from recent peak (${peak:,.0f} -> ${cur:,.0f})")
    return _interp(drop, 10, 40, 0, 30) if drop >= 10 else 0.0


def insider_exit_risk(
    report: SecurityReport,
    holder_history: Sequence[HolderSnapshot] = (),
    liquidity_series: Sequence[tuple[datetime, Decimal]] = (),
    now: datetime | None = None,
    liquidity_window: timedelta = timedelta(minutes=60),
) -> InsiderRisk:
    now = now or report.checked_at
    evidence: list[str] = []
    gaps: list[str] = []
    comp = {
        "concentration": _concentration(report, evidence, gaps),
        "suspicious": _suspicious(report, evidence, gaps),
        "selling": _selling(holder_history, report, evidence, gaps),
        "liquidity": _liquidity(liquidity_series, now, liquidity_window, evidence, gaps),
    }
    raw = sum(comp.values())
    if comp["liquidity"] >= 20:
        situation, score, response = "liquidity_deterioration", max(raw, 85), "exit"
    elif comp["selling"] >= 20:
        situation, score, response = "large_holder_selling", max(raw, 80), "exit"
    elif comp["concentration"] >= 15 and comp["suspicious"] >= 8:
        situation, score, response = "concentrated_suspicious", min(max(raw, 50), 79), "reduce_size"
    elif comp["concentration"] >= 15:
        situation, score, response = "concentrated_inactive", min(max(raw, 30), 49), "monitor"
    else:
        situation, score, response = "low", min(raw, 29), "normal"
    return InsiderRisk(int(round(min(score, 100))), situation, response,
                       {k: round(v, 1) for k, v in comp.items()}, tuple(evidence), tuple(dict.fromkeys(gaps)))
