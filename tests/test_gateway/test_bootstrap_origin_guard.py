"""The Control UI bootstrap endpoint must stay behind the Origin guard.

/control/api/bootstrap is served under the UI prefix but is an RPC payload:
it leaks the host config path and the auth mode to any cross-origin page that
can fetch it. The UI-shell exemption exists for served pages (guarded by CSP),
not for JSON RPC sinks — so the bootstrap route is carved out of it.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.config import GatewayConfig
from agentos.gateway.middleware import LoopbackOriginMiddleware


def _bootstrap(request):
    return JSONResponse({"config_path": "/home/operator/.agentos/config.toml"})


def _app() -> TestClient:
    inner = Starlette(
        routes=[
            Route("/control/api/bootstrap", _bootstrap),
            Route("/control", _bootstrap),
        ]
    )
    config = GatewayConfig()
    mw = LoopbackOriginMiddleware(inner, config=config, bind_is_loopback=True)
    return TestClient(mw)


def test_bootstrap_rejects_foreign_origin():
    resp = _app().get(
        "/control/api/bootstrap",
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert b"config.toml" not in resp.content


def test_bootstrap_allows_requests_without_origin():
    # CLI/curl/browser navigations send no Origin — pass through.
    resp = _app().get("/control/api/bootstrap")
    assert resp.status_code == 200


def test_ui_shell_still_exempt():
    # The served shell itself remains exempt (CSP-guarded page, not RPC).
    resp = _app().get("/control", headers={"origin": "https://evil.example"})
    assert resp.status_code == 200
