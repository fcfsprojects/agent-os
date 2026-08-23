"""Removing subagents.archive_after_minutes must not break installs that set it.

The key was a silent no-op: no auto-archive timer exists anywhere, and
SubagentRegistry.archive() has zero callers. SubagentsGatewayConfig forbids
extra keys, so an existing agentos.toml carrying the key would fail validation
at boot without the deprecated-field migration.
"""

from __future__ import annotations

from pathlib import Path

from agentos.gateway.config import GatewayConfig


def test_an_existing_config_with_archive_after_minutes_still_loads(tmp_path: Path):
    config_path = tmp_path / "agentos.toml"
    config_path.write_text(
        "[subagents]\n"
        "enforce_disabled_agents = true\n"
        "archive_after_minutes = 120\n",
        encoding="utf-8",
    )

    config = GatewayConfig.load(config_path)

    assert config.subagents.enforce_disabled_agents is True
    assert not hasattr(config.subagents, "archive_after_minutes")


def test_the_dropped_key_is_reported_not_silently_eaten():
    from agentos.gateway.config_migration import DEPRECATED_SUBAGENTS_FIELDS

    assert "subagents.archive_after_minutes" in DEPRECATED_SUBAGENTS_FIELDS


def test_a_config_without_the_key_is_unaffected(tmp_path: Path):
    config_path = tmp_path / "agentos.toml"
    config_path.write_text(
        "[subagents]\nsubagent_reserved_slots = 4\n",
        encoding="utf-8",
    )

    assert GatewayConfig.load(config_path).subagents.subagent_reserved_slots == 4
