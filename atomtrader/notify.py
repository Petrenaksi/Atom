"""Notifications.

Every notification is written to the events table (``notify:<kind>``) and the structured
log, which the dashboard and the daily report read. An optional webhook channel (e.g. a
Telegram bot or Discord webhook) is supported but disabled until the owner configures its
URL; webhook payloads go through the same secret-redaction as logs.

Kinds: opportunity, narrative_breakout, rotation, entry, partial_exit, exit, insider_alert,
liquidity_alert, daily_loss_limit, execution_failure, system_error, awaiting_confirmation,
daily_report.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

import httpx

from atomtrader.logging import get_logger, redact_secrets
from atomtrader.persistence.repository import Repository

log = get_logger("notify")
LEVELS = ("info", "warning", "critical")


class Channel(Protocol):
    async def send(self, kind: str, title: str, body: str, level: str) -> None: ...


class WebhookChannel:
    """Generic JSON webhook (Discord-compatible ``content`` field). Disabled unless a URL is
    configured by the owner. Only levels >= ``min_level`` are sent."""

    def __init__(self, url: str, min_level: str = "warning", timeout: float = 10.0) -> None:
        self.url = url
        self.min_level = min_level
        self._client = httpx.AsyncClient(timeout=timeout)

    async def send(self, kind: str, title: str, body: str, level: str) -> None:
        if LEVELS.index(level) < LEVELS.index(self.min_level):
            return
        payload = redact_secrets(None, "info", {"content": f"[{level.upper()}] {title}\n{body}"[:1900]})
        try:
            await self._client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("webhook_failed", error=str(exc)[:200])


class Notifier:
    def __init__(self, repo: Repository, channels: Sequence[Channel] = (),
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.repo = repo
        self.channels = list(channels)
        self.clock = clock

    async def notify(self, kind: str, title: str, body: str = "", level: str = "info") -> None:
        if level not in LEVELS:
            raise ValueError(level)
        await self.repo.log_event(self.clock(), f"notify:{kind}", f"[{level}] {title} | {body}"[:2048], "engine")
        getattr(log, "warning" if level != "info" else "info")("notify", kind=kind, title=title, level=level)
        for ch in self.channels:
            await ch.send(kind, title, body, level)
