"""Which stability scenarios every registered tool owes, and which it has.

Each tool gets a ``ToolProfile`` describing what it touches; the profile
decides which ``Dimension``s apply. Tests claim coverage with
``@pytest.mark.tool_scenario("<tool>", "<dimension code>", ...)``, and the
contract test cross-checks claims against this ledger: a tool marked
``delivered`` must cover every required dimension, and a newly registered
tool without a ledger entry fails the contract test.

Observability and end-to-end behaviour are checked once for the whole
registry (error shape in ``invoke.expect_failed``, Agent turns in the loop
scenarios), so they are not per-tool dimensions.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

TOOL_SCENARIO_MARKER = "tool_scenario"
TOOL_TESTS_ROOT = Path(__file__).resolve().parents[1]


class Dimension(str, Enum):
    INPUT = "A"
    DEPENDENCY_FAILURE = "B"
    TIMEOUT_RETRY = "C"
    RESOURCE_LIMIT = "D"
    CONCURRENCY = "E"
    WRITE_CONSISTENCY = "F"
    SECURITY = "G"


class ToolProfile(str, Enum):
    PURE = "pure"
    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    STORE_READ = "store_read"
    STORE_WRITE = "store_write"
    CONTROL_STATE = "control_state"


D = Dimension
PROFILE_DIMENSIONS: Mapping[ToolProfile, frozenset[Dimension]] = {
    ToolProfile.PURE: frozenset({D.INPUT, D.RESOURCE_LIMIT}),
    ToolProfile.NETWORK_READ: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.TIMEOUT_RETRY, D.RESOURCE_LIMIT, D.CONCURRENCY, D.SECURITY}
    ),
    ToolProfile.NETWORK_WRITE: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.TIMEOUT_RETRY, D.RESOURCE_LIMIT, D.WRITE_CONSISTENCY, D.SECURITY}
    ),
    ToolProfile.LOCAL_READ: frozenset({D.INPUT, D.DEPENDENCY_FAILURE, D.RESOURCE_LIMIT, D.SECURITY}),
    ToolProfile.LOCAL_WRITE: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.RESOURCE_LIMIT, D.WRITE_CONSISTENCY, D.SECURITY}
    ),
    ToolProfile.STORE_READ: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.TIMEOUT_RETRY, D.CONCURRENCY}
    ),
    ToolProfile.STORE_WRITE: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.TIMEOUT_RETRY, D.WRITE_CONSISTENCY, D.SECURITY}
    ),
    ToolProfile.CONTROL_STATE: frozenset(
        {D.INPUT, D.DEPENDENCY_FAILURE, D.TIMEOUT_RETRY, D.WRITE_CONSISTENCY}
    ),
}
WRITE_PROFILES = frozenset(
    {ToolProfile.NETWORK_WRITE, ToolProfile.LOCAL_WRITE, ToolProfile.STORE_WRITE}
)
EXTERNAL_PROFILES = frozenset(
    {
        ToolProfile.NETWORK_READ,
        ToolProfile.NETWORK_WRITE,
        ToolProfile.STORE_READ,
        ToolProfile.STORE_WRITE,
        ToolProfile.CONTROL_STATE,
    }
)


@dataclass(frozen=True)
class ToolLedgerEntry:
    name: str
    profile: ToolProfile
    phase: int
    extra: frozenset[Dimension] = frozenset()
    exempt: Mapping[Dimension, str] = field(default_factory=dict)
    delivered: bool = False

    @property
    def required(self) -> frozenset[Dimension]:
        return (PROFILE_DIMENSIONS[self.profile] | self.extra) - frozenset(self.exempt)


def _entry(
    name: str,
    profile: ToolProfile,
    phase: int,
    *extra: Dimension,
    exempt: Mapping[Dimension, str] | None = None,
    delivered: bool = False,
) -> ToolLedgerEntry:
    return ToolLedgerEntry(
        name=name,
        profile=profile,
        phase=phase,
        extra=frozenset(extra),
        exempt=dict(exempt or {}),
        delivered=delivered,
    )


P = ToolProfile
LEDGER: tuple[ToolLedgerEntry, ...] = (
    # Phase 2: network reads.
    _entry("source.fetch_url", P.NETWORK_READ, 2),
    _entry("source.fetch_official_blog", P.NETWORK_READ, 2),
    _entry("source.probe", P.NETWORK_READ, 2),
    _entry("source.check_health", P.NETWORK_READ, 2),
    _entry("source.parse_rss", P.PURE, 2, D.SECURITY),
    _entry("source.parse_atom", P.PURE, 2, D.SECURITY),
    _entry("source.extract_html", P.PURE, 2),
    _entry("source.extract_items", P.PURE, 2),
    _entry("source.extract_manual", P.PURE, 2),
    _entry("source.normalize_url", P.PURE, 2),
    _entry("source.search", P.PURE, 2),
    _entry("web.search", P.NETWORK_READ, 2),
    _entry("arxiv.search_papers", P.NETWORK_READ, 2),
    _entry("github.fetch_releases", P.NETWORK_READ, 2),
    _entry("github.search_repositories", P.NETWORK_READ, 2),
    # Phase 3: external writes and stores.
    _entry("notification.webhook", P.NETWORK_WRITE, 3),
    _entry("notification.slack", P.NETWORK_WRITE, 3),
    _entry("notification.email", P.NETWORK_WRITE, 3),
    _entry("notification.rss_publish", P.LOCAL_WRITE, 3, D.CONCURRENCY),
    _entry("postgres.save_report", P.STORE_WRITE, 3),
    _entry("postgres.insert_report", P.STORE_WRITE, 3),
    _entry("postgres.update_source_health", P.STORE_WRITE, 3),
    _entry("qdrant.search", P.STORE_READ, 3, D.SECURITY),
    _entry("qdrant.upsert", P.STORE_WRITE, 3),
    # Phase 4: local state and the rest.
    _entry("local_json.save", P.LOCAL_WRITE, 4, D.CONCURRENCY),
    _entry("artifact.load", P.LOCAL_READ, 4),
    _entry("artifact.search", P.LOCAL_READ, 4),
    _entry("artifact.write", P.LOCAL_WRITE, 4, D.CONCURRENCY),
    _entry("report.export", P.LOCAL_WRITE, 4),
    _entry("report.publish", P.STORE_WRITE, 4),
    _entry("report.search", P.STORE_READ, 4),
    _entry("report.render_json", P.PURE, 4),
    _entry("report.render_markdown", P.PURE, 4),
    _entry("report.validate", P.PURE, 4),
    _entry("quality.citation_check", P.PURE, 4),
    _entry("quality.claim_support_check", P.PURE, 4),
    _entry("quality.duplicate_check", P.PURE, 4),
    _entry("quality.editor_score", P.PURE, 4),
    _entry("memory.recall", P.STORE_READ, 4, D.SECURITY),
    _entry("memory.explain", P.PURE, 4),
    _entry("control.set_output", P.PURE, 4),
    _entry("control.report_progress", P.PURE, 4),
    _entry("control.request_human_review", P.CONTROL_STATE, 4),
    _entry("control.escalate", P.CONTROL_STATE, 4),
    _entry("control.delegate_to_subagent", P.CONTROL_STATE, 4),
)
LEDGER_BY_NAME: Mapping[str, ToolLedgerEntry] = {entry.name: entry for entry in LEDGER}


@dataclass(frozen=True)
class ScenarioClaim:
    tool_name: str
    dimensions: tuple[str, ...]
    location: str


def scenario_claims_in_source(source: str, *, location: str) -> list[ScenarioClaim]:
    """Every ``tool_scenario`` marker applied as a decorator in ``source``."""

    claims: list[ScenarioClaim] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == TOOL_SCENARIO_MARKER
            ):
                continue
            values = [arg.value for arg in decorator.args if isinstance(arg, ast.Constant)]
            if len(values) != len(decorator.args) or not values:
                raise ValueError(f"{location}:{decorator.lineno}: tool_scenario takes string literals")
            claims.append(
                ScenarioClaim(
                    tool_name=str(values[0]),
                    dimensions=tuple(str(value) for value in values[1:]),
                    location=f"{location}:{decorator.lineno}",
                )
            )
    return claims


def collect_scenario_claims(root: Path = TOOL_TESTS_ROOT) -> list[ScenarioClaim]:
    claims: list[ScenarioClaim] = []
    for path in sorted(root.rglob("test_*.py")):
        claims.extend(
            scenario_claims_in_source(
                path.read_text(encoding="utf-8"),
                location=path.relative_to(root).as_posix(),
            )
        )
    return claims


def claim_errors(claims: Iterable[ScenarioClaim], registered: Iterable[str]) -> list[str]:
    known_tools = set(registered)
    known_codes = {dimension.value for dimension in Dimension}
    errors: list[str] = []
    for claim in claims:
        if claim.tool_name not in known_tools:
            errors.append(f"{claim.location}: unknown tool {claim.tool_name!r}")
        if not claim.dimensions:
            errors.append(f"{claim.location}: {claim.tool_name} claims no dimension")
        for code in claim.dimensions:
            if code not in known_codes:
                errors.append(f"{claim.location}: unknown dimension {code!r}")
    return errors


def coverage_gaps(
    claims: Iterable[ScenarioClaim],
    ledger: Iterable[ToolLedgerEntry] = LEDGER,
) -> dict[str, list[str]]:
    """Missing dimension codes for every tool the ledger marks delivered."""

    covered: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        covered[claim.tool_name].update(claim.dimensions)
    gaps: dict[str, list[str]] = {}
    for entry in ledger:
        if not entry.delivered:
            continue
        missing = sorted({dimension.value for dimension in entry.required} - covered[entry.name])
        if missing:
            gaps[entry.name] = missing
    return gaps
