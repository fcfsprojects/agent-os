"""Rate limiter must not trust client-supplied X-Forwarded-For or grow unbounded.

Two failure modes, one fix:

1. Bypass: on a direct (non-proxied) connection, X-Forwarded-For is fully
   client-controlled. Trusting it lets a caller rotate the header and get a
   fresh bucket per request. It is only honored when the immediate peer is
   the configured trusted proxy.

2. DoS: _windows was an unbounded dict keyed by client IP — rotating source
   IPs grew it forever on a long-lived gateway. Buckets are now pruned and
   the tracked-client count is capped.
"""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.config import AuthConfig, GatewayConfig, RateLimitConfig
from agentos.gateway.middleware import RateLimitMiddleware


def _ok(request):
    return PlainTextResponse("ok")


def _app(config: GatewayConfig) -> tuple[TestClient, RateLimitMiddleware]:
    inner = Starlette(routes=[Route("/", _ok)])
    mw = RateLimitMiddleware(inner, config=config)
    return TestClient(mw), mw


def test_x_forwarded_for_is_ignored_without_trusted_proxy():
    config = GatewayConfig(
        rate_limit=RateLimitConfig(enabled=True, max_requests=2, window_seconds=60),
    )
    client, mw = _app(config)

    for i in range(3):
        resp = client.get("/", headers={"x-forwarded-for": f"10.0.0.{i}"})
    # All three requests share one bucket (the real peer / testclient), so the
    # third one is limited — rotating XFF must not bypass.
    assert resp.status_code == 429
    assert len(mw._windows) <= 2  # "unknown" or the single peer bucket


def test_x_forwarded_for_honored_from_trusted_proxy():
    config = GatewayConfig(
        auth=AuthConfig(mode="trusted-proxy", trusted_proxy="testclient"),
        rate_limit=RateLimitConfig(enabled=True, max_requests=1, window_seconds=60),
    )
    client, mw = _app(config)

    # Peer's host under TestClient is "testclient"; when it matches the
    # configured trusted proxy, distinct XFF values get distinct buckets.
    client.get("/", headers={"x-forwarded-for": "10.0.0.1"})
    resp = client.get("/", headers={"x-forwarded-for": "10.0.0.2"})
    assert resp.status_code == 200
    assert "10.0.0.1" in mw._windows and "10.0.0.2" in mw._windows


def test_windows_dict_is_bounded():
    config = GatewayConfig(
        rate_limit=RateLimitConfig(enabled=True, max_requests=100, window_seconds=60),
    )
    _, mw = _app(config)

    now = time.time()
    # Simulate a flood from many distinct IPs.
    for i in range(RateLimitMiddleware._MAX_TRACKED_CLIENTS + 500):
        mw._windows[f"192.0.{i // 256}.{i % 256}"] = [now]

    mw._prune_windows(now)
    assert len(mw._windows) <= RateLimitMiddleware._MAX_TRACKED_CLIENTS


def test_expired_buckets_are_pruned():
    config = GatewayConfig(
        rate_limit=RateLimitConfig(enabled=True, max_requests=1, window_seconds=1),
    )
    _, mw = _app(config)

    stale = time.time() - 3600
    mw._windows["192.0.2.1"] = [stale]
    mw._prune_windows(time.time())
    assert "192.0.2.1" not in mw._windows
