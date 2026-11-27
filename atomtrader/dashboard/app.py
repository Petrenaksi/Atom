"""Dashboard: a separate process that only reads the database and enqueues operator commands.

The engine keeps trading and managing positions whether or not this process runs. The
dashboard never holds wallet keys and cannot place orders itself; control actions go
through the engine's command queue and are audited there.

    ATOM_DASHBOARD_TOKEN=... python -m atomtrader.dashboard.app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import argparse
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from atomtrader.analytics.performance import breakdown, metrics
from atomtrader.config.settings import RiskLimits
from atomtrader.engine.orchestrator import _trade_from_row
from atomtrader.persistence.repository import EventRow, Repository
from atomtrader.persistence.trading_store import CommandRow, TradingStore, enqueue_command, load_trade_records

STATIC = Path(__file__).parent / "static"
ALLOWED_ACTIONS = {"emergency_stop", "clear_emergency_stop", "pause", "resume", "autonomous", "pause_chain",
                   "resume_chain", "blacklist", "close_position", "set_risk"}
STALE_AFTER_SECONDS = 300


class Command(BaseModel):
    action: str
    args: dict[str, Any] = {}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float) and v == float("inf"):
        return "inf"
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "__dataclass_fields__"):
        return _jsonable(v.__dict__)
    return v


def create_app(database_url: str, token: str) -> FastAPI:
    if not token or len(token) < 16:
        raise SystemExit("ATOM_DASHBOARD_TOKEN must be set (>= 16 characters)")
    repo = Repository(database_url)
    store = TradingStore(repo.sessionmaker)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repo.init()
        yield
        await repo.close()

    app = FastAPI(title="Atom trader dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def auth(authorization: str = Header(default="")) -> None:
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state() -> dict[str, Any]:
        now = datetime.now(UTC)
        account = await store.load_state("dash:account") or {}
        positions = await store.load_state("dash:positions") or {"positions": []}
        opps = await store.load_state("dash:opportunities") or {}
        controls = await store.load_state("controls:paper") or {}
        override = await store.load_state("risk_limits_override")
        limits = RiskLimits(**override) if override else RiskLimits()
        rows = await load_trade_records(store, "paper")
        trades = [_trade_from_row(r.data) for r in rows]
        async with repo.sessionmaker() as s:
            events = list((await s.execute(select(EventRow).order_by(EventRow.id.desc()).limit(60))).scalars())
            cmds = list((await s.execute(select(CommandRow).order_by(CommandRow.id.desc()).limit(20))).scalars())
        heartbeat = account.get("at")
        stale = heartbeat is None or (now - datetime.fromisoformat(heartbeat)).total_seconds() > STALE_AFTER_SECONDS
        return _jsonable({
            "label": account.get("label", "PAPER — simulated funds"),
            "engine": {"heartbeat": heartbeat, "stale": stale},
            "account": account,
            "positions": positions.get("positions", []),
            "opportunities": opps,
            "performance": {
                "overall": metrics(trades), "by_strategy": breakdown(trades, "strategy"),
                "by_chain": breakdown(trades, "chain"), "by_mode": breakdown(trades, "max_mode"),
                "recent_trades": [t.__dict__ for t in trades[-25:]][::-1],
            },
            "controls": controls,
            "risk_limits": limits.model_dump(mode="json"),
            "events": [{"at": e.at, "kind": e.kind, "detail": e.detail, "actor": e.actor} for e in events],
            "commands": [{"id": c.id, "at": c.created_at, "action": c.action, "args": c.args, "status": c.status,
                          "result": c.result} for c in cmds],
        })

    @app.post("/api/command", dependencies=[Depends(auth)])
    async def command(cmd: Command) -> dict[str, Any]:
        if cmd.action not in ALLOWED_ACTIONS:
            raise HTTPException(status_code=400, detail=f"unknown action {cmd.action}")
        if cmd.action == "set_risk":
            try:  # validate early for a clear error; the engine validates again
                RiskLimits(**{**RiskLimits().model_dump(), **cmd.args})
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
        cid = await enqueue_command(store, cmd.action, cmd.args, "dashboard", datetime.now(UTC))
        return {"queued": cid}

    return app


def main() -> None:
    import uvicorn

    from atomtrader.config.settings import Settings

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    settings = Settings()
    app = create_app(settings.database_url, os.environ.get("ATOM_DASHBOARD_TOKEN", ""))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
