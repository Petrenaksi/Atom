"""Security data model shared by the adapters, the assessor and the insider-risk analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from atomtrader.core.models import TokenRef


class HolderKind(StrEnum):
    AMM = "amm"  # liquidity pool / bonding curve vault — not a person
    LOCKER = "locker"  # lock / vesting / staking contract
    CONTRACT = "contract"  # other contract (EVM), purpose unknown
    CREATOR = "creator"
    UNKNOWN = "unknown"  # wallet with no label (could be a person, an exchange, a fund)

    @property
    def is_infrastructure(self) -> bool:
        return self in (HolderKind.AMM, HolderKind.LOCKER)


@dataclass(frozen=True, slots=True)
class Holder:
    address: str
    pct: Decimal  # 0..100 of supply
    kind: HolderKind = HolderKind.UNKNOWN
    insider: bool | None = None  # flagged by a source as insider-linked; None = unknown
    locked: bool | None = None


@dataclass
class SecurityReport:
    token: TokenRef
    checked_at: datetime
    sources: dict[str, str] = field(default_factory=dict)  # source -> "ok" | "error: ..." | "n/a"
    hard_fail: list[str] = field(default_factory=list)  # confirmed problems: block entries AND exit
    unverified: list[str] = field(default_factory=list)  # could not verify: block entries only
    warnings: list[str] = field(default_factory=list)
    buy_tax_pct: Decimal | None = None
    sell_tax_pct: Decimal | None = None
    mint_authority_active: bool | None = None
    freeze_authority_active: bool | None = None
    holders: list[Holder] = field(default_factory=list)
    total_holders: int | None = None
    creator_pct: Decimal | None = None
    insider_network_pct: Decimal | None = None  # supply currently held by linked wallets
    insider_network_wallets: int | None = None
    lp_locked_pct: Decimal | None = None
    rugcheck_score: Decimal | None = None  # 0..100, higher = riskier (RugCheck's own scale)
    sell_route_ok: bool | None = None
    simulated_sell_failures: int | None = None

    @property
    def passed(self) -> bool:
        """No confirmed security problem. (An open position is exited when this is False.)"""
        return not self.hard_fail

    @property
    def entry_allowed(self) -> bool:
        """New entries need a clean AND verified result — an unverifiable sell blocks entry."""
        return not self.hard_fail and not self.unverified

    @property
    def verified(self) -> bool:
        return any(v == "ok" for v in self.sources.values())

    def top_pct(self, n: int = 10, exclude_infrastructure: bool = True) -> Decimal | None:
        if not self.holders:
            return None
        hs = [h for h in self.holders if not (exclude_infrastructure and h.kind.is_infrastructure)]
        return sum((h.pct for h in sorted(hs, key=lambda h: -h.pct)[:n]), Decimal(0))


@dataclass(frozen=True, slots=True)
class HolderSnapshot:
    at: datetime
    holders: tuple[Holder, ...]
    total_holders: int | None
    creator_pct: Decimal | None
