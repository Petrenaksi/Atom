"""Shared async JSON HTTP client for read-only data providers.

* Numbers are parsed as ``Decimal`` (never float) so prices of micro-cap tokens such as
  0.0000022 keep full precision.
* A per-client async rate limiter keeps us under each provider's published limit.
* Failures are classified so a token-specific API rejection is never mistaken for an
  outage, and vice versa:

    PROXY_BLOCKED     egress policy refused the host (environment config, not the API)
    NETWORK_ERROR     DNS/connect/TLS/timeout — the API may be fine
    RATE_LIMITED      HTTP 429
    SERVER_ERROR      HTTP 5xx — provider-side problem
    API_REJECTION     other 4xx — the request/token was refused (bad mint, unsupported
                      token, missing report, invalid parameter). The endpoint is up.
    INVALID_RESPONSE  2xx but not the JSON we expect

NETWORK_ERROR and SERVER_ERROR are retried with exponential backoff. RATE_LIMITED is not
retried immediately: the client's limiter is paused for Retry-After (default 60 s) so every
later call through it waits, and the error is raised for the caller to skip this cycle.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx

USER_AGENT = "atomtrader/0.1 (paper-trading research)"


class ErrorKind(StrEnum):
    PROXY_BLOCKED = "proxy_blocked"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    API_REJECTION = "api_rejection"
    INVALID_RESPONSE = "invalid_response"


TRANSIENT = frozenset({ErrorKind.NETWORK_ERROR, ErrorKind.SERVER_ERROR})
DEFAULT_RATE_LIMIT_PAUSE_S = 60.0


class ApiError(RuntimeError):
    def __init__(self, kind: ErrorKind, host: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{kind.value} {host} (status={status}): {message}")
        self.kind = kind
        self.host = host
        self.status = status
        self.message = message


class RateLimiter:
    """Adaptive async spacing limiter shared by all calls through one client.

    Starts at ``per_minute`` (evenly spaced). Keyless limits are often enforced per egress
    IP and may be shared with other users, so the configured rate can still be too high:
    each 429 halves the rate (floor ``min_per_minute``); each success raises it by
    ``recover_step`` per minute, back up to the configured ceiling (AIMD)."""

    def __init__(
        self,
        per_minute: float,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
        min_per_minute: float = 1.0,
        recover_step: float = 0.25,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.max_per_minute = per_minute
        self.min_per_minute = min(min_per_minute, per_minute)
        self.recover_step = recover_step
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next = max(now, self._next) + self.interval

    @property
    def interval(self) -> float:
        return 60.0 / self.per_minute

    def pause(self, seconds: float) -> None:
        """Push the next permitted request at least ``seconds`` into the future."""
        self._next = max(self._next, self._clock() + seconds)

    def on_rate_limited(self) -> None:
        self.per_minute = max(self.min_per_minute, self.per_minute / 2)

    def on_success(self) -> None:
        self.per_minute = min(self.max_per_minute, self.per_minute + self.recover_step)


def parse_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:200]
    if isinstance(data, dict):
        for key in ("error", "message", "errorCode", "detail"):
            if key in data:
                return str(data[key])[:200]
    return body[:200]


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        per_minute: float,
        timeout: float = 15.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = httpx.URL(self.base_url).host
        self.limiter = RateLimiter(per_minute, sleep=sleep)
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self._sleep = sleep
        # trust_env=True: honours HTTPS_PROXY and SSL_CERT_FILE. TLS verification stays on.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
            trust_env=transport is None,
            follow_redirects=False,  # a redirect to a non-allowlisted host must be visible
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> JsonHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post_json(self, path: str, payload: Any) -> Any:
        return await self._request("POST", path, json_body=payload)

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, json_body: Any = None
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire()
            try:
                return await self._once(method, url, params, json_body)
            except ApiError as err:
                if err.kind not in TRANSIENT or attempt > self.max_retries:
                    raise
                await self._sleep(self.backoff * (2 ** (attempt - 1)))

    async def _once(self, method: str, url: str, params: Any, json_body: Any) -> Any:
        try:
            resp = await self._client.request(method, url, params=params, json=json_body)
        except httpx.ProxyError as exc:
            raise ApiError(ErrorKind.PROXY_BLOCKED, self.host, str(exc)) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ApiError(ErrorKind.NETWORK_ERROR, self.host, repr(exc)) from exc

        status = resp.status_code
        if 300 <= status < 400:
            loc = resp.headers.get("location", "")
            raise ApiError(ErrorKind.API_REJECTION, self.host, f"redirect to {loc}", status)
        if status == 429:
            try:
                pause = float(resp.headers.get("retry-after", DEFAULT_RATE_LIMIT_PAUSE_S))
            except ValueError:
                pause = DEFAULT_RATE_LIMIT_PAUSE_S
            self.limiter.pause(min(max(pause, 1.0), 600.0))
            self.limiter.on_rate_limited()
            raise ApiError(ErrorKind.RATE_LIMITED, self.host, _error_message(resp.text), status)
        if status >= 500:
            raise ApiError(ErrorKind.SERVER_ERROR, self.host, _error_message(resp.text), status)
        if status >= 400:
            raise ApiError(ErrorKind.API_REJECTION, self.host, _error_message(resp.text), status)
        self.limiter.on_success()
        try:
            return parse_json(resp.text)
        except ValueError as exc:
            raise ApiError(ErrorKind.INVALID_RESPONSE, self.host, "non-JSON body", status) from exc
