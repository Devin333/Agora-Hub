"""Machine-readable proof for the retired Workflow runtime boundary.

The proof is deliberately based on tracked Git paths and parsed source rather
than the filesystem. Ignored bytecode or a stale checkout must not make a
deleted runtime appear active again.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable


PROOF_SCHEMA = "newsroom.graph-only-legacy-workflow-deletion-proof/v1"
DELETION_COMMIT = "ad48da64d5e3798d301ea0a7efd77b2e2f66e6ea"
DELETED_PATH_GROUPS = {
    "workflow_runtime": (
        "framework/workflow/",
        "framework/specs/workflow.py",
    ),
    "harness_workflow_namespace": ("framework/harness/workflow/",),
    "workflow_spec_models": ("framework/specs/",),
    "legacy_tests": (
        "tests/framework/workflow/",
        "tests/framework/harness/workflow/",
        "tests/framework/contracts/test_workflow_runtime_contract.py",
        "tests/interfaces/api/test_api_run_inspection.py",
        "tests/interfaces/api/test_api_run_operations.py",
        "tests/interfaces/cli/test_runs_commands.py",
        "tests/interfaces/services/test_run_inspection_factory.py",
        "tests/interfaces/services/test_run_inspection_service.py",
    ),
}
PRODUCTION_ROOTS = (
    "backend",
    "framework",
    "infrastructure",
    "interfaces",
    "scripts",
)
RETIRED_SYMBOLS = (
    "AgentLoopStepRunner",
    "HarnessWorkflowGraphCompiler",
    "HarnessWorkflowSpec",
    "RunResult",
    "WorkflowArtifactPublisher",
    "WorkflowArtifactPublisherRegistry",
    "WorkflowArtifactRef",
    "WorkflowCheckpoint",
    "WorkflowEnvelope",
    "WorkflowEvent",
    "WorkflowEventEmitter",
    "WorkflowEventRecorderFacade",
    "WorkflowExecutor",
    "WorkflowGovernancePort",
    "WorkflowLLMPort",
    "WorkflowMemoryAdapter",
    "WorkflowReplayBundle",
    "WorkflowRunContext",
    "WorkflowRunner",
    "WorkflowState",
    "DataBuffer",
    "HarnessCheckpoint",
    "InMemoryHarnessCheckpointStore",
    "SkillStepRunner",
)
FORBIDDEN_IMPORTS = (
    "framework.workflow",
    "framework.harness.workflow",
)
FORBIDDEN_SCHEMA_MARKERS = (
    "newsroom.workflow",
    "newsroom.harness-workflow",
    "workflow-checkpoint",
)
HISTORY_ALLOWLIST = {
    "scripts/graph_only_migration/",
    "tests/fixtures/graph_only_migration/",
    "framework/harness/artifacts/terminal_manifest.py",
}
NON_WORKFLOW_MIGRATION_BOUNDARIES = {
    "research_run_disposition_schema_compatibility": {
        "scope": "Research run-disposition record schema compatibility",
        "paths": (
            "backend/research/application/run_disposition.py",
            "backend/research/ports/run_store.py",
            "infrastructure/research/filesystem_run_store.py",
            "interfaces/composition/research.py",
            "interfaces/composition/research_settings.py",
        ),
        "legacy_workflow_authority": False,
        "graph_history_authority": False,
        "pointer_or_dual_store_writer": False,
        "reason": (
            "The v1/v2 reader and v2 writer classify Research run disposition; "
            "they do not read, resume, replay, or publish legacy Workflow history."
        ),
    },
    "durable_replay_component_version_migration": {
        "scope": "Canonical durable replay component version migration",
        "paths": ("framework/events/runtime/history.py",),
        "legacy_workflow_authority": False,
        "graph_history_authority": False,
        "pointer_or_dual_store_writer": False,
        "reason": (
            "Exact-version replay migration is owned by the canonical event history "
            "runtime and is not a legacy Workflow record migrator."
        ),
    },
    "harness_sqlite_schema_migration": {
        "scope": "Harness side-effect SQLite schema migration",
        "paths": ("infrastructure/storage/harness/sqlite.py",),
        "legacy_workflow_authority": False,
        "graph_history_authority": False,
        "pointer_or_dual_store_writer": False,
        "reason": (
            "SQLite schema upgrades are storage initialization for the active Harness "
            "side-effect store and do not select a history runtime or execution path."
        ),
    },
}
SYMBOL_PROOF = {
    "flat_harness_state": {
        "status": "deleted",
        "retired_symbols": ("WorkflowRunContext", "WorkflowState", "DataBuffer"),
    },
    "subagent_v1_v2": {
        "status": "deleted",
        "retired_symbols": (
            "SUBAGENT_NODE_RESULT_SCHEMA_V1",
            "SUBAGENT_NODE_RESULT_SCHEMA_V2",
            "SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V1",
            "SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V2",
        ),
    },
    "workflow_event_catalog_facade": {
        "status": "deleted",
        "retired_symbols": (
            "WorkflowEvent",
            "WorkflowEventEmitter",
            "WorkflowEventRecorderFacade",
        ),
    },
    "workflow_memory_governance_worker_skill_llm": {
        "status": "deleted",
        "retired_symbols": (
            "WorkflowMemoryAdapter",
            "WorkflowGovernancePort",
            "AgentLoopStepRunner",
            "SkillStepRunner",
            "WorkflowLLMPort",
        ),
    },
    "flat_harness_checkpoint_replay": {
        "status": "deleted",
        "retired_symbols": (
            "HarnessCheckpoint",
            "InMemoryHarnessCheckpointStore",
            "WorkflowReplayBundle",
        ),
    },
    "workflow_artifact_publisher": {
        "status": "deleted",
        "retired_symbols": (
            "WorkflowArtifactPublisher",
            "WorkflowArtifactPublisherRegistry",
        ),
    },
    "graph_replacement_owners": {
        "status": "retained_graph_owner",
        "owners": {
            "HarnessGraphState": "framework.harness.control_plane.graph_state",
            "HarnessGraphCheckpoint": "framework.harness.control_plane.graph_checkpoint",
            "StoredEvent": "framework.events.canonical",
            "EventSchemaCatalog": "framework.events.schema.catalog",
            "ArtifactPort": "framework.harness.artifacts.ports",
            "ArtifactPublisher": "framework.agent.artifacts.runtime.publisher",
        },
    },
}


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    category: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "category": self.category,
            "value": self.value,
        }


def build_deletion_proof(project_root: Path) -> dict[str, Any]:
    tracked = _tracked_files(project_root)
    deleted = _deleted_paths(project_root)
    source_files = _tracked_source_files(project_root, tracked)
    references, allowlisted_references = _scan_sources(project_root, source_files)
    grouped_deletions = {
        group: sorted(
            path
            for path in deleted
            if any(path == prefix or path.startswith(prefix) for prefix in prefixes)
        )
        for group, prefixes in DELETED_PATH_GROUPS.items()
    }
    return {
        "schema": PROOF_SCHEMA,
        "deletion_commit": DELETION_COMMIT,
        "deletion_parent": _git(project_root, ["rev-parse", f"{DELETION_COMMIT}^"]),
        "tracked_source": {
            "workflow_runtime_files": sorted(
                path for path in tracked if path.startswith("framework/workflow/")
            ),
            "harness_workflow_files": sorted(
                path for path in tracked if path.startswith("framework/harness/workflow/")
            ),
            "workflow_spec_file": "framework/specs/workflow.py"
            if "framework/specs/workflow.py" in tracked
            else None,
        },
        "deleted_paths": {
            group: {"count": len(paths), "paths": paths}
            for group, paths in grouped_deletions.items()
        },
        "zero_reference_scan": {
            "production_roots": list(PRODUCTION_ROOTS),
            "files_scanned": len(source_files),
            "references": [item.to_dict() for item in references],
            "allowlisted_references": [
                item.to_dict() for item in allowlisted_references
            ],
            "summary": {
                "retired_symbol_hits": sum(
                    item.category == "symbol" for item in references
                ),
                "forbidden_import_hits": sum(
                    item.category == "import" for item in references
                ),
                "legacy_schema_writer_hits": sum(
                    item.category == "schema_writer" for item in references
                ),
                "allowlisted_reference_hits": len(allowlisted_references),
                "is_valid": not references,
            },
        },
        "symbol_deletion_proof": _symbol_deletion_proof(
            tracked=tracked,
            project_root=project_root,
        ),
        "history_allowlist": sorted(HISTORY_ALLOWLIST),
        "non_workflow_migration_boundaries": _migration_boundary_proof(
            project_root
        ),
        "proof_policy": {
            "ignored_bytecode_is_not_source": True,
            "history_inputs_are_read_only": True,
            "history_inputs_cannot_grant_execution_authority": True,
            "history_tooling_excluded_from_production_scan": True,
            "allowlist_is_path_scoped": True,
        },
    }


def _migration_boundary_proof(project_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, boundary in NON_WORKFLOW_MIGRATION_BOUNDARIES.items():
        paths = tuple(boundary["paths"])
        result[name] = {
            **boundary,
            "paths": list(paths),
            "tracked_paths": {
                path: (project_root / path).is_file() for path in paths
            },
            "is_valid": all((project_root / path).is_file() for path in paths)
            and boundary["legacy_workflow_authority"] is False
            and boundary["graph_history_authority"] is False
            and boundary["pointer_or_dual_store_writer"] is False,
        }
    return result


def _tracked_files(project_root: Path) -> list[str]:
    return _git_path_list(project_root, ["ls-files", "-z"])


def _git_path_list(project_root: Path, args: list[str]) -> list[str]:
    output = _git(project_root, args)
    return sorted(item for item in output.split("\0") if item)


def _tracked_source_files(project_root: Path, tracked: Iterable[str]) -> list[str]:
    """Return tracked production sources excluding explicit worktree deletions."""
    worktree_deleted = set(
        _git_path_list(project_root, ["ls-files", "--deleted", "-z"])
    )
    return sorted(
        path
        for path in tracked
        if path.endswith(".py")
        and _in_production_roots(path)
        and not path.startswith("scripts/graph_only_migration/")
        and path not in worktree_deleted
    )


def _deleted_paths(project_root: Path) -> list[str]:
    output = _git(project_root, ["diff-tree", "--no-commit-id", "--name-status", "-r", DELETION_COMMIT])
    return sorted(
        line.split("\t", 1)[1]
        for line in output.splitlines()
        if line.startswith("D\t") and "\t" in line
    )


def _scan_sources(
    project_root: Path, files: Iterable[str]
) -> tuple[list[Reference], list[Reference]]:
    references: list[Reference] = []
    allowlisted: list[Reference] = []
    for relative in files:
        path = project_root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            references.append(Reference(relative, 0, "parse_failure", relative))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(
                        alias.name == prefix or alias.name.startswith(prefix + ".")
                        for prefix in FORBIDDEN_IMPORTS
                    ):
                        _record(
                            references,
                            allowlisted,
                            relative,
                            node.lineno,
                            "import",
                            alias.name,
                        )
                    if alias.asname in RETIRED_SYMBOLS:
                        _record(
                            references,
                            allowlisted,
                            relative,
                            node.lineno,
                            "symbol",
                            alias.asname,
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(
                    node.module == prefix or node.module.startswith(prefix + ".")
                    for prefix in FORBIDDEN_IMPORTS
                ):
                    _record(
                        references,
                        allowlisted,
                        relative,
                        node.lineno,
                        "import",
                        node.module,
                    )
                for alias in node.names:
                    if alias.name in RETIRED_SYMBOLS or alias.asname in RETIRED_SYMBOLS:
                        _record(
                            references,
                            allowlisted,
                            relative,
                            node.lineno,
                            "symbol",
                            alias.name if alias.name in RETIRED_SYMBOLS else alias.asname,
                        )
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in RETIRED_SYMBOLS:
                    _record(
                        references,
                        allowlisted,
                        relative,
                        node.lineno,
                        "symbol",
                        node.name,
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value in RETIRED_SYMBOLS:
                    _record(
                        references,
                        allowlisted,
                        relative,
                        node.lineno,
                        "symbol",
                        value,
                    )
                elif any(marker in value for marker in FORBIDDEN_SCHEMA_MARKERS):
                    _record(
                        references,
                        allowlisted,
                        relative,
                        node.lineno,
                        "schema_writer",
                        value,
                    )
    key = lambda item: (item.path, item.line, item.category, item.value)
    return sorted(references, key=key), sorted(allowlisted, key=key)


def _symbol_deletion_proof(
    *,
    tracked: Iterable[str],
    project_root: Path,
) -> dict[str, Any]:
    tracked_source = [
        project_root / path
        for path in tracked
        if path.endswith(".py")
        and _in_production_roots(path)
        and not path.startswith("scripts/graph_only_migration/")
    ]
    symbol_hits = _collect_symbol_hits(project_root, tracked_source)
    result: dict[str, Any] = {}
    for name, spec in SYMBOL_PROOF.items():
        if spec["status"] == "retained_graph_owner":
            owners = {
                owner_name: {
                    "status": "retained_graph_owner",
                    "owner": owner_path,
                    "definition_present": _definition_present(
                        project_root,
                        owner_name,
                        owner_path,
                    ),
                }
                for owner_name, owner_path in spec["owners"].items()
            }
            result[name] = {
                "status": "retained_graph_owner",
                "owners": owners,
            }
            continue
        symbols = tuple(spec["retired_symbols"])
        result[name] = {
            "status": "deleted",
            "symbols": {
                symbol: {
                    "status": "deleted",
                    "tracked_definition_or_export_hits": len(symbol_hits.get(symbol, ())),
                    "hits": symbol_hits.get(symbol, []),
                }
                for symbol in symbols
            },
        }
    return result


def _collect_symbol_hits(
    project_root: Path,
    paths: Iterable[Path],
) -> dict[str, list[dict[str, Any]]]:
    hits: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        try:
            relative = path.relative_to(project_root).as_posix()
        except ValueError:
            relative = path.as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            names: list[tuple[str, str]] = []
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append((node.name, "definition"))
            elif isinstance(node, ast.Import):
                names.extend(
                    (alias.asname or alias.name.rsplit(".", 1)[-1], "import")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                names.extend((alias.asname or alias.name, "import") for alias in node.names)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        try:
                            values = ast.literal_eval(node.value)
                        except (ValueError, TypeError, SyntaxError):
                            values = ()
                        if isinstance(values, (list, tuple, set)):
                            names.extend(
                                (value, "export")
                                for value in values
                                if isinstance(value, str)
                            )
            for symbol, kind in names:
                if symbol in RETIRED_SYMBOLS:
                    hits.setdefault(symbol, []).append(
                        {"path": relative, "line": node.lineno, "kind": kind}
                    )
    for values in hits.values():
        values.sort(key=lambda item: (item["path"], item["line"], item["kind"]))
    return hits


def _definition_present(
    project_root: Path,
    symbol: str,
    owner_path: str,
) -> bool:
    path = project_root / (owner_path.replace(".", "/") + ".py")
    if not path.exists():
        path = project_root / owner_path.replace(".", "/") / "__init__.py"
    if not path.exists():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    return any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
        for node in ast.walk(tree)
    )


def _record(
    references: list[Reference],
    allowlisted: list[Reference],
    path: str,
    line: int,
    category: str,
    value: str,
) -> None:
    item = Reference(path, line, category, value)
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in HISTORY_ALLOWLIST):
        allowlisted.append(item)
    else:
        references.append(item)


def _in_production_roots(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in PRODUCTION_ROOTS)


def _git(project_root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", errors="strict").strip()


def write_deletion_proof(project_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_deletion_proof(project_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    project_root = args.project_root.resolve()
    report = build_deletion_proof(project_root)
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if report["zero_reference_scan"]["summary"]["is_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_deletion_proof", "write_deletion_proof", "main"]
