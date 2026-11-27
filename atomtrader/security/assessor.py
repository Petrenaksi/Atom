"""Token security assessment: merges sources, applies hard-fail policy, caches results.

Two tiers.

Confirmed hard failures (block entries AND make the exit engine leave an open position):
  * honeypot / simulated sell failure / cannot sell (GoPlus, Honeypot.is)
  * no executable sell route at a small size (Jupiter, Solana)
  * freeze authority active (Solana: the issuer can freeze holders' tokens)
  * owner can change balances or modify taxes (EVM)
  * buy or sell tax above policy
  * RugCheck marks the token as rugged

Unverified (block NEW entries only — a temporary API outage is not evidence of a scam, so it
never forces an exit):
  * no security source answered at all
  * EVM: no sell simulation available (Honeypot.is could not simulate this pair)
  * Solana: the sell-route probe could not run
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from atomtrader.core.models import Chain, TokenRef
from atomtrader.core.money import MissingMarketData
from atomtrader.execution.base import NoRoute, QuoteUnavailable, SizedQuoter
from atomtrader.security.models import HolderKind, SecurityReport
from atomtrader.security.sources import SecuritySources, apply_goplus, apply_honeypot, apply_rugcheck, apply_solana_mint


@dataclass(frozen=True)
class SecurityPolicy:
    max_buy_tax_pct: Decimal = Decimal(5)
    max_sell_tax_pct: Decimal = Decimal(5)
    freeze_authority_is_hard_fail: bool = True
    sell_route_probe_usd: Decimal = Decimal(20)
    cache_seconds: int = 600


class SecurityAssessor:
    def __init__(
        self,
        sources: SecuritySources,
        solana_quoter: SizedQuoter | None = None,
        policy: SecurityPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sources = sources
        self.quoter = solana_quoter
        self.policy = policy or SecurityPolicy()
        self.clock = clock
        self._cache: dict[str, SecurityReport] = {}

    async def assess(self, token: TokenRef, force: bool = False, pool_addresses: Iterable[str] = ()) -> SecurityReport:
        """``pool_addresses``: the token's known pool/pair addresses (from market data). On EVM
        chains the pool contract holds the tokens, so such holders are relabelled as AMM
        instead of counting as concentration. Unidentified contracts still count."""
        cached = self._cache.get(token.key)
        now = self.clock()
        if cached and not force and (now - cached.checked_at).total_seconds() < self.policy.cache_seconds:
            return cached
        report = SecurityReport(token=token, checked_at=now)
        if token.chain is Chain.SOLANA:
            apply_rugcheck(report, await self.sources.rugcheck_report(token.address))
            if report.sources.get("rugcheck") != "ok" or report.freeze_authority_active is None:
                apply_solana_mint(report, await self.sources.solana_mint(token.address))
            await self._probe_sell_route(report)
        else:
            apply_goplus(report, await self.sources.goplus_token(token.chain.value, token.address), token.address)
            apply_honeypot(report, await self.sources.honeypot_check(token.chain.value, token.address))
        pools = {a.lower() for a in pool_addresses}
        if pools:
            report.holders = [replace(h, kind=HolderKind.AMM) if h.address.lower() in pools else h
                              for h in report.holders]
        self.apply_policy(report)
        self._cache[token.key] = report
        return report

    async def _probe_sell_route(self, report: SecurityReport) -> None:
        if self.quoter is None:
            report.sources["sell_route"] = "n/a"
            return
        try:
            qty, _ = await self.quoter.buy(report.token.address, self.policy.sell_route_probe_usd, 500)
            await self.quoter.sell(report.token.address, qty, 500)
            report.sell_route_ok = True
            report.sources["sell_route"] = "ok"
        except NoRoute as exc:
            report.sell_route_ok = False
            report.sources["sell_route"] = "ok"
            report.warnings.append(f"no_route_detail:{str(exc)[:80]}")
        except (QuoteUnavailable, MissingMarketData) as exc:
            report.sources["sell_route"] = f"error: {str(exc)[:120]}"

    def apply_policy(self, r: SecurityReport) -> None:
        p = self.policy
        if not r.verified:
            r.unverified.append("no_security_source_answered")
        if r.sell_route_ok is False:
            r.hard_fail.append("no_sell_route")
        if r.token.chain is Chain.SOLANA and r.sell_route_ok is None:
            r.unverified.append("sell_route_unverified")
        if r.token.chain is not Chain.SOLANA and r.sources.get("honeypot_is") != "ok":
            r.unverified.append("sell_simulation_unavailable")
        if r.freeze_authority_active and p.freeze_authority_is_hard_fail:
            r.hard_fail.append("freeze_authority_active")
        if r.mint_authority_active:
            r.warnings.append("mint_authority_active")
        if r.sell_tax_pct is not None and r.sell_tax_pct > p.max_sell_tax_pct:
            r.hard_fail.append(f"sell_tax_too_high:{r.sell_tax_pct:.1f}%")
        if r.buy_tax_pct is not None and r.buy_tax_pct > p.max_buy_tax_pct:
            r.hard_fail.append(f"buy_tax_too_high:{r.buy_tax_pct:.1f}%")
        r.hard_fail = list(dict.fromkeys(r.hard_fail))
        r.unverified = list(dict.fromkeys(r.unverified))
        r.warnings = list(dict.fromkeys(r.warnings))
