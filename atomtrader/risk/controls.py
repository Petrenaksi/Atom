"""Operator controls: emergency stop, pauses, blacklist, autonomous toggle.

These are checked before every new entry. None of them require or touch wallet keys, so
trading can always be disabled without compromising the wallet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from atomtrader.core.models import Chain, TokenRef


@dataclass(frozen=True, slots=True)
class ControlEvent:
    at: datetime
    action: str
    detail: str
    actor: str


@dataclass
class TradingControls:
    emergency_stop: bool = False
    global_pause: bool = False
    # When False, approved trades need manual confirmation before execution.
    autonomous: bool = False
    paused_chains: set[Chain] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)  # TokenRef.key values
    events: list[ControlEvent] = field(default_factory=list)

    def _log(self, now: datetime, action: str, detail: str, actor: str) -> None:
        self.events.append(ControlEvent(now, action, detail, actor))

    def trigger_emergency_stop(self, now: datetime, reason: str, actor: str = "operator") -> None:
        """Blocks all new entries. Exits remain allowed so positions can be closed safely."""
        self.emergency_stop = True
        self.autonomous = False
        self._log(now, "emergency_stop", reason, actor)

    def clear_emergency_stop(self, now: datetime, actor: str = "operator") -> None:
        self.emergency_stop = False
        self._log(now, "clear_emergency_stop", "", actor)

    def pause(self, now: datetime, actor: str = "operator") -> None:
        self.global_pause = True
        self._log(now, "pause", "", actor)

    def resume(self, now: datetime, actor: str = "operator") -> None:
        self.global_pause = False
        self._log(now, "resume", "", actor)

    def set_autonomous(self, enabled: bool, now: datetime, actor: str = "operator") -> None:
        if enabled and self.emergency_stop:
            raise PermissionError("clear the emergency stop before enabling autonomous mode")
        self.autonomous = enabled
        self._log(now, "autonomous", str(enabled), actor)

    def pause_chain(self, chain: Chain, now: datetime, actor: str = "operator") -> None:
        self.paused_chains.add(chain)
        self._log(now, "pause_chain", chain.value, actor)

    def resume_chain(self, chain: Chain, now: datetime, actor: str = "operator") -> None:
        self.paused_chains.discard(chain)
        self._log(now, "resume_chain", chain.value, actor)

    def blacklist_token(self, token: TokenRef, now: datetime, reason: str, actor: str = "operator") -> None:
        self.blacklist.add(token.key)
        self._log(now, "blacklist", f"{token.key}: {reason}", actor)

    def entry_block_reason(self, token: TokenRef) -> str | None:
        if self.emergency_stop:
            return "emergency_stop"
        if self.global_pause:
            return "global_pause"
        if token.chain in self.paused_chains:
            return f"chain_paused:{token.chain.value}"
        if token.key in self.blacklist:
            return "token_blacklisted"
        return None
