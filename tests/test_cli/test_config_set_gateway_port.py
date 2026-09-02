"""Regression test for issue #840 — documented ``agentos config set
gateway.port`` must fail loudly.

The previous docs example (``README.product.md``, ``docs/cli.md``)
told users to run ``agentos config set gateway.port 18791``, but the
real key is the top-level ``port`` on ``GatewayConfig``. ``gateway``
is not a TOML table (``extra = forbid``). The CLI used to:

- exit 1 with ``Key not found: gateway.port`` under ``--config``
- exit 0 silently and print an invented
  ``export AGENTOS_GATEWAY_GATEWAY__PORT=18791`` env hint without
  ``--config`` (the env-hint branch mechanically string-mangled any
  key; the code half of that fix lives in PR #834).

This file pins the ``--config`` half of the behavior so a future
revert of the docs immediately fails in CI rather than silently
shipping a broken example. The env-hint half is covered separately
by ``test_config_cmd.py`` once #834 is merged; we do not duplicate
that here so this PR stays self-contained on ``origin/main``.

The docs side of the fix lives in ``README.product.md`` and
``docs/cli.md`` (both updated in this commit).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


def test_config_set_gateway_port_with_config_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config`` path: must exit 1 with a ``Key not found`` line so
    the user has a signal to investigate, rather than a near-collision
    write. ``gateway.port`` is not a valid path on ``GatewayConfig``
    because there is no ``[gateway]`` TOML table (``extra = forbid``)
    — the real key is the top-level ``port``."""
    config_path = tmp_path / "agentos.toml"
    config_path.write_text("")
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app,
        ["config", "set", "gateway.port", "18791", "--config", str(config_path)],
    )
    assert result.exit_code == 1, result.stdout
    assert "key not found" in result.stdout.lower()


def test_config_set_port_with_config_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path for the corrected docs example. ``port`` is the real
    key, and ``--config`` must round-trip through
    ``GatewayConfig.model_validate``."""
    import tomllib

    config_path = tmp_path / "agentos.toml"
    config_path.write_text("")
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app,
        ["config", "set", "port", "18791", "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.stdout

    cfg_dict = tomllib.loads(config_path.read_text())
    assert cfg_dict["port"] == 18791
