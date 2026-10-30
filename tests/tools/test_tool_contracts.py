"""Registry-wide contracts every tool definition must keep.

These run over the complete production registry (every optional dependency
wired), so a new tool or a changed definition is checked without anyone
remembering to add it here.
"""

from __future__ import annotations

import pytest

from framework.llm.clients.tool_adapters import to_openai_tools
from framework.tool import ToolDefinition, ToolPolicy
from framework.tool.models import ToolSideEffect, is_default_dangerous_tool_name
from framework.tool.runtime.mcp_adapter import _KNOWN_REMOTE_SIDE_EFFECTS
from tests.tools.harness.registry import build_tool_environment
from tests.tools.harness.tool_ledger import (
    EXTERNAL_PROFILES,
    LEDGER,
    LEDGER_BY_NAME,
    WRITE_PROFILES,
    Dimension,
    ScenarioClaim,
    ToolLedgerEntry,
    ToolProfile,
    claim_errors,
    collect_scenario_claims,
    coverage_gaps,
    scenario_claims_in_source,
)

_READ_ONLY_SIDE_EFFECTS = frozenset({"", "none", "read_only"})
_KNOWN_SIDE_EFFECTS = frozenset(_KNOWN_REMOTE_SIDE_EFFECTS) | {item.value for item in ToolSideEffect}


@pytest.fixture(scope="module")
def definitions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ToolDefinition]:
    environment = build_tool_environment(tmp_path_factory.mktemp("contracts"))
    return {definition.name: definition for definition in environment.registry.list_tools()}


def _each(definitions: dict[str, ToolDefinition]) -> list[tuple[str, ToolDefinition]]:
    return sorted(definitions.items())


def test_ledger_lists_exactly_the_registered_tools(definitions) -> None:
    ledger_names = [entry.name for entry in LEDGER]
    assert len(ledger_names) == len(set(ledger_names)), "ledger lists a tool twice"
    assert sorted(ledger_names) == sorted(definitions), (
        f"unledgered tools: {sorted(set(definitions) - set(ledger_names))}; "
        f"ledgered but unregistered: {sorted(set(ledger_names) - set(definitions))}"
    )


def test_every_schema_converts_for_the_model_provider(definitions) -> None:
    failures = []
    for name, definition in _each(definitions):
        try:
            to_openai_tools([definition.to_dict()])
        except Exception as exc:  # noqa: BLE001 - report every broken tool at once
            failures.append(f"{name}: {exc}")
    assert failures == []


def test_every_schema_rejects_undeclared_arguments(definitions) -> None:
    assert [
        name
        for name, definition in _each(definitions)
        if definition.input_schema.get("additionalProperties") is not False
    ] == []


def test_required_arguments_are_declared_properties(definitions) -> None:
    assert {
        name: sorted(set(definition.required_arguments) - set(definition.input_schema.get("properties") or {}))
        for name, definition in _each(definitions)
        if set(definition.required_arguments) - set(definition.input_schema.get("properties") or {})
    } == {}


def test_side_effects_use_the_framework_vocabulary(definitions) -> None:
    assert {
        name: definition.side_effect_value
        for name, definition in _each(definitions)
        if definition.side_effect_value not in _KNOWN_SIDE_EFFECTS
    } == {}


def test_parallel_safe_tools_are_read_only_and_ungated(definitions) -> None:
    """A tool declared concurrency safe must actually qualify for the parallel lane."""

    policy = ToolPolicy()
    assert [
        name
        for name, definition in _each(definitions)
        if definition.concurrency_safe
        and (
            definition.side_effect_value not in _READ_ONLY_SIDE_EFFECTS
            or definition.is_dangerous
            or is_default_dangerous_tool_name(name)
            or policy.requires_approval(definition)
        )
    ] == []


def test_ledger_profile_matches_the_declared_side_effect(definitions) -> None:
    mismatches = []
    for name, definition in _each(definitions):
        entry = LEDGER_BY_NAME.get(name)
        if entry is None or entry.profile is ToolProfile.CONTROL_STATE:
            continue
        writes = definition.side_effect_value not in _READ_ONLY_SIDE_EFFECTS
        if writes != (entry.profile in WRITE_PROFILES):
            mismatches.append(f"{name}: profile={entry.profile.value} side_effect={definition.side_effect_value}")
    assert mismatches == []


def test_every_write_tool_needs_approval_by_default(definitions) -> None:
    policy = ToolPolicy()
    assert [
        name
        for name, definition in _each(definitions)
        if LEDGER_BY_NAME[name].profile in WRITE_PROFILES and not policy.requires_approval(definition)
    ] == []


def test_delivered_external_tools_declare_their_own_timeout(definitions) -> None:
    """External calls must budget their own timeout instead of inheriting the 30s policy default."""

    assert [
        name
        for name, definition in _each(definitions)
        if LEDGER_BY_NAME[name].delivered
        and LEDGER_BY_NAME[name].profile in EXTERNAL_PROFILES
        and definition.timeout_seconds is None
    ] == []


def test_scenario_markers_name_real_tools_and_dimensions(definitions) -> None:
    assert claim_errors(collect_scenario_claims(), definitions) == []


def test_delivered_tools_cover_every_required_dimension() -> None:
    assert coverage_gaps(collect_scenario_claims()) == {}


def test_coverage_check_reports_unclaimed_dimensions() -> None:
    source = '''
import pytest

@pytest.mark.tool_scenario("demo.read", "A", "B")
def test_input_and_dependency():
    pass

class TestDemo:
    @pytest.mark.tool_scenario("demo.read", "Z")
    def test_unknown_dimension(self):
        pass
'''
    claims = scenario_claims_in_source(source, location="synthetic.py")
    assert claims == [
        ScenarioClaim("demo.read", ("A", "B"), "synthetic.py:4"),
        ScenarioClaim("demo.read", ("Z",), "synthetic.py:9"),
    ]
    assert claim_errors(claims, ["demo.read"]) == ["synthetic.py:9: unknown dimension 'Z'"]
    delivered = ToolLedgerEntry(
        name="demo.read",
        profile=ToolProfile.NETWORK_READ,
        phase=1,
        exempt={Dimension.SECURITY: "fixed upstream host"},
        delivered=True,
    )
    assert coverage_gaps(claims, [delivered]) == {"demo.read": ["C", "D", "E"]}


def test_tool_test_markers_are_registered(pytestconfig: pytest.Config) -> None:
    registered = {
        line.partition(":")[0].partition("(")[0].strip() for line in pytestconfig.getini("markers")
    }
    assert {"tool_scenario", "leak_grace", "tool_stress", "tool_live"} <= registered
