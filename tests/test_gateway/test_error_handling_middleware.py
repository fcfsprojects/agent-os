"""Unhandled-exception responses must not leak internals outside debug mode.

Raw str(exc) can contain host paths, SQL fragments, provider URLs, and other
internals. Any client that can reach the gateway could harvest them from a
500 response. The full exception belongs in the server log; production
clients get a generic message.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.middleware import ErrorHandlingMiddleware


def _boom(request):
    raise RuntimeError("secret path /home/operator/.agentos/config.toml")


def _app(debug: bool) -> TestClient:
    app = Starlette(routes=[Route("/boom", _boom)])
    app.add_middleware(ErrorHandlingMiddleware, debug=debug)
    return TestClient(app, raise_server_exceptions=False)


def test_production_mode_returns_generic_error():
    resp = _app(debug=False).get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["error"] == "internal server error"
    assert "/home/operator" not in body["error"]
    assert "secret" not in body["error"]


def test_debug_mode_returns_raw_exception():
    resp = _app(debug=True).get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "/home/operator/.agentos/config.toml" in body["error"]
