from __future__ import annotations

import copy
from pathlib import Path

import pytest

from tests.architecture._graph_only_freeze import (
    CATEGORIES,
    FreezeBaselineError,
    capture_baseline,
    load_baseline,
    scan_tree,
    update_baseline,
    validate_baseline,
    verify_tree,
)
from tests.architecture._helpers import PROJECT_ROOT


BASELINE = Path(__file__).with_name("fixtures") / "graph_only_freeze_baseline.json"


def test_graph_only_legacy_dependency_baseline_is_exact() -> None:
    violations = verify_tree(PROJECT_ROOT, load_baseline(BASELINE))

    assert violations == []


def test_history_deletion_proof_is_an_explicit_non_runtime_audit_file() -> None:
    from tests.architecture._graph_only_freeze import NON_RUNTIME_AUDIT_FILES

    assert NON_RUNTIME_AUDIT_FILES == frozenset(
        {
            "scripts/graph_only_migration/deletion_proof.py",
            "scripts/graph_only_migration/zero_reference_scan.py",
        }
    )


def test_new_external_forbidden_import_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "backend/new_service.py",
        "from framework.workflow.runtime import WorkflowRunner\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_import_edge",
    )


def test_new_legacy_namespace_file_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(tmp_path, "framework/workflow/new_runtime.py", "VALUE = 1\n")

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_namespace_file",
    )


def test_new_internal_relative_import_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"framework/workflow/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "framework/workflow/service.py",
        "from .runtime import WorkflowRunner\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_import_edge",
    )


def test_symbol_added_to_existing_import_fails_as_expansion(tmp_path: Path) -> None:
    baseline = _capture(
        tmp_path,
        {
            "backend/service.py": (
                "from framework.workflow.runtime import WorkflowRunner\n"
                "VALUE = WorkflowRunner\n"
            )
        },
    )
    _write(
        tmp_path,
        "backend/service.py",
        "from framework.workflow.runtime import WorkflowExecutor, WorkflowRunner\n"
        "VALUE = WorkflowRunner\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_legacy_import_edge_expanded",
    )


def test_import_symbol_subtraction_can_advance_the_baseline(tmp_path: Path) -> None:
    baseline = _capture(
        tmp_path,
        {
            "backend/service.py": (
                "from framework.workflow.runtime import "
                "WorkflowExecutor, WorkflowRunner\n"
                "VALUE = WorkflowRunner\n"
            )
        },
    )
    _write(
        tmp_path,
        "backend/service.py",
        "from framework.workflow.runtime import WorkflowRunner\n"
        "VALUE = WorkflowRunner\n",
    )
    reduced = scan_tree(tmp_path)

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_baseline_not_monotonic",
    )
    updated = update_baseline(
        baseline,
        reduced,
        source_commit="b" * 40,
        source_tree="c" * 40,
    )

    assert verify_tree(tmp_path, updated) == []
    assert any(
        row["category"] == "import_edges"
        and row["key"].endswith("|WorkflowExecutor")
        for row in updated["retired"]
    )


@pytest.mark.parametrize(
    ("symbol", "owner_module"),
    [
        ("HarnessWorkflowSpec", "framework.harness.workflow.spec"),
        (
            "HarnessWorkflowGraphCompiler",
            "framework.harness.workflow.compiler",
        ),
    ],
)
def test_harness_workflow_facade_import_can_refine_to_exact_owner(
    tmp_path: Path,
    symbol: str,
    owner_module: str,
) -> None:
    baseline = _capture(
        tmp_path,
        {"backend/service.py": f"from framework.harness.workflow import {symbol}\n"},
    )
    _write(
        tmp_path,
        "backend/service.py",
        f"from {owner_module} import {symbol}\n",
    )
    current = scan_tree(tmp_path)

    assert verify_tree(tmp_path, baseline) == []
    updated = update_baseline(
        baseline,
        current,
        source_commit="b" * 40,
        source_tree="c" * 40,
    )
    assert verify_tree(tmp_path, updated) == []


