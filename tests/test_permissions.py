"""Tests for ``agentos.permissions`` shared posture helpers.

The module is small but sits in front of cron scheduling, gateway routing,
and the agent CLI command path. The boundary contracts exercised here:

* ``normalize_permission_mode`` — composed modes, default, "restricted"
  legacy spelling, casing/whitespace, and the surfaced ``ValueError`` for
  unrecognised input.
* ``normalize_cron_elevated`` — the cron-specific validator that refuses
  ``"on"`` because cron runs are unattended.
* ``cron_tool_policy_elevated`` — the read-side counterpart that must
  *never* raise regardless of legacy or hand-edited rows.
* ``configured_default_elevated``/``configured_cron_default_elevated`` —
  config-driven defaults that gate whether elevated routes are reachable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.permissions import (
    CRON_ELEVATED_MODES,
    ELEVATED_PERMISSION_MODES,
    PERMISSION_MODES,
    configured_cron_default_elevated,
    configured_default_elevated,
    cron_tool_policy_elevated,
    normalize_cron_elevated,
    normalize_permission_mode,
)

# ---------------------------------------------------------------------------
# normalize_permission_mode
# ---------------------------------------------------------------------------


def test_normalize_permission_mode_returns_canonical_modes() -> None:
    assert normalize_permission_mode("off") == "off"
    assert normalize_permission_mode("on") == "on"
    assert normalize_permission_mode("bypass") == "bypass"
    assert normalize_permission_mode("full") == "full"


def test_normalize_permission_mode_treats_restricted_as_off() -> None:
    # "restricted" is the historical spelling from before the cleanup that
    # collapsed it down to "off". It must keep working but cannot leak
    # through the API surface as a distinct value.
    assert normalize_permission_mode("restricted") == "off"
    assert "restricted" not in PERMISSION_MODES
    assert "restricted" not in ELEVATED_PERMISSION_MODES


def test_normalize_permission_mode_normalizes_case_and_whitespace() -> None:
    assert normalize_permission_mode("  BYPASS  ") == "bypass"
    assert normalize_permission_mode("FuLl") == "full"


def test_normalize_permission_mode_uses_default_when_none() -> None:
    assert normalize_permission_mode(None) == "off"
    assert normalize_permission_mode(None, default="bypass") == "bypass"


def test_normalize_permission_mode_default_does_not_swallow_strings() -> None:
    # The validator falls back to the default only for ``None``; an empty
    # string is treated as the (unrecognised) value ``""`` and must raise so
    # config bugs do not silently default to elevated.
    with pytest.raises(ValueError, match="permissions must be one of"):
        normalize_permission_mode("", default="off")


def test_normalize_permission_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_permission_mode("banana")
    # Error must enumerate the full valid set so users can self-correct.
    message = str(exc_info.value)
    for allowed in ("bypass", "full", "off", "on", "restricted"):
        assert allowed in message


def test_normalize_permission_mode_frozen_sets_have_expected_shape() -> None:
    # Locked-in invariants: "off" is not considered elevated; "on" is;
    # cron-elevated is a strict subset (omitting "on", which needs a human).
    assert "off" not in ELEVATED_PERMISSION_MODES
    assert "on" in ELEVATED_PERMISSION_MODES
    assert ELEVATED_PERMISSION_MODES <= PERMISSION_MODES
    assert CRON_ELEVATED_MODES == {"bypass", "full"}
    assert "on" not in CRON_ELEVATED_MODES


# ---------------------------------------------------------------------------
# normalize_cron_elevated
# ---------------------------------------------------------------------------


def test_normalize_cron_elevated_none_means_not_elevated() -> None:
    assert normalize_cron_elevated(None) is None


def test_normalize_cron_elevated_bool_spelling() -> None:
    assert normalize_cron_elevated(True) == "bypass"
    assert normalize_cron_elevated(False) == "off"


def test_normalize_cron_elevated_empty_string_is_not_elevated() -> None:
    # An empty string is the same shape as "not set" — never treat it as
    # an unknown token and never coerce it to elevated.
    assert normalize_cron_elevated("") is None


def test_normalize_cron_elevated_accepts_bypass_and_full() -> None:
    assert normalize_cron_elevated("bypass") == "bypass"
    assert normalize_cron_elevated("FULL") == "full"
    assert normalize_cron_elevated("  bypass  ") == "bypass"


def test_normalize_cron_elevated_rejects_on_for_cron() -> None:
    # "on" needs a human approver; cron runs are unattended, so this is the
    # one mode that is *allowed* for interactive use but *forbidden* here.
    with pytest.raises(ValueError, match="cannot use 'on'"):
        normalize_cron_elevated("on")


def test_normalize_cron_elevated_rejects_unknown_value() -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_cron_elevated("paranoid")
    message = str(exc_info.value)
    for allowed in ("bypass", "full", "off"):
        assert allowed in message


# ---------------------------------------------------------------------------
# cron_tool_policy_elevated — read path, must never raise
# ---------------------------------------------------------------------------


def test_cron_tool_policy_elevated_returns_none_for_non_mapping() -> None:
    # Strings/ints/lists/None all read as "not elevated" — this is the
    # catch-all that keeps corrupted rows from breaking routing.
    assert cron_tool_policy_elevated(None) is None
    assert cron_tool_policy_elevated("bypass") is None
    assert cron_tool_policy_elevated(42) is None
    assert cron_tool_policy_elevated(["bypass"]) is None


def test_cron_tool_policy_elevated_reads_full_mapping() -> None:
    assert cron_tool_policy_elevated({"elevated": "bypass"}) == "bypass"
    assert cron_tool_policy_elevated({"elevated": "full"}) == "full"


def test_cron_tool_policy_elevated_ignores_off_and_missing() -> None:
    assert cron_tool_policy_elevated({}) is None
    assert cron_tool_policy_elevated({"elevated": None}) is None
    assert cron_tool_policy_elevated({"elevated": False}) is None
    assert cron_tool_policy_elevated({"elevated": True}) == "bypass"
    assert cron_tool_policy_elevated({"elevated": "off"}) is None
    assert cron_tool_policy_elevated({"elevated": ""}) is None


def test_cron_tool_policy_elevated_swallows_legacy_or_invalid_values() -> None:
    # "on" is invalid for cron but the read path must not raise — it is
    # sufficient to read it as "not elevated" so legacy rows do not break
    # routing. Garbage values are treated the same way.
    assert cron_tool_policy_elevated({"elevated": "on"}) is None
    assert cron_tool_policy_elevated({"elevated": "banana"}) is None
    assert cron_tool_policy_elevated({"elevated": 7}) is None


def test_cron_tool_policy_elevated_ignores_unrelated_keys() -> None:
    # The read path keys off "elevated" only; loose extras are tolerated
    # because persisted policy blobs can carry unrelated metadata.
    assert cron_tool_policy_elevated({"sandbox": "bypass"}) is None
    assert cron_tool_policy_elevated(
        {"elevated": "full", "tool": "shell", "tags": ["a", "b"]}
    ) == "full"


# ---------------------------------------------------------------------------
# configured_default_elevated / configured_cron_default_elevated
# ---------------------------------------------------------------------------


def test_configured_default_elevated_none_when_no_permissions() -> None:
    # Missing block or None block → default-mode path is "off" → not elevated.
    assert configured_default_elevated(SimpleNamespace()) is None
    assert configured_default_elevated(SimpleNamespace(permissions=None)) is None


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("off", None),
        ("restricted", None),
        ("bypass", "bypass"),
        ("full", "full"),
        ("ON", "on"),
    ],
)
def test_configured_default_elevated_maps_mode_subset(
    mode: str, expected: str | None
) -> None:
    cfg = SimpleNamespace(permissions=SimpleNamespace(default_mode=mode))
    assert configured_default_elevated(cfg) == expected


def test_configured_default_elevated_uses_off_default_when_value_missing() -> None:
    cfg = SimpleNamespace(permissions=SimpleNamespace(default_mode=None))
    assert configured_default_elevated(cfg) is None


def test_configured_default_elevated_propagates_value_error() -> None:
    # Unknown modes are a configuration error, not metadata. The read path
    # must surface it instead of silently dropping to "off".
    cfg = SimpleNamespace(permissions=SimpleNamespace(default_mode="banana"))
    with pytest.raises(ValueError, match="permissions must be one of"):
        configured_default_elevated(cfg)


def test_configured_cron_default_elevated_defaults_to_bypass() -> None:
    # Cron runs are unattended; the safe default for "is the cron path
    # elevated by config?" is "yes, bypass" — an explicit perm block can
    # override it, but its absence must not accidentally disable cron.
    assert configured_cron_default_elevated(SimpleNamespace()) == "bypass"
    assert configured_cron_default_elevated(SimpleNamespace(permissions=None)) == "bypass"


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("bypass", "bypass"),
        ("FULL", "full"),
        ("off", None),
        ("on", None),
        ("restricted", None),
    ],
)
def test_configured_cron_default_elevated_maps_cron_aware_subset(
    mode: str, expected: str | None
) -> None:
    # "on" is filtered out because cron-elevation disallows it; the read
    # helper returns None (= "not elevated for cron") rather than upfront
    # failing, matching the existing cron_tool_policy_elevated contract.
    cfg = SimpleNamespace(permissions=SimpleNamespace(cron_default_mode=mode))
    assert configured_cron_default_elevated(cfg) == expected


def test_configured_cron_default_elevated_no_block_keeps_bypass() -> None:
    # When perm block exists but the cron-default key is missing, the
    # documented default is still "bypass".
    cfg = SimpleNamespace(permissions=SimpleNamespace(default_mode="off"))
    assert configured_cron_default_elevated(cfg) == "bypass"


def test_configured_cron_default_elevated_propagates_value_error() -> None:
    cfg = SimpleNamespace(permissions=SimpleNamespace(cron_default_mode="banana"))
    with pytest.raises(ValueError, match="permissions must be one of"):
        configured_cron_default_elevated(cfg)
