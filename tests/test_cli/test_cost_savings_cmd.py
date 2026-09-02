"""`agentos cost savings` — Pilot Router savings report from the decision log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


def _write_log(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "turn_id": "t1",
            "session_key": "s1",
            "prompt_hash": "a" * 16,
            "system_prompt_hash": "b" * 16,
            "tool_list_hash": "c" * 16,
            "tool_choice": "auto",
            "tokens_input": 1000,
            "tokens_output": 100,
            "model": "glm-5.2",
            "provider": "openrouter",
            "latency_ms": 900,
            "ts": "2026-09-01T10:00:00Z",
            "savings": {
                "routed_model": "glm-5.2",
                "baseline_model": "gpt-5.6-luna",
                "routing_confidence": 0.6,
                "routing_savings_pct": 20.0,
                "routing_savings_usd_estimated_vs_baseline": 0.25,
                "cost_usd": 0.75,
            },
        },
    ]
    (log_dir / "decisions-20260901.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    path = tmp_path / "logs"
    _write_log(path)
    return path


def _explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("cost savings must not talk to the gateway")


def test_savings_reads_the_decision_log_without_the_gateway(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir)])

    assert result.exit_code == 0, result.output
    assert "0.25" in result.output


def test_savings_json_carries_the_aggregate_and_route_rows(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["routingSavingsUsd"] == 0.25
    assert payload["topTierCostUsd"] == 1.0
    assert payload["savingsPct"] == 25.0
    assert payload["byRoute"][0]["routedModel"] == "glm-5.2"
    assert payload["byRoute"][0]["requestedModel"] == "gpt-5.6-luna"


def test_savings_csv_emits_one_row_per_route_pair(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--csv"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("RequestedModel,RoutedModel,Turns")
    assert "gpt-5.6-luna,glm-5.2,1" in lines[1]


def test_savings_pdf_writes_a_branded_report(
    log_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)
    out = tmp_path / "report.pdf"

    result = runner.invoke(app, ["cost", "savings", "--log-dir", str(log_dir), "--pdf", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_bytes().startswith(b"%PDF-")
    assert str(out) in result.output


def test_savings_date_window_is_applied(
    log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _explode)

    result = runner.invoke(
        app,
        [
            "cost",
            "savings",
            "--log-dir",
            str(log_dir),
            "--start-date",
            "2026-09-02",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["turnsRouted"] == 0
    assert payload["routingSavingsUsd"] == 0.0


def test_bare_cost_still_queries_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def _fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(True)
        return {"breakdown": [], "totalCostUsd": 0.0}

    monkeypatch.setattr("agentos.cli.cost_cmd.run_gateway_sync", _fake_run)

    result = runner.invoke(app, ["cost"])

    assert result.exit_code == 0, result.output
    assert calls == [True]


@pytest.fixture
def cost_breakdown() -> dict[str, Any]:
    """A minimal payload shape that exercises the CSV/JSON export paths."""

    return {
        "breakdown": [
            {
                "session": "session-1",
                "model": "glm-5.2",
                "provider": "openrouter",
                "agent_id": "main",
                "channel": "telegram",
                "tool_name": "web_search",
                "skill": "",
                "input_tokens": 1000,
                "output_tokens": 100,
                "cost_usd": 0.25,
                "created_at": 1756723200,
            },
        ],
        "totalCostUsd": 0.25,
    }


def test_cost_export_json_creates_missing_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost_breakdown: dict[str, Any]
) -> None:
    """Issue #846: ``agentos cost --export reports/usage.json`` previously
    crashed with FileNotFoundError when ``reports/`` did not yet exist. Both
    the JSON and CSV export branches must create the parent directory, the
    same way ``render_savings_pdf`` does for the PDF report.
    """

    monkeypatch.setattr(
        "agentos.cli.cost_cmd.run_gateway_sync",
        lambda *_args, **_kwargs: cost_breakdown,
    )

    nested = tmp_path / "reports" / "nested" / "usage.json"
    assert not nested.parent.exists()

    result = runner.invoke(app, ["cost", "--export", str(nested)])

    assert result.exit_code == 0, result.output
    assert nested.is_file()
    payload = json.loads(nested.read_text(encoding="utf-8"))
    assert payload["totalCostUsd"] == 0.25
    assert payload["breakdown"][0]["session"] == "session-1"


def test_cost_export_csv_creates_missing_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost_breakdown: dict[str, Any]
) -> None:
    """Companion of the JSON case for #846: ``--csv`` flag and a ``.csv``
    suffix must both go through the same parent-directory creation.
    """

    monkeypatch.setattr(
        "agentos.cli.cost_cmd.run_gateway_sync",
        lambda *_args, **_kwargs: cost_breakdown,
    )

    nested_csv = tmp_path / "exports" / "usage.csv"
    assert not nested_csv.parent.exists()

    result = runner.invoke(app, ["cost", "--export", str(nested_csv), "--csv"])

    assert result.exit_code == 0, result.output
    assert nested_csv.is_file()
    text = nested_csv.read_text(encoding="utf-8")
    # Header row + the breakdown row
    assert text.count("\n") >= 1
    assert "Session,Model,Provider" in text
    assert "session-1,glm-5.2" in text


def test_cost_export_at_an_existing_directory_does_not_recreate_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost_breakdown: dict[str, Any]
) -> None:
    """``mkdir(parents=True, exist_ok=True)`` must not raise when the
    directory is already present. This guards against a regression where a
    future refactor drops ``exist_ok``.
    """

    existing = tmp_path / "already"
    existing.mkdir()
    target = existing / "usage.json"

    monkeypatch.setattr(
        "agentos.cli.cost_cmd.run_gateway_sync",
        lambda *_args, **_kwargs: cost_breakdown,
    )

    result = runner.invoke(app, ["cost", "--export", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["totalCostUsd"] == 0.25