def test_harness_workflow_facade_import_rejects_wrong_concrete_owner(
    tmp_path: Path,
) -> None:
    baseline = _capture(
        tmp_path,
        {
            "backend/service.py": (
                "from framework.harness.workflow import HarnessWorkflowSpec\n"
            )
        },
    )
    _write(
        tmp_path,
        "backend/service.py",
        (
            "from framework.harness.workflow.compiler import "
            "HarnessWorkflowSpec\n"
        ),
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_import_edge",
    )


@pytest.mark.parametrize(
    "source,code",
    [
        ("class WorkflowRunner:\n    pass\n", "graph_only_freeze_new_workflow_runner_symbol"),
        ("ALIAS = 'WorkflowExecutor'\n", "graph_only_freeze_new_workflow_executor_symbol"),
        (
            "def __getattr__(name):\n"
            "    if name == 'AgentLoopStepRunner':\n"
            "        return object\n",
            "graph_only_freeze_new_legacy_reflection_binding",
        ),
        ("RUNNER = 'RouterStepRunner'\n", "graph_only_freeze_new_legacy_reflection_binding"),
    ],
)
def test_new_definition_alias_or_reflection_fails(
    tmp_path: Path,
    source: str,
    code: str,
) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(tmp_path, "backend/service.py", source)

    assert _has_code(verify_tree(tmp_path, baseline), code)


@pytest.mark.parametrize("flag", ["False", "True"])
def test_lower_camel_boolean_policy_dictionary_keys_are_not_legacy_bindings(
    tmp_path: Path, flag: str
) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "backend/service.py",
        "def to_payload():\n"
        "    return {'permissions': {"
        f"'mayChangeWorkflow': {flag}, 'mayAuthorizeTools': False"
        "}}\n",
    )

    assert verify_tree(tmp_path, baseline) == []
    assert scan_tree(tmp_path).active["retired_symbols"] == {}


@pytest.mark.parametrize("symbol", ["WorkflowRunner", "WorkflowExecutor"])
def test_policy_dictionary_still_rejects_real_retired_reflection_values(
    tmp_path: Path, symbol: str
) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "backend/service.py",
        "PAYLOAD = {'mayChangeWorkflow': False, "
        f"'runner': '{symbol}'}}\n",
    )

    violations = verify_tree(tmp_path, baseline)
    expected = (
        "graph_only_freeze_new_workflow_runner_symbol"
        if symbol == "WorkflowRunner"
        else "graph_only_freeze_new_workflow_executor_symbol"
    )
    assert _has_code(violations, expected)
    symbols = scan_tree(tmp_path).active["retired_symbols"]
    assert any(key.endswith(f"|string_literal|{symbol}") for key in symbols)
    assert not any(key.endswith("|mayChangeWorkflow") for key in symbols)


@pytest.mark.parametrize(
    "source",
    [
        "RUNNER = getattr(module, 'WorkflowRunner')\n",
        "RUNNER = getattr(module, 'customWorkflowBinding')\n",
        "RUNNER = __import__('provider', fromlist=['WorkflowExecutor'])\n",
        "RUNNER = getattr(importlib.import_module('provider'), 'WorkflowRunner')\n",
        "def __getattr__(name):\n"
        "    if name == 'customWorkflowBinding':\n"
        "        return object\n",
        "ALIAS = 'customWorkflowBinding'\n",
        "PAYLOAD = {'mayChangeWorkflow': 'WorkflowRunner'}\n",
        "PAYLOAD = {'WorkflowRunner': False}\n",
        "PAYLOAD = {'customWorkflowRunner': False}\n",
        "PAYLOAD = {'LEGACY_WORKFLOW': False}\n",
        "PAYLOAD = {'StepRunner': False}\n",
        "def customWorkflowBinding():\n    pass\n",
        "from provider import customWorkflowBinding\n",
        "__all__ = ['customWorkflowBinding']\n",
    ],
)
def test_policy_key_exclusion_does_not_hide_retired_symbols_in_other_contexts(
    tmp_path: Path, source: str
) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(tmp_path, "backend/service.py", source)

    assert scan_tree(tmp_path).active["retired_symbols"]
    assert verify_tree(tmp_path, baseline)


@pytest.mark.parametrize(
    "source",
    [
        "from framework.workflow.runtime import WorkflowRunner\n",
        "from framework.harness.workflow import HarnessWorkflowSpec\n",
        "import framework.workflow.runtime\n",
    ],
)
def test_policy_key_exclusion_keeps_forbidden_namespace_imports_strict(
    tmp_path: Path, source: str
) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "backend/service.py",
        source + "PAYLOAD = {'mayChangeWorkflow': False}\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_import_edge",
    )


