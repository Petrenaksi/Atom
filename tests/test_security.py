"""Security assessment against recorded real responses + insider-risk situations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from atomtrader.core.models import Chain, TokenRef
from atomtrader.execution.base import NoRoute, QuoteUnavailable
from atomtrader.persistence.repository import Repository
from atomtrader.persistence.security_store import SecurityStore
from atomtrader.security.assessor import SecurityAssessor
from atomtrader.security.insider import insider_exit_risk
from atomtrader.security.models import Holder, HolderKind, HolderSnapshot, SecurityReport
from atomtrader.security.sources import apply_goplus, apply_honeypot, apply_rugcheck

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
BONK = TokenRef(Chain.SOLANA, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "Bonk")
FRESH = TokenRef(Chain.SOLANA, "CGEYuiXo5BHZRr9QNmdGz4DqXJ7UvWCP5jRCuFNgpump", "FRESH")
ELSA = TokenRef(Chain.BASE, "0x29cc30f9d113b356ce408667aa6433589cecbdca", "ELSA")


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class FakeSources:
    def __init__(self, rug=None, goplus=None, honeypot=None, mint=None) -> None:
        self.rug, self.gp, self.hp, self.mint = rug, goplus, honeypot, mint
        self.calls: list[str] = []

    async def rugcheck_report(self, mint):
        self.calls.append("rugcheck")
        return self.rug if self.rug is not None else {"error": "network_error: down"}

    async def goplus_token(self, chain, address):
        self.calls.append("goplus")
        return self.gp if self.gp is not None else {"error": "x"}

    async def honeypot_check(self, chain, address):
        self.calls.append("honeypot")
        return self.hp if self.hp is not None else {"error": "x"}

    async def solana_mint(self, mint):
        self.calls.append("rpc")
        return self.mint if self.mint is not None else {"error": "rate_limited"}


class Quoter:
    def __init__(self, sell_error=None, buy_error=None):
        self.sell_error, self.buy_error = sell_error, buy_error

    async def buy(self, address, spend_usd, slippage_bps):
        if self.buy_error:
            raise self.buy_error
        return Decimal(1000), {}

    async def sell(self, address, quantity, slippage_bps):
        if self.sell_error:
            raise self.sell_error
        return Decimal(19), {}


# ------------------------------------------------------------------ source parsing (real responses)


def test_rugcheck_bonk_parsing() -> None:
    r = SecurityReport(BONK, NOW)
    apply_rugcheck(r, fx("rugcheck_report_bonk.json"))
    assert r.sources["rugcheck"] == "ok" and not r.hard_fail
    assert r.mint_authority_active is False and r.freeze_authority_active is False
    assert r.total_holders == 2118573 and len(r.holders) == 20
    assert r.top_pct(10) == pytest.approx(Decimal("36.3"), abs=Decimal("0.1"))
    # 48 linked wallets holding ~0.09% of supply
    assert r.insider_network_wallets == 48 and Decimal("0.05") < r.insider_network_pct < Decimal("0.2")


def test_rugcheck_fresh_token_excludes_pool_from_concentration() -> None:
    r = SecurityReport(FRESH, NOW)
    apply_rugcheck(r, fx("rugcheck_report_fresh.json"))
    assert r.rugcheck_score == 1  # RugCheck's own score looks harmless...
    amm = [h for h in r.holders if h.kind is HolderKind.AMM]
    assert amm and amm[0].pct > 47  # ...the 47.6% top holder is the Pump.fun pool
    assert r.top_pct(10, exclude_infrastructure=False) > 70
    excl = r.top_pct(10)
    assert Decimal(20) < excl < Decimal(40)  # real-wallet concentration once the pool is excluded


def test_goplus_and_honeypot_parsing_for_clean_base_token() -> None:
    r = SecurityReport(ELSA, NOW)
    apply_goplus(r, fx("goplus_base_token.json"), ELSA.address)
    apply_honeypot(r, fx("honeypot_base_token.json"))
    assert r.sources == {"goplus": "ok", "honeypot_is": "ok"} and not r.hard_fail
    assert r.buy_tax_pct == 0 and r.sell_tax_pct == 0 and r.simulated_sell_failures == 0
    assert r.holders[0].kind is HolderKind.CONTRACT and r.holders[0].pct > 69


def test_goplus_malicious_permissions_are_hard_fails() -> None:
    raw = fx("goplus_base_token.json")
    info = raw["result"][ELSA.address]
    info.update({"is_honeypot": "1", "slippage_modifiable": "1", "owner_change_balance": "1",
                 "sell_tax": "0.25", "is_blacklisted": "1"})
    r = SecurityReport(ELSA, NOW)
    apply_goplus(r, raw, ELSA.address)
    assert {"goplus_honeypot", "goplus_tax_modifiable", "goplus_owner_can_change_balance"} <= set(r.hard_fail)
    assert "goplus:has_blacklist" in r.warnings and r.sell_tax_pct == 25


# ------------------------------------------------------------------ assessor policy


async def test_assessor_solana_clean_token_with_sell_route() -> None:
    a = SecurityAssessor(FakeSources(rug=fx("rugcheck_report_bonk.json")), Quoter(), clock=lambda: NOW)
    r = await a.assess(BONK)
    assert r.passed and r.sell_route_ok is True and r.sources["sell_route"] == "ok"
    again = await a.assess(BONK)  # cached
    assert again is r


async def test_assessor_hard_fails() -> None:
    no_route = SecurityAssessor(FakeSources(rug=fx("rugcheck_report_bonk.json")), Quoter(sell_error=NoRoute("x")), clock=lambda: NOW)
    assert "no_sell_route" in (await no_route.assess(BONK)).hard_fail

    rug = fx("rugcheck_report_bonk.json")
    rug["freezeAuthority"] = "SomeAuthority1111111111111111111111111111111"
    rug["transferFee"] = {"pct": 12}
    r = await SecurityAssessor(FakeSources(rug=rug), Quoter(), clock=lambda: NOW).assess(BONK)
    assert {"freeze_authority_active", "sell_tax_too_high:12.0%", "buy_tax_too_high:12.0%"} <= set(r.hard_fail)

    nothing = await SecurityAssessor(FakeSources(), None, clock=lambda: NOW).assess(BONK)
    assert nothing.hard_fail == [] and not nothing.verified and not nothing.entry_allowed
    assert {"no_security_source_answered", "sell_route_unverified"} <= set(nothing.unverified)
    assert nothing.passed  # unverified never forces an exit on its own


async def test_assessor_falls_back_to_rpc_and_tolerates_quote_outage() -> None:
    src = FakeSources(rug=None, mint=fx("solana_rpc_mint_bonk.json"))
    r = await SecurityAssessor(src, Quoter(buy_error=QuoteUnavailable("429")), clock=lambda: NOW).assess(BONK)
    assert src.calls == ["rugcheck", "rpc"]
    assert r.sources["solana_rpc"] == "ok" and r.freeze_authority_active is False
    assert r.sell_route_ok is None and r.sources["sell_route"].startswith("error")
    assert r.passed and not r.entry_allowed  # no confirmed problem, but entry needs a verified sell route
    assert r.unverified == ["sell_route_unverified"]


async def test_evm_assessor_uses_goplus_and_honeypot() -> None:
    hp = fx("honeypot_base_token.json")
    hp["honeypotResult"] = {"isHoneypot": True}
    src = FakeSources(goplus=fx("goplus_base_token.json"), honeypot=hp)
    r = await SecurityAssessor(src, clock=lambda: NOW).assess(ELSA)
    assert src.calls == ["goplus", "honeypot"]
    # GoPlus reports the token sellable and the simulation shows a 0 % tax: contested, so entry
    # is blocked (unverified) but a held position is not force-exited (audit change).
    assert "honeypot_verdicts_conflict" in r.unverified and not r.entry_allowed


# ------------------------------------------------------------------ insider exit risk situations


def report_with(holders: list[Holder], **kw) -> SecurityReport:
    r = SecurityReport(FRESH, NOW, sources={"rugcheck": "ok"})
    r.holders = holders
    r.insider_network_wallets = kw.pop("wallets", 0)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def H(addr: str, pct: str, kind=HolderKind.UNKNOWN, insider=None) -> Holder:
    return Holder(addr, Decimal(pct), kind, insider)


def flat_liq(v: int = 100_000) -> list:
    return [(NOW - timedelta(minutes=m), Decimal(v)) for m in (50, 30, 10, 0)]


def test_low_risk_distributed_token() -> None:
    r = report_with([H(f"w{i}", "1.5") for i in range(10)] + [H("pool", "40", HolderKind.AMM)])
    risk = insider_exit_risk(r, [], flat_liq(), NOW)
    assert risk.situation == "low" and risk.response == "normal" and risk.score < 30
    assert "holder_history" in risk.data_gaps


def test_concentration_without_activity_is_not_rejected() -> None:
    r = report_with([H("whale", "30"), H("w2", "10")] + [H(f"small{i}", "1") for i in range(8)])
    hist = [HolderSnapshot(NOW - timedelta(minutes=30), tuple(r.holders), None, None)]
    risk = insider_exit_risk(r, hist, flat_liq(), NOW)
    assert risk.situation == "concentrated_inactive" and risk.response == "monitor"
    assert 30 <= risk.score < 50  # below the risk engine's 50-point haircut threshold


def test_concentration_with_linked_wallets_reduces_size() -> None:
    r = report_with([H("whale", "30", insider=True), H("w2", "10")] + [H(f"small{i}", "1") for i in range(8)],
                    insider_network_pct=Decimal(12), wallets=40)
    risk = insider_exit_risk(r, [], flat_liq(), NOW)
    assert risk.situation == "concentrated_suspicious" and risk.response == "reduce_size"
    assert 50 <= risk.score < 80
    assert any("linked-wallet" in e for e in risk.evidence)


def test_confirmed_large_holder_selling_blocks_and_exits() -> None:
    before = [H("whale", "30"), H("w2", "10")] + [H(f"small{i}", "1") for i in range(8)]
    after = [H("whale", "24"), H("w2", "9")] + [H(f"small{i}", "1") for i in range(8)]
    hist = [HolderSnapshot(NOW - timedelta(minutes=20), tuple(before), None, None)]
    risk = insider_exit_risk(report_with(after), hist, flat_liq(), NOW)
    assert risk.situation == "large_holder_selling" and risk.response == "exit" and risk.score >= 80
    assert any("reduced by 7.0 pp" in e for e in risk.evidence)


def test_liquidity_deterioration_dominates() -> None:
    r = report_with([H(f"w{i}", "1.5") for i in range(10)])
    liq = [(NOW - timedelta(minutes=40), Decimal(200_000)), (NOW - timedelta(minutes=5), Decimal(150_000)),
           (NOW, Decimal(100_000))]
    risk = insider_exit_risk(r, [], liq, NOW)
    assert risk.situation == "liquidity_deterioration" and risk.score >= 85
    assert any("liquidity down 50%" in e for e in risk.evidence)


def test_missing_holder_data_is_cautious_not_zero() -> None:
    risk = insider_exit_risk(report_with([]), [], [], NOW)
    assert risk.components["concentration"] == 15.0
    assert set(risk.data_gaps) >= {"holders", "holder_history", "liquidity_history"}


async def test_holder_history_round_trip(tmp_path) -> None:
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    await repo.init()
    store = SecurityStore(repo.sessionmaker)
    r = SecurityReport(BONK, NOW)
    apply_rugcheck(r, fx("rugcheck_report_bonk.json"))
    await store.save(r)
    [snap] = await store.holder_history(BONK.key)
    assert snap.at == NOW and len(snap.holders) == 20 and snap.holders[0].pct == r.holders[0].pct
    await repo.close()


async def test_known_pool_address_is_not_counted_as_concentration() -> None:
    src = FakeSources(goplus=fx("goplus_base_token.json"), honeypot=fx("honeypot_base_token.json"))
    top = fx("goplus_base_token.json")["result"][ELSA.address]["holders"][0]["address"]
    without = await SecurityAssessor(src, clock=lambda: NOW).assess(ELSA)
    with_pool = await SecurityAssessor(src, clock=lambda: NOW).assess(ELSA, pool_addresses=[top.upper()])
    assert without.top_pct(10) > 85
    assert with_pool.holders[0].kind is HolderKind.AMM and with_pool.top_pct(10) < 20


async def test_evm_without_sell_simulation_blocks_entry_but_not_holding() -> None:
    src = FakeSources(goplus=fx("goplus_base_token.json"), honeypot={"error": "api_rejection: pair not found"})
    r = await SecurityAssessor(src, clock=lambda: NOW).assess(ELSA)
    assert r.passed and not r.entry_allowed and r.unverified == ["sell_simulation_unavailable"]
