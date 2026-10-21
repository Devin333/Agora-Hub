from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.graph_only_migration.deletion_proof import (
    DELETION_COMMIT,
    _scan_sources,
    _tracked_files,
    _tracked_source_files,
    build_deletion_proof,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_workflow_deletion_proof_is_zero_reference_and_tracked_path_based() -> None:
    proof = build_deletion_proof(PROJECT_ROOT)

    assert proof["deletion_commit"] == DELETION_COMMIT
    assert proof["tracked_source"]["workflow_runtime_files"] == []
    assert proof["tracked_source"]["harness_workflow_files"] == []
    assert proof["tracked_source"]["workflow_spec_file"] is None
    assert all(row["count"] > 0 for row in proof["deleted_paths"].values())
    assert proof["zero_reference_scan"]["summary"] == {
        "retired_symbol_hits": 0,
        "forbidden_import_hits": 0,
        "legacy_schema_writer_hits": 0,
        "allowlisted_reference_hits": 1,
        "is_valid": True,
    }
    assert proof["proof_policy"]["ignored_bytecode_is_not_source"] is True
    assert proof["proof_policy"]["history_tooling_excluded_from_production_scan"] is True


def test_history_allowlist_is_explicit_and_path_scoped() -> None:
    proof = build_deletion_proof(PROJECT_ROOT)
    allowlist = proof["history_allowlist"]

    assert "scripts/graph_only_migration/" in allowlist
    assert "tests/fixtures/graph_only_migration/" in allowlist
    assert all("*" not in path for path in allowlist)


def test_symbol_proof_and_non_workflow_migration_boundaries_are_explicit() -> None:
    proof = build_deletion_proof(PROJECT_ROOT)

    for group_name, group in proof["symbol_deletion_proof"].items():
        if group["status"] == "retained_graph_owner":
            assert all(
                owner["definition_present"]
                for owner in group["owners"].values()
            ), group_name
            continue
        assert group["status"] == "deleted"
        assert all(
            symbol["status"] == "deleted"
            and symbol["tracked_definition_or_export_hits"] == 0
            and symbol["hits"] == []
            for symbol in group["symbols"].values()
        ), group_name

    boundaries = proof["non_workflow_migration_boundaries"]
    assert boundaries
    assert all(item["is_valid"] for item in boundaries.values())
    assert all(
        item["legacy_workflow_authority"] is False
        and item["graph_history_authority"] is False
        and item["pointer_or_dual_store_writer"] is False
        for item in boundaries.values()
    )


def test_source_scan_uses_effective_tracked_worktree_and_preserves_syntax_failures(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    paths = (
        "backend/research/unstaged_deleted.py",
        "backend/research/staged_deleted.py",
        "backend/research/syntax_error.py",
    )
    for relative in paths:
        path = project_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def valid(): pass\n", encoding="utf-8")
    (project_root / paths[2]).write_text("def broken(:\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

    git("init", "--quiet")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("add", *paths)
    git("commit", "--quiet", "-m", "tracked sources")

    (project_root / paths[0]).unlink()
    (project_root / paths[1]).unlink()
    git("add", "--", paths[1])

    tracked = _tracked_files(project_root)
    sources = _tracked_source_files(project_root, tracked)

    assert paths[0] in tracked  # Unstaged deletion remains in the index.
    assert paths[1] not in tracked  # Staged deletion is absent from the index.
    assert paths[0] not in sources
    assert paths[1] not in sources
    assert paths[2] in sources

    references, allowlisted = _scan_sources(project_root, sources)
    assert allowlisted == []
    assert [item.to_dict() for item in references] == [
        {
            "path": paths[2],
            "line": 0,
            "category": "parse_failure",
            "value": paths[2],
        }
    ]