def test_new_legacy_public_export_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "__all__ = []\n"})
    _write(tmp_path, "backend/service.py", "__all__ = ['RunResult']\n")

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_public_export",
    )


def test_new_legacy_schema_writer_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "backend/service.py",
        "RENAMED = 'newsroom.workflow-event/v1'\n"
        "PAYLOAD = {'schema_version': RENAMED}\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_new_legacy_schema_writer",
    )


def test_legacy_schema_read_comparison_is_not_a_writer(tmp_path: Path) -> None:
    source = (
        "def is_legacy(value):\n"
        "    return value == 'newsroom.workflow-event/v1'\n"
    )
    baseline = _capture(tmp_path, {"backend/service.py": source})

    assert verify_tree(tmp_path, baseline) == []
    assert baseline["active"]["legacy_schema_writers"] == {}


def test_unregistered_migration_reader_fails(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"scripts/migration_reader.py": "VALUE = 1\n"})
    _write(
        tmp_path,
        "scripts/migration_reader.py",
        "from framework.workflow.runtime.manifest import validate_run_manifest\n",
    )

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_unregistered_migration_reader",
    )


def test_baseline_update_allows_only_subtraction(tmp_path: Path) -> None:
    baseline = _capture(
        tmp_path,
        {
            "backend/a.py": "from framework.workflow.runtime import WorkflowRunner\n",
            "backend/b.py": "VALUE = 1\n",
        },
    )
    _write(tmp_path, "backend/a.py", "VALUE = 1\n")
    reduced = scan_tree(tmp_path)
    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_baseline_not_monotonic",
    )
    updated = update_baseline(
        baseline,
        reduced,
        source_commit="b" * 40,
        source_tree="c" * 40,
    )

    assert verify_tree(tmp_path, updated) == []
    assert updated["generation"] == 2
    assert updated["retired"]

    _write(
        tmp_path,
        "backend/b.py",
        "from framework.workflow.runtime import WorkflowExecutor\n",
    )
    with pytest.raises(FreezeBaselineError, match="not subtract-only"):
        update_baseline(
            updated,
            scan_tree(tmp_path),
            source_commit="d" * 40,
            source_tree="e" * 40,
        )


def test_source_parse_failure_fails_closed(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    _write(tmp_path, "backend/service.py", "def broken(:\n")

    assert _has_code(
        verify_tree(tmp_path, baseline),
        "graph_only_freeze_source_parse_failed",
    )


def test_missing_or_incomplete_baseline_fields_fail_closed(tmp_path: Path) -> None:
    baseline = _capture(tmp_path, {"backend/service.py": "VALUE = 1\n"})
    missing = copy.deepcopy(baseline)
    del missing["active"]
    with pytest.raises(FreezeBaselineError, match="fields must be exactly"):
        validate_baseline(missing)

    incomplete_exception = copy.deepcopy(baseline)
    incomplete_exception["migration_reader_exceptions"] = [{"exact_path": "x.py"}]
    with pytest.raises(FreezeBaselineError, match="exception fields"):
        validate_baseline(incomplete_exception)


def test_duplicate_baseline_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text('{"schema": "one", "schema": "two"}\n', encoding="utf-8")

    with pytest.raises(FreezeBaselineError, match="duplicate JSON key"):
        load_baseline(path)


def _capture(project_root: Path, files: dict[str, str]) -> dict[str, object]:
    for root in ("backend", "framework", "infrastructure", "interfaces", "scripts"):
        (project_root / root).mkdir(parents=True, exist_ok=True)
    for relative_path, source in files.items():
        _write(project_root, relative_path, source)
    baseline = capture_baseline(
        project_root,
        source_commit="a" * 40,
        source_tree="0" * 40,
    )
    assert set(baseline["active"]) == set(CATEGORIES)
    return baseline


def _write(project_root: Path, relative_path: str, source: str) -> None:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _has_code(violations: list[str], code: str) -> bool:
    return any(violation.startswith(f"{code}|") for violation in violations)
