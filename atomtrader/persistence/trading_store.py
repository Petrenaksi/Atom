"""Trading-session state that must survive restarts.

* ``state_kv``: operator controls (emergency stop, pauses, blacklist, autonomous flag)
  and the daily risk state. An emergency stop set before a crash is still set after it.
* ``positions_meta``: per-position thesis (strategy, narrative, stop, evidence) — the
  quantities and P&L themselves are always rebuilt from fills, never stored twice.
* ``equity_snapshots``: periodic mark-to-market, always tagged paper/live.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from atomtrader.persistence.repository import Base, DecimalString, UTCDateTime


class StateRow(Base):
    __tablename__ = "state_kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PositionMetaRow(Base):
    __tablename__ = "positions_meta"
    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8))
    chain: Mapped[str] = mapped_column(String(16))
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    narrative: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stop_price_usd: Mapped[Decimal] = mapped_column(DecimalString)
    est_fee_pct_per_side: Mapped[Decimal] = mapped_column(DecimalString)
    est_network_fee_eur: Mapped[Decimal] = mapped_column(DecimalString)
    thesis: Mapped[dict[str, Any]] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    account_kind: Mapped[str] = mapped_column(String(8))
    cash_eur: Mapped[Decimal] = mapped_column(DecimalString)
    invested_eur: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    equity_eur: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    realized_pnl_eur: Mapped[Decimal] = mapped_column(DecimalString)
    unrealized_pnl_eur: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    total_fees_eur: Mapped[Decimal] = mapped_column(DecimalString)
    open_positions: Mapped[int] = mapped_column(Integer)
    fully_priced: Mapped[bool] = mapped_column(Boolean)
    unpriced: Mapped[list[str]] = mapped_column(JSON)


class TradingStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._session = sessionmaker

    async def save_state(self, key: str, value: dict[str, Any], now: datetime) -> None:
        async with self._session() as s:
            row = await s.get(StateRow, key)
            if row is None:
                s.add(StateRow(key=key, value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now
            await s.commit()

    async def load_state(self, key: str) -> dict[str, Any] | None:
        async with self._session() as s:
            row = await s.get(StateRow, key)
            return None if row is None else dict(row.value)

    async def save_position_meta(self, row: PositionMetaRow) -> None:
        async with self._session() as s:
            s.add(row)
            await s.commit()

    async def position_meta(self) -> dict[str, PositionMetaRow]:
        async with self._session() as s:
            return {r.position_id: r for r in (await s.execute(select(PositionMetaRow))).scalars()}

    async def save_equity_snapshot(self, row: EquitySnapshotRow) -> None:
        async with self._session() as s:
            s.add(row)
            await s.commit()

    async def equity_snapshots(self) -> list[EquitySnapshotRow]:
        async with self._session() as s:
            return list((await s.execute(select(EquitySnapshotRow).order_by(EquitySnapshotRow.id))).scalars())


class CommandRow(Base):
    """Operator commands written by the dashboard and executed by the engine (the dashboard
    never touches the engine's memory or the wallet)."""

    __tablename__ = "commands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending | done | rejected
    result: Mapped[str | None] = mapped_column(String(512), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class TradeRecordRow(Base):
    __tablename__ = "trade_records"
    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8), index=True)
    closed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


async def enqueue_command(store: TradingStore, action: str, args: dict[str, Any], actor: str, now: datetime) -> int:
    async with store._session() as s:
        row = CommandRow(created_at=now, actor=actor, action=action, args=args, status="pending")
        s.add(row)
        await s.commit()
        return row.id


async def pending_commands(store: TradingStore) -> list[CommandRow]:
    async with store._session() as s:
        q = select(CommandRow).where(CommandRow.status == "pending").order_by(CommandRow.id)
        return list((await s.execute(q)).scalars())


async def complete_command(store: TradingStore, cid: int, status: str, result: str, now: datetime) -> None:
    async with store._session() as s:
        row = await s.get(CommandRow, cid)
        if row is not None:
            row.status, row.result, row.processed_at = status, result[:512], now
            await s.commit()


async def save_trade_record(store: TradingStore, pid: str, account_kind: str, closed_at: datetime, data: dict[str, Any]) -> None:
    async with store._session() as s:
        if await s.get(TradeRecordRow, pid) is None:
            s.add(TradeRecordRow(position_id=pid, account_kind=account_kind, closed_at=closed_at, data=data))
            await s.commit()


async def load_trade_records(store: TradingStore, account_kind: str, since: datetime | None = None) -> list[TradeRecordRow]:
    async with store._session() as s:
        q = select(TradeRecordRow).where(TradeRecordRow.account_kind == account_kind)
        if since is not None:
            q = q.where(TradeRecordRow.closed_at >= since)
        return list((await s.execute(q.order_by(TradeRecordRow.closed_at))).scalars())
