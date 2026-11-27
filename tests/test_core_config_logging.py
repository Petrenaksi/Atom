from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from atomtrader.config import (
    LIVE_AUTHORIZATION_PHRASE,
    LiveTradingNotAuthorized,
    RiskLimits,
    Settings,
    TradingMode,
    assert_live_authorized,
)
from atomtrader.core.money import D, FxRate, FxTable, MissingMarketData, bps, round_eur
from atomtrader.logging import REDACTED, redact_secrets, register_secret
from tests.conftest import T0

# ---------------------------------------------------------------- money


def test_money_refuses_floats() -> None:
    with pytest.raises(TypeError):
        D(0.1)  # type: ignore[arg-type]
    assert D("0.1") + D("0.2") == Decimal("0.3")
    assert bps(50) == Decimal("0.005")
    assert round_eur(Decimal("1.005")) == Decimal("1.00")  # banker's rounding


def test_fx_missing_and_stale_rates_raise_instead_of_guessing() -> None:
    table = FxTable(max_age_seconds=60)
    with pytest.raises(MissingMarketData):
        table.get("USD", T0)
    table.update(FxRate("USD", Decimal("0.9"), T0, "ecb"))
    assert table.get("usd", T0 + timedelta(seconds=30)).to_eur(Decimal(10)) == Decimal("9.0")
    with pytest.raises(MissingMarketData):
        table.get("USD", T0 + timedelta(seconds=61))
    assert table.get("EUR", T0).eur_per_unit == 1


# ---------------------------------------------------------------- settings


def test_default_settings_are_paper_and_match_mandate() -> None:
    s = Settings(_env_file=None)
    assert s.mode is TradingMode.PAPER
    r = s.risk
    assert (r.min_risk_per_trade_pct, r.max_risk_per_trade_pct) == (Decimal("0.5"), Decimal("1.0"))
    assert r.max_daily_loss_pct == 3 and r.max_open_positions == 3 and r.max_trades_per_day == 20
    assert r.consecutive_losses_for_cooldown == 3 and r.leverage == 1


@pytest.mark.parametrize(
    "override",
    [
        {"leverage": Decimal(2)},
        {"max_risk_per_trade_pct": Decimal("5")},
        {"max_daily_loss_pct": Decimal("25")},
        {"max_total_exposure_pct": Decimal("150")},
        {"max_trades_per_day": 500},
        {"min_risk_per_trade_pct": Decimal("1.5"), "max_risk_per_trade_pct": Decimal("1.0")},
    ],
)
def test_risk_limits_enforce_hard_ceilings(override: dict) -> None:
    with pytest.raises(ValidationError):
        RiskLimits(**override)


def test_risk_limits_are_immutable() -> None:
    limits = RiskLimits()
    with pytest.raises(ValidationError):
        limits.max_daily_loss_pct = Decimal(9)  # type: ignore[misc]


def test_live_mode_requires_explicit_authorization() -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        assert_live_authorized(Settings(_env_file=None))
    with pytest.raises(LiveTradingNotAuthorized):
        assert_live_authorized(Settings(_env_file=None, mode="live", live_authorization=SecretStr("yes")))
    with pytest.raises(LiveTradingNotAuthorized):  # phrase right, but no capital cap
        assert_live_authorized(
            Settings(_env_file=None, mode="live", live_authorization=SecretStr(LIVE_AUTHORIZATION_PHRASE))
        )
    assert_live_authorized(
        Settings(
            _env_file=None,
            mode="live",
            live_authorization=SecretStr(LIVE_AUTHORIZATION_PHRASE),
            live_max_capital_eur=Decimal(100),
        )
    )


# ---------------------------------------------------------------- logging redaction


def _redact(**event: object) -> dict:
    return redact_secrets(None, "info", dict(event))


def test_redacts_registered_secret_anywhere() -> None:
    register_secret("my-api-secret-value-123")
    out = _redact(event="calling with my-api-secret-value-123 inside", nested={"x": ["my-api-secret-value-123"]})
    assert "my-api-secret-value-123" not in json.dumps(out)


def test_redacts_key_shaped_values_and_sensitive_keys() -> None:
    evm_key = "0x" + "4c" * 32
    sol_bytes = "[" + ",".join(["17"] * 64) + "]"
    sol_b58 = "5" + "K" * 86
    mnemonic = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    out = _redact(event=f"key={evm_key}", a=sol_bytes, b=sol_b58, c=mnemonic, private_key="anything", apiKey="x")
    dumped = json.dumps(out)
    for leaked in (evm_key, sol_b58, mnemonic, "anything"):
        assert leaked not in dumped
    assert out["a"] == REDACTED and out["private_key"] == REDACTED and out["apiKey"] == REDACTED


def test_keeps_addresses_tx_refs_and_prose() -> None:
    address = "0x" + "ab" * 20
    sol_pubkey = "So1anaMemeMint1111111111111111111111111111"
    tx_hash = "0x" + "9f" * 32
    prose = "rejected because the daily loss limit was hit and the cooldown is still active now"
    out = _redact(event="fill", token=address, mint=sol_pubkey, tx_hash=tx_hash, why=prose)
    assert out["token"] == address and out["mint"] == sol_pubkey
    assert out["tx_hash"] == tx_hash
    assert out["why"] == prose
