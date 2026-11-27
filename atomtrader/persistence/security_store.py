"""Persistence for security reports and holder snapshots (needed to *measure* distribution
by comparing holdings over time rather than assuming it)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from atomtrader.persistence.repository import Base, DecimalString, UTCDateTime
from atomtrader.security.models import Holder, HolderKind, HolderSnapshot, SecurityReport


class SecurityReportRow(Base):
    __tablename__ = "security_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    token_key: Mapped[str] = mapped_column(String(160), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    hard_fail: Mapped[list[str]] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON)
    sources: Mapped[dict[str, str]] = mapped_column(JSON)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON)


class HolderSnapshotRow(Base):
    __tablename__ = "holder_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    token_key: Mapped[str] = mapped_column(String(160), index=True)
    holders: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    total_holders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator_pct: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)


def _s(v: Any) -> Any:
    return None if v is None else str(v)


class SecurityStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._session = sessionmaker

    async def save(self, r: SecurityReport) -> None:
        summary = {
            "buy_tax_pct": _s(r.buy_tax_pct), "sell_tax_pct": _s(r.sell_tax_pct),
            "mint_authority_active": r.mint_authority_active, "freeze_authority_active": r.freeze_authority_active,
            "top10_excl_pools_pct": _s(r.top_pct(10)), "creator_pct": _s(r.creator_pct),
            "insider_network_pct": _s(r.insider_network_pct), "lp_locked_pct": _s(r.lp_locked_pct),
            "rugcheck_score": _s(r.rugcheck_score), "total_holders": r.total_holders, "sell_route_ok": r.sell_route_ok,
            "unverified": list(r.unverified),
        }
        async with self._session() as s:
            s.add(SecurityReportRow(at=r.checked_at, token_key=r.token.key, passed=r.passed, hard_fail=list(r.hard_fail),
                                    warnings=list(r.warnings), sources=dict(r.sources), summary=summary))
            if r.holders:
                s.add(HolderSnapshotRow(
                    at=r.checked_at, token_key=r.token.key, total_holders=r.total_holders, creator_pct=r.creator_pct,
                    holders=[{"address": h.address, "pct": str(h.pct), "kind": h.kind.value, "insider": h.insider}
                             for h in r.holders]))
            await s.commit()

    async def holder_history(self, token_key: str, since: datetime | None = None) -> list[HolderSnapshot]:
        async with self._session() as s:
            q = select(HolderSnapshotRow).where(HolderSnapshotRow.token_key == token_key)
            if since is not None:
                q = q.where(HolderSnapshotRow.at >= since)
            rows = (await s.execute(q.order_by(HolderSnapshotRow.at))).scalars()
            return [
                HolderSnapshot(
                    at=r.at,
                    holders=tuple(Holder(h["address"], Decimal(h["pct"]), HolderKind(h["kind"]), h.get("insider"))
                                  for h in r.holders),
                    total_holders=r.total_holders,
                    creator_pct=r.creator_pct,
                )
                for r in rows
            ]
