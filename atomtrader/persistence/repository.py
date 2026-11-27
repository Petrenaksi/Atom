"""Persistent store for orders, fills, decisions and control/risk events.

Works on PostgreSQL (asyncpg) in production and SQLite (aiosqlite) in tests. Decimals are
stored as strings so no precision is lost on any backend.

Idempotency: ``orders.client_order_id`` is the primary key. An order row is written as
PENDING *before* anything is sent, then SUBMITTED immediately before the send. After a
crash, PENDING rows were provably never sent; SUBMITTED rows must be reconciled with the
provider/chain before anything else trades that token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    TypeDecorator,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from atomtrader.core.models import (
    AccountKind,
    Chain,
    ExecutionResult,
    Fill,
    OrderRequest,
    OrderStatus,
    Side,
    TokenRef,
)
from atomtrader.core.money import FxRate

FINAL_STATUSES = (OrderStatus.FILLED.value, OrderStatus.FAILED.value, OrderStatus.REJECTED.value)


class DecimalString(TypeDecorator[Decimal]):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"
    client_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    position_id: Mapped[str] = mapped_column(String(128), index=True)
    chain: Mapped[str] = mapped_column(String(16))
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    notional_eur: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    max_slippage_bps: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(512))
    account_kind: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    failed_fee_eur: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    realized_slippage_bps: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FillRow(Base):
    __tablename__ = "fills"
    client_order_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("orders.client_order_id"), primary_key=True
    )
    position_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(DecimalString)
    price_usd: Mapped[Decimal] = mapped_column(DecimalString)
    gross_eur: Mapped[Decimal] = mapped_column(DecimalString)
    fee_eur: Mapped[Decimal] = mapped_column(DecimalString)
    fx_currency: Mapped[str] = mapped_column(String(8))
    fx_eur_per_unit: Mapped[Decimal] = mapped_column(DecimalString)
    fx_as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    fx_source: Mapped[str] = mapped_column(String(64))
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    tx_ref: Mapped[str] = mapped_column(String(160))
    account_kind: Mapped[str] = mapped_column(String(8))


class DecisionRow(Base):
    """Every entry/rejection decision with its evidence, for audit and self-analysis."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    chain: Mapped[str] = mapped_column(String(16))
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    approved: Mapped[bool] = mapped_column(Boolean)
    notional_eur: Mapped[Decimal] = mapped_column(DecimalString)
    risk_eur: Mapped[Decimal] = mapped_column(DecimalString)
    reasons: Mapped[list[str]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    account_kind: Mapped[str] = mapped_column(String(8))


class EventRow(Base):
    """Control actions (pause, kill switch, blacklist) and risk events (daily limit, cooldown)."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(String(2048))
    actor: Mapped[str] = mapped_column(String(64))


class Repository:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url)
        self._session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------ orders
    async def insert_order(self, order: OrderRequest, account_kind: AccountKind, now: datetime) -> bool:
        """Insert as PENDING. Returns False if the client_order_id already exists."""
        async with self._session() as s:
            seq = (await s.execute(select(OrderRow.seq).order_by(OrderRow.seq.desc()).limit(1))).scalar()
            s.add(
                OrderRow(
                    client_order_id=order.client_order_id,
                    seq=(seq or 0) + 1,
                    position_id=order.position_id,
                    chain=order.token.chain.value,
                    token_address=order.token.address,
                    symbol=order.token.symbol,
                    side=order.side.value,
                    notional_eur=order.notional_eur,
                    quantity=order.quantity,
                    max_slippage_bps=order.max_slippage_bps,
                    reason=order.reason[:512],
                    account_kind=account_kind.value,
                    status=OrderStatus.PENDING.value,
                    failed_fee_eur=Decimal(0),
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return False
        return True

    async def get_order_status(self, client_order_id: str) -> OrderStatus | None:
        async with self._session() as s:
            row = await s.get(OrderRow, client_order_id)
            return OrderStatus(row.status) if row else None

    async def mark_submitted(self, client_order_id: str, now: datetime) -> None:
        async with self._session() as s:
            row = await s.get(OrderRow, client_order_id)
            if row is None or row.status != OrderStatus.PENDING.value:
                raise RuntimeError(f"order {client_order_id} not pending")
            row.status = OrderStatus.SUBMITTED.value
            row.updated_at = now
            await s.commit()

    async def save_result(self, result: ExecutionResult, now: datetime) -> None:
        async with self._session() as s:
            row = await s.get(OrderRow, result.client_order_id)
            if row is None:
                raise RuntimeError(f"unknown order {result.client_order_id}")
            if row.status in FINAL_STATUSES:
                return  # already final; idempotent
            row.status = result.status.value
            row.failed_fee_eur = result.failed_fee_eur
            row.realized_slippage_bps = result.realized_slippage_bps
            row.error = (result.error or None) and result.error[:512]
            row.updated_at = now
            if result.fill is not None:
                f = result.fill
                s.add(
                    FillRow(
                        client_order_id=f.client_order_id,
                        position_id=f.position_id,
                        side=f.side.value,
                        quantity=f.quantity,
                        price_usd=f.price_usd,
                        gross_eur=f.gross_eur,
                        fee_eur=f.fee_eur,
                        fx_currency=f.fx.currency,
                        fx_eur_per_unit=f.fx.eur_per_unit,
                        fx_as_of=f.fx.as_of,
                        fx_source=f.fx.source,
                        executed_at=f.executed_at,
                        tx_ref=f.tx_ref,
                        account_kind=f.account_kind.value,
                    )
                )
            await s.commit()

    async def unreconciled_orders(self) -> list[OrderRow]:
        async with self._session() as s:
            rows = await s.execute(
                select(OrderRow)
                .where(OrderRow.status.in_([OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value]))
                .order_by(OrderRow.seq)
            )
            return list(rows.scalars())

    async def load_final_results(self, account_kind: AccountKind) -> list[ExecutionResult]:
        """All final results in submission order, for rebuilding the ledger."""
        async with self._session() as s:
            orders = (
                await s.execute(
                    select(OrderRow)
                    .where(OrderRow.account_kind == account_kind.value)
                    .where(OrderRow.status.in_(FINAL_STATUSES))
                    .order_by(OrderRow.seq)
                )
            ).scalars()
            fills = {
                f.client_order_id: f
                for f in (
                    await s.execute(select(FillRow).where(FillRow.account_kind == account_kind.value))
                ).scalars()
            }
            out: list[ExecutionResult] = []
            for o in orders:
                token = TokenRef(Chain(o.chain), o.token_address, o.symbol)
                fr = fills.get(o.client_order_id)
                fill = None
                if fr is not None:
                    fill = Fill(
                        client_order_id=fr.client_order_id,
                        position_id=fr.position_id,
                        token=token,
                        side=Side(fr.side),
                        quantity=fr.quantity,
                        price_usd=fr.price_usd,
                        gross_eur=fr.gross_eur,
                        fee_eur=fr.fee_eur,
                        fx=FxRate(fr.fx_currency, fr.fx_eur_per_unit, fr.fx_as_of, fr.fx_source),
                        executed_at=fr.executed_at,
                        tx_ref=fr.tx_ref,
                        account_kind=AccountKind(fr.account_kind),
                    )
                out.append(
                    ExecutionResult(
                        client_order_id=o.client_order_id,
                        status=OrderStatus(o.status),
                        account_kind=AccountKind(o.account_kind),
                        fill=fill,
                        failed_fee_eur=o.failed_fee_eur,
                        realized_slippage_bps=o.realized_slippage_bps,
                        error=o.error,
                    )
                )
            return out

    # --------------------------------------------------------------- audit log
    async def log_decision(
        self,
        *,
        at: datetime,
        token: TokenRef,
        strategy: str,
        approved: bool,
        notional_eur: Decimal,
        risk_eur: Decimal,
        reasons: list[str],
        evidence: dict[str, Any],
        account_kind: AccountKind,
    ) -> None:
        # Round-trip through JSON with str() so Decimals/datetimes in evidence serialize.
        safe_evidence = json.loads(json.dumps(evidence, default=str))
        async with self._session() as s:
            s.add(
                DecisionRow(
                    at=at,
                    chain=token.chain.value,
                    token_address=token.address,
                    symbol=token.symbol,
                    strategy=strategy,
                    approved=approved,
                    notional_eur=notional_eur,
                    risk_eur=risk_eur,
                    reasons=reasons,
                    evidence=safe_evidence,
                    account_kind=account_kind.value,
                )
            )
            await s.commit()

    async def log_event(self, at: datetime, kind: str, detail: str, actor: str = "system") -> None:
        async with self._session() as s:
            s.add(EventRow(at=at, kind=kind, detail=detail[:2048], actor=actor))
            await s.commit()

    async def events(self, kind: str | None = None) -> list[EventRow]:
        async with self._session() as s:
            q = select(EventRow).order_by(EventRow.id)
            if kind:
                q = q.where(EventRow.kind == kind)
            return list((await s.execute(q)).scalars())

    async def decisions(self) -> list[DecisionRow]:
        async with self._session() as s:
            return list((await s.execute(select(DecisionRow).order_by(DecisionRow.id))).scalars())
