from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from atomtrader.marketdata.http import ApiError, ErrorKind, JsonHttpClient, RateLimiter


async def _no_sleep(_: float) -> None:
    return None


def client_for(handler, retries: int = 2) -> JsonHttpClient:
    return JsonHttpClient(
        "https://api.example.test", per_minute=10_000, max_retries=retries,
        transport=httpx.MockTransport(handler), sleep=_no_sleep,
    )


async def test_parses_numbers_as_decimal() -> None:
    c = client_for(lambda r: httpx.Response(200, text='{"price": 0.0000022101, "n": 3}'))
    data = await c.get_json("/x")
    assert data["price"] == Decimal("0.0000022101") and isinstance(data["price"], Decimal)


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (400, '{"error":"The token X is not tradable","errorCode":"TOKEN_NOT_TRADABLE"}', ErrorKind.API_REJECTION),
        (404, "not found", ErrorKind.API_REJECTION),
        (422, '{"message":"unknown parameter: symbols"}', ErrorKind.API_REJECTION),
        (429, "slow down", ErrorKind.RATE_LIMITED),
        (503, "maintenance", ErrorKind.SERVER_ERROR),
        (301, "", ErrorKind.API_REJECTION),
    ],
)
async def test_classifies_http_failures(status: int, body: str, kind: ErrorKind) -> None:
    c = client_for(lambda r: httpx.Response(status, text=body, headers={"location": "https://elsewhere.test/"}), retries=0)
    with pytest.raises(ApiError) as e:
        await c.get_json("/x")
    assert e.value.kind is kind and e.value.status == status


async def test_token_rejection_message_is_extracted() -> None:
    c = client_for(lambda r: httpx.Response(400, text='{"error":"unable to generate report"}'))
    with pytest.raises(ApiError) as e:
        await c.get_json("/x")
    assert e.value.message == "unable to generate report"


async def test_non_json_2xx_is_invalid_response() -> None:
    c = client_for(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ApiError) as e:
        await c.get_json("/x")
    assert e.value.kind is ErrorKind.INVALID_RESPONSE


async def test_network_and_proxy_errors() -> None:
    def boom(r):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(ApiError) as e:
        await client_for(boom, retries=0).get_json("/x")
    assert e.value.kind is ErrorKind.NETWORK_ERROR

    def proxy(r):
        raise httpx.ProxyError("403 Forbidden")

    with pytest.raises(ApiError) as e:
        await client_for(proxy, retries=3).get_json("/x")
    assert e.value.kind is ErrorKind.PROXY_BLOCKED


async def test_retries_only_transient_errors_and_bounded() -> None:
    calls = {"n": 0}

    def flaky(r):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, text="{}")

    assert await client_for(flaky, retries=2).get_json("/x") == {}
    assert calls["n"] == 3

    calls["n"] = 0

    def always_503(r):
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(ApiError):
        await client_for(always_503, retries=2).get_json("/x")
    assert calls["n"] == 3  # 1 + 2 retries, then stop

    calls["n"] = 0

    def rejected(r):
        calls["n"] += 1
        return httpx.Response(400, text='{"error":"bad mint"}')

    with pytest.raises(ApiError):
        await client_for(rejected, retries=5).get_json("/x")
    assert calls["n"] == 1  # API rejections are never retried


async def test_rate_limiter_spaces_requests() -> None:
    t = {"now": 0.0}
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        t["now"] += s

    lim = RateLimiter(per_minute=30, clock=lambda: t["now"], sleep=fake_sleep)
    for _ in range(3):
        await lim.acquire()
    assert slept == [2.0, 2.0]


async def test_rate_limited_is_not_retried_and_pauses_client() -> None:
    calls = {"n": 0}

    def limited(r):
        calls["n"] += 1
        return httpx.Response(429, text="slow", headers={"retry-after": "30"})

    c = client_for(limited, retries=5)
    with pytest.raises(ApiError) as e:
        await c.get_json("/x")
    assert e.value.kind is ErrorKind.RATE_LIMITED and calls["n"] == 1
    # Subsequent calls through this client must wait ~30 s before being sent.
    assert c.limiter._next - c.limiter._clock() >= 29


def test_limiter_halves_on_429_and_recovers_slowly() -> None:
    lim = RateLimiter(per_minute=8, min_per_minute=1, recover_step=0.5)
    lim.on_rate_limited()
    assert lim.per_minute == 4 and lim.interval == 15
    lim.on_rate_limited(); lim.on_rate_limited(); lim.on_rate_limited()
    assert lim.per_minute == 1  # floor
    for _ in range(100):
        lim.on_success()
    assert lim.per_minute == 8  # ceiling restored, never above configured max
