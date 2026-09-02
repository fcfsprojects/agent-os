"""Regression tests for ``agentos config set`` (issue #834).

Three things to lock down, per Andre's acceptance criteria:

1. ``_set_key`` may create new keys **only** under ``skills.config.*``.
   The free-form ``SkillsConfig.config`` map is the documented escape
   hatch for skill-declared settings, and the docs example
   ``agentos config set skills.config.wiki.path /srv/wiki`` is the
   proof case. Every other branch keeps the typo guard so a misspelled
   ``skillz.max_skills_prompt_chars`` does not silently persist.
2. The no-``--config`` env-hint branch must validate the key against
   ``GatewayConfig`` before printing the ``export`` line. A previous
   version of the code string-mangled any key into an env var name and
   printed it as if it were actionable, even for keys (like
   ``skills.config.*``) that do not bind to a pydantic-settings field
   — silent success with nothing persisted.
3. Round-trip coverage: an existing model key still works, a new
   ``skills.config.*`` key works, and a typo outside ``skills.config``
   still fails. Without these the next refactor will silently regress
   one of the three paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.config_cmd import _probe_key_value, _set_key, _UnknownConfigKeyError
from agentos.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# _set_key — direct unit tests (Andre: "Add tests for _set_key")
# ---------------------------------------------------------------------------


def test_set_key_creates_new_skills_config_subkey() -> None:
    """Documented command ``agentos config set skills.config.wiki.path /srv/wiki``
    must persist. Pre-fix this returned False because ``to_toml_dict()``
    omitted the empty ``skills.config`` map."""
    data: dict[str, Any] = {"skills": {"max_skills_prompt_chars": 4000}}
    assert _set_key(data, "skills.config.wiki.path", "/srv/wiki") is True
    assert data == {
        "skills": {
            "max_skills_prompt_chars": 4000,
            "config": {"wiki": {"path": "/srv/wiki"}},
        }
    }


def test_set_key_creates_brand_new_top_level_skill_config() -> None:
    """Even when ``skills`` is absent from the loaded dict, the free-form
    map can be created from scratch (so the cold-start ``agentos config
    set skills.config.<skill>.<key>`` works without a pre-existing
    section)."""
    data: dict[str, Any] = {}
    assert _set_key(data, "skills.config.notes.dir", "/var/notes") is True
    assert data == {"skills": {"config": {"notes": {"dir": "/var/notes"}}}}


def test_set_key_overwrites_existing_skills_config_leaf() -> None:
    """A repeat ``set`` against an existing leaf must overwrite in place
    rather than fail the typo guard."""
    data: dict[str, Any] = {"skills": {"config": {"wiki": {"path": "/old"}}}}
    assert _set_key(data, "skills.config.wiki.path", "/new") is True
    assert data["skills"]["config"]["wiki"]["path"] == "/new"


def test_set_key_still_rejects_typo_outside_skills_config() -> None:
    """Andre: "preserve the typo guard everywhere else". A misspelled
    ``skillz.max_skills_prompt_chars`` must still fail rather than
    silently land in a near-collision path."""
    data: dict[str, Any] = {"skills": {"max_skills_prompt_chars": 4000}}
    assert _set_key(data, "skillz.max_skills_prompt_chars", 8000) is False
    assert data == {"skills": {"max_skills_prompt_chars": 4000}}


def test_set_key_still_overwrites_existing_model_field() -> None:
    """The happy path for an existing model field must not regress."""
    data: dict[str, Any] = {"skills": {"max_skills_prompt_chars": 4000}}
    assert _set_key(data, "skills.max_skills_prompt_chars", 8000) is True
    assert data == {"skills": {"max_skills_prompt_chars": 8000}}


def test_set_key_rejects_unknown_top_level_key() -> None:
    """A typo on a top-level key (e.g. ``agentos.config.set
    sklls.config.x /y``) must still fail rather than create a new
    top-level map."""
    data: dict[str, Any] = {"skills": {"max_skills_prompt_chars": 4000}}
    assert _set_key(data, "sklls.config.x", "/y") is False


def test_set_key_rejects_brand_new_outside_skills_config() -> None:
    """Outside ``skills.config.*``, new intermediate dicts are not
    created. The pre-existing typo guard is preserved."""
    data: dict[str, Any] = {"gateway": {"host": "127.0.0.1"}}
    assert _set_key(data, "gateway.runtime.new_field", True) is False


# ---------------------------------------------------------------------------
# _probe_key_value — used by the no-``--config`` env-hint branch
# ---------------------------------------------------------------------------


def test_probe_known_model_field_round_trips() -> None:
    """Probing an existing model field produces a value ``GatewayConfig``
    can validate, so the env-hint branch accepts it."""
    from agentos.gateway.config import GatewayConfig

    payload = _probe_key_value("skills.max_skills_prompt_chars")
    cfg = GatewayConfig.model_validate(payload)
    assert cfg.skills.max_skills_prompt_chars >= 0


def test_probe_skills_config_subkey_accepted() -> None:
    """Probing ``skills.config.wiki.path`` must not raise — the env-hint
    branch must accept documented free-form-map keys."""
    from agentos.gateway.config import GatewayConfig

    payload = _probe_key_value("skills.config.wiki.path")
    cfg = GatewayConfig.model_validate(payload)
    assert cfg.skills.config["wiki"]["path"] == ""


def test_probe_unknown_top_level_key_raises() -> None:
    """A typo on the top-level key must raise ``_UnknownConfigKeyError``
    so the env-hint branch fails loudly rather than printing an
    invented env var name."""
    with pytest.raises(_UnknownConfigKeyError):
        _probe_key_value("sklls.config.x")


def test_probe_unknown_subkey_raises() -> None:
    """A typo on a sub-key (not under ``skills.config``) must also
    raise so the env-hint branch fails loudly."""
    with pytest.raises(_UnknownConfigKeyError):
        _probe_key_value("skillz.max_skills_prompt_chars")


# ---------------------------------------------------------------------------
# CLI integration — the full ``agentos config set`` command
# ---------------------------------------------------------------------------


def test_config_set_skills_config_subkey_persists_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the documented command must persist and survive a
    round-trip load."""
    config_path = tmp_path / "agentos.toml"
    config_path.write_text("")
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app,
        ["config", "set", "skills.config.wiki.path", "/srv/wiki", "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.stdout

    from agentos.gateway.config import GatewayConfig

    cfg = GatewayConfig.model_validate(__import__("tomllib").loads(config_path.read_text()))
    assert cfg.skills.config["wiki"]["path"] == "/srv/wiki"


def test_config_set_rejects_typo_outside_skills_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--config`` persist path must still fail loudly on a typo
    outside ``skills.config`` so the user gets a signal rather than a
    silent near-collision write."""
    config_path = tmp_path / "agentos.toml"
    config_path.write_text("")
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app,
        ["config", "set", "skillz.max_skills_prompt_chars", "8000", "--config", str(config_path)],
    )
    assert result.exit_code == 1, result.stdout
    assert "key not found" in result.stdout.lower()


def test_config_set_without_config_validates_known_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-``--config`` env-hint branch must accept an existing
    model field and print the export line."""
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app, ["config", "set", "skills.max_skills_prompt_chars", "8000"]
    )
    assert result.exit_code == 0, result.stdout
    assert "AGENTOS_GATEWAY_SKILLS__MAX_SKILLS_PROMPT_CHARS" in result.stdout


def test_config_set_without_config_accepts_skills_config_subkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-``--config`` env-hint branch must accept documented
    ``skills.config.*`` keys (the probe rounds them through
    ``GatewayConfig.model_validate``)."""
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app, ["config", "set", "skills.config.wiki.path", "/srv/wiki"]
    )
    assert result.exit_code == 0, result.stdout
    assert "AGENTOS_GATEWAY_SKILLS__CONFIG__WIKI__PATH" in result.stdout


def test_config_set_without_config_rejects_typo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-``--config`` env-hint branch must fail loudly on a typo
    rather than print a misleading ``export`` line."""
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    result = runner.invoke(
        app, ["config", "set", "sklls.config.x", "/y"]
    )
    assert result.exit_code == 1, result.stdout
    assert "key not found" in result.stdout.lower()
    # The export line must NOT be printed when the key is invalid —
    # that was the original bug (silent success with no actual binding).
    assert "export AGENTOS_GATEWAY" not in result.stdout
