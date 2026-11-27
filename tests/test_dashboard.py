from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from atomtrader.dashboard.app import create_app
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.trading_store import TradingStore, pending_commands

TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def setup(tmp_path):
    db = f"sqlite+aiosqlite:///{tmp_path / 'dash.db'}"

    async def seed():
        repo = Repository(db)
        await repo.init()
        st = TradingStore(repo.sessionmaker)
        now = datetime.now(UTC)
        await st.save_state("dash:account", {"label": "PAPER — simulated funds", "account_kind": "paper",
                                             "at": now.isoformat(), "equity_eur": "1003.20", "cash_eur": "903.20"}, now)
        await st.save_state("controls:paper", {"emergency_stop": False, "autonomous": True, "paused_chains": []}, now)
        await repo.close()

    asyncio.run(seed())
    return db


def test_requires_token_and_refuses_weak_token(setup) -> None:
    with pytest.raises(SystemExit):
        create_app(setup, "short")
    with TestClient(create_app(setup, TOKEN)) as c:
        assert c.get("/").status_code == 200  # static shell carries no data
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/api/command", json={"action": "pause"}).status_code == 401


def test_state_is_labelled_paper_and_engine_heartbeat_reported(setup) -> None:
    with TestClient(create_app(setup, TOKEN)) as c:
        r = c.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        s = r.json()
        assert s["label"].startswith("PAPER") and s["account"]["equity_eur"] == "1003.20"
        assert s["engine"]["stale"] is False and s["controls"]["autonomous"] is True
        assert s["risk_limits"]["max_daily_loss_pct"] == "3.0" and s["performance"]["overall"]["trades"] == 0


def test_commands_are_validated_and_queued_not_executed(setup) -> None:
    h = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(setup, TOKEN)) as c:
        assert c.post("/api/command", json={"action": "withdraw", "args": {}}, headers=h).status_code == 400
        assert c.post("/api/command", json={"action": "set_risk", "args": {"max_risk_per_trade_pct": 9}}, headers=h).status_code == 400
        assert c.post("/api/command", json={"action": "emergency_stop", "args": {"reason": "t"}}, headers=h).json()["queued"] >= 1

    async def check():
        repo = Repository(setup)
        rows = await pending_commands(TradingStore(repo.sessionmaker))
        await repo.close()
        return rows

    rows = asyncio.run(check())
    assert [r.action for r in rows] == ["emergency_stop"] and rows[0].actor == "dashboard"


def test_no_attacker_controlled_data_inside_inline_handlers() -> None:
    """Token symbols are attacker-controlled. HTML-escaping does not protect a JavaScript
    string inside an inline event handler (the browser decodes entities first) — an audit
    reproduced script execution via a crafted symbol. Data must travel via data-* attributes."""
    import re
    from pathlib import Path

    html = (Path(__file__).parent.parent / "atomtrader" / "dashboard" / "static" / "index.html").read_text()
    handlers = re.findall(r'on\w+="[^"]*"', html) + re.findall(r"on\w+=\\\\?'[^']*'", html)
    assert not [h for h in handlers if "esc(" in h], handlers
    assert 'class="close-pos"' in html and "dataset.pid" in html
