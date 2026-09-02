"""Config command — get/set configuration values."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from agentos.cli.ui import ACCENT_HEADER, ACCENT_MARKUP, console

app = typer.Typer(help="Manage AgentOS configuration.")


@app.command("get")
def config_get(
    key: str = typer.Argument("", help="Config key to get (empty = show all)"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Get a configuration value."""
    from agentos.gateway.config import GatewayConfig

    cfg = GatewayConfig.load(config_path or os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
    data = cfg.to_public_dict()

    if key:
        # Support dot-notation: auth.mode
        val = _get_key(data, key)
        if val is _MISSING:
            console.print(f"[red]Key not found: {key}[/red]")
            raise typer.Exit(1)
        console.print(f"[{ACCENT_MARKUP}]{escape(key)}[/] = [green]{escape(repr(val))}[/green]")
    else:
        table = Table(title="Gateway Config", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Key")
        table.add_column("Value")
        _add_flat(table, data)
        console.print(table)


_MISSING = object()


def _get_key(data: dict[str, Any], key: str) -> Any:
    val: Any = data
    for part in key.split("."):
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return _MISSING
    return val


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dot-notation)"),
    value: str = typer.Argument(..., help="Value to set"),
    config_path: Path | None = typer.Option(None, "--config", help="Persist to config path."),
) -> None:
    """Set a configuration value (env-var backed, prints export command)."""
    if config_path is not None:
        from agentos.gateway.config import GatewayConfig
        from agentos.onboarding.config_store import load_config, persist_config

        cfg = load_config(config_path)
        data = cfg.to_toml_dict()
        if not _set_key(data, key, _parse_config_value(value)):
            console.print(f"[red]Key not found: {escape(key)}[/red]")
            raise typer.Exit(1)
        try:
            updated = GatewayConfig.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - show config validation errors as CLI input errors.
            console.print(f"[red]Invalid value for {escape(key)}:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc
        persist = persist_config(updated, path=config_path, restart_required=True)
        console.print(f"[{ACCENT_MARKUP}]Config:[/] {persist.path}")
        if persist.backup_path:
            console.print(f"[dim]Backup:[/dim] {persist.backup_path}")
        console.print("[yellow]Restart the gateway to apply this setting.[/yellow]")
        return

    # Issue #834: the no-``--config`` branch previously string-mangled any
    # key into an env var name and printed it as if it were actionable,
    # even for keys (like ``skills.config.*``) that do not bind to a
    # ``GatewayConfig`` pydantic-settings field. Validate the key against
    # the live model first so a typo or free-form-map key fails loudly
    # rather than producing a misleading ``export`` line that would not
    # actually take effect.
    from agentos.gateway.config import GatewayConfig

    try:
        GatewayConfig.model_validate(_probe_key_value(key))
    except _UnknownConfigKeyError:
        console.print(f"[red]Key not found: {escape(key)}[/red]")
        raise typer.Exit(1) from None
    except Exception as exc:  # noqa: BLE001 - same rationale as the --config branch.
        console.print(f"[red]Invalid value for {escape(key)}:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc

    env_key = "AGENTOS_GATEWAY_" + key.upper().replace(".", "__")
    console.print("[dim]To persist this setting, export:[/dim]")
    console.print(f"  [bold]export {env_key}={value}[/bold]")


def _parse_config_value(value: str) -> Any:
    try:
        return tomllib.loads(f"value = {value}\n")["value"]
    except tomllib.TOMLDecodeError:
        return value


def _set_key(data: dict[str, Any], key: str, value: Any) -> bool:
    # Issue #834: ``_set_key`` previously refused to create any new keys,
    # which made the documented ``agentos config set
    # skills.config.wiki.path /srv/wiki`` fail with ``Key not found``
    # because ``to_toml_dict()`` omits the empty ``skills.config`` map.
    # The free-form ``SkillsConfig.config`` (``dict[str, Any]``) is the
    # one place we relax creation: any key under ``skills.config.*`` may
    # materialize missing intermediate dicts, and the final key may be
    # brand new. Every other top-level branch keeps the typo guard — a
    # misspelled ``skillz.max_skills_prompt_chars`` must still fail so we
    # do not silently persist nonsense into a near-collision path.
    parts = key.split(".")
    if len(parts) >= 2 and parts[0] == "skills" and parts[1] == "config":
        cursor: Any = data
        for part in parts[:-1]:
            if not isinstance(cursor, dict):
                return False
            if part not in cursor or cursor[part] in (None, {}):
                cursor[part] = {}
            cursor = cursor[part]
        if not isinstance(cursor, dict):
            return False
        cursor[parts[-1]] = value
        return True

    cursor = data
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False
    cursor[parts[-1]] = value
    return True


class _UnknownConfigKeyError(KeyError):
    """Raised when a key does not bind to a ``GatewayConfig`` field or to
    the ``skills.config`` free-form map. Used by the no-``--config`` env
    hint branch in ``config_set`` to fail loudly rather than print an
    invented ``export`` line (issue #834)."""


def _probe_key_value(key: str) -> Any:
    """Build a dict payload that ``GatewayConfig.model_validate`` will
    accept for the given key path, so the no-``--config`` env-hint branch
    can probe without printing an invented ``export`` line.

    For known model fields the probe reconstructs a nested ``{top:
    {middle: ..., leaf: default}}`` dict by walking the actual model so
    the validator never sees an ``extra=forbid`` violation on a sibling
    field. For ``skills.config.*`` the free-form map accepts any value,
    so a single empty-string sentinel is enough."""
    parts = key.split(".")
    from agentos.gateway.config import GatewayConfig  # local import: keep --config path light

    if len(parts) >= 2 and parts[0] == "skills" and parts[1] == "config":
        # Build a nested dict from the path so deep keys like
        # ``skills.config.wiki.path`` round-trip through
        # ``GatewayConfig.model_validate`` with each intermediate dict
        # materialized.
        sub: dict[str, Any] = {}
        cursor: Any = sub
        for i, part in enumerate(parts[2:]):
            if i == len(parts) - 3:
                cursor[part] = ""
            else:
                cursor[part] = {}
                cursor = cursor[part]
        return {"skills": {"config": sub}}

    cfg = GatewayConfig()
    # Walk the key path against the model. Build a nested dict of
    # defaults so ``model_validate`` round-trips cleanly under
    # ``extra=forbid``. If any segment is not a model attribute, the key
    # does not bind to a ``GatewayConfig`` field.
    payload: dict[str, Any] = {}
    cursor_cfg: Any = cfg
    cursor_payload: Any = payload
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        if not hasattr(cursor_cfg, part):
            raise _UnknownConfigKeyError(key)
        value = getattr(cursor_cfg, part)
        if is_last:
            cursor_payload[part] = value
        else:
            # Intermediate segment must be a pydantic model or a dict;
            # flatten its own subfields so the validator does not see an
            # ``extra_forbidden`` violation when the caller only asked
            # for a deep subkey.
            if hasattr(value, "model_dump"):
                cursor_payload[part] = value.model_dump()
            elif isinstance(value, dict):
                cursor_payload[part] = dict(value)
            else:
                raise _UnknownConfigKeyError(key)
            cursor_payload = cursor_payload[part]
            cursor_cfg = value
    return payload


def _add_flat(table: Table, data: dict, prefix: str = "") -> None:
    for k, v in data.items():
        full_key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            _add_flat(table, v, full_key)
        else:
            table.add_row(escape(full_key), escape(str(v)))
