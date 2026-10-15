from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from framework.shared.windows_paths import storage_io_path
from interfaces.composition.personal_memory import (
    PERSONAL_MEMORY_PATH_ENV,
    build_personal_memory_runtime,
    personal_memory_database_path_from_env,
)
from interfaces.composition.research_errors import ResearchConfigurationError
from interfaces.composition.research_settings import research_storage_paths_from_env
from interfaces.services.research_desktop_service import ResearchDesktopOwner


def test_personal_memory_composition_uses_explicit_absolute_path_and_persists(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "nested" / "personal-memory.sqlite3").resolve()
    env = {PERSONAL_MEMORY_PATH_ENV: str(database_path)}

    runtime = build_personal_memory_runtime(env=env)
    owner = ResearchDesktopOwner(user_id="user-a")
    created = runtime.service.create_confirmed(
        owner,
        content="I prefer concise evidence summaries.",
        category="reading_preference",
        source_refs=[
            {
                "source_type": "user_input",
                "source_ref": "user://settings/memory",
            }
        ],
        request_id="composition-create-1",
    )["record"]
    runtime.close()

    reopened = build_personal_memory_runtime(env=env)
    try:
        loaded = reopened.service.get(owner, created["memory_id"])["record"]
        assert loaded["content"] == "I prefer concise evidence summaries."
        policy = reopened.service.get_policy(owner)["policy"]
        assert policy["allow_remote_processing"] is False
        assert policy["revision"] == 1
        snapshot = reopened.service.select_active(
            owner,
            memory_refs=[
                {"memory_id": created["memory_id"], "revision": created["revision"]}
            ],
        )
        assert snapshot["used_memory_refs"] == [
            {"memory_id": created["memory_id"], "revision": created["revision"]}
        ]
        assert reopened.service.rebuild_recall_index(owner)["indexed_records"] == 1
        assert reopened.store.database_path == database_path
        assert database_path.is_file()
    finally:
        reopened.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path contract")
@pytest.mark.parametrize(
    "explicit",
    [False, True],
    ids=["research-root", "explicit-path"],
)
def test_personal_memory_composition_persists_under_long_database_root(
    tmp_path: Path,
    explicit: bool,
) -> None:
    research_root = _long_windows_research_root(tmp_path)
    database_path = research_root / "personal-memory.sqlite3"
    assert len(os.fspath(database_path)) > 260
    assert not os.fspath(database_path).startswith("\\\\?\\")
    env = (
        {PERSONAL_MEMORY_PATH_ENV: os.fspath(database_path)}
        if explicit
        else {"NEWS_RESEARCH_ROOT": os.fspath(research_root)}
    )
    assert personal_memory_database_path_from_env(env, cwd=tmp_path) == database_path
    owner = ResearchDesktopOwner(user_id="long-root-user")

    runtime = build_personal_memory_runtime(env=env, cwd=tmp_path)
    try:
        created = runtime.service.create_confirmed(
            owner,
            content="Keep long-path personal memory durable.",
            category="reading_preference",
            source_refs=[
                {
                    "source_type": "user_input",
                    "source_ref": "user://settings/long-path-memory",
                }
            ],
            request_id="composition-long-root-create-1",
        )["record"]
        assert runtime.store.database_path == database_path
        assert not os.fspath(runtime.store.database_path).startswith("\\\\?\\")
    finally:
        runtime.close()

    reopened = build_personal_memory_runtime(env=env, cwd=tmp_path)
    try:
        loaded = reopened.service.get(owner, created["memory_id"])["record"]
        assert loaded["content"] == "Keep long-path personal memory durable."
        assert loaded["revision"] == created["revision"]
        assert reopened.store.database_path == database_path
        assert storage_io_path(database_path).is_file()
    finally:
        reopened.close()


def test_personal_memory_composition_defaults_under_research_root(tmp_path: Path) -> None:
    resolved = personal_memory_database_path_from_env({}, cwd=tmp_path)
    assert resolved == (
        tmp_path / ".newsroom" / "research" / "personal-memory.sqlite3"
    ).resolve()

    runtime = build_personal_memory_runtime(env={}, cwd=tmp_path)
    try:
        assert runtime.store.database_path == resolved
        assert resolved.parent.is_dir()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "configured",
    [
        {"NEWS_ARTIFACT_ROOT": "artifacts/runs"},
        {"NEWS_RESEARCH_ARTIFACT_ROOT": "artifacts/runs"},
        {
            "NEWS_ARTIFACT_ROOT": "ignored/runs",
            "NEWS_RESEARCH_ARTIFACT_ROOT": "preferred/runs",
        },
        {
            "NEWS_ARTIFACT_ROOT": "artifacts/runs",
            "NEWS_RESEARCH_ARTIFACT_ROOT": "  ",
        },
        {"NEWS_ARTIFACT_ROOT": "artifacts/runs/.."},
    ],
    ids=["artifact", "research-artifact", "precedence", "blank-fallback", "dotdot"],
)
def test_personal_memory_artifact_fallback_matches_storage_settings(
    tmp_path: Path,
    configured: dict[str, str],
) -> None:
    expected = (
        research_storage_paths_from_env(configured, cwd=tmp_path).research_root
        / "personal-memory.sqlite3"
    )
    assert personal_memory_database_path_from_env(configured, cwd=tmp_path) == expected
    runtime = build_personal_memory_runtime(env=configured, cwd=tmp_path)
    try:
        assert runtime.store.database_path == expected
        assert storage_io_path(expected).is_file()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "setting",
    [
        "NEWS_RESEARCH_ARTIFACT_ROOT",
        "NEWS_RESEARCH_RUN_STORE_ROOT",
        "NEWS_RESEARCH_RAG_LOCAL_ROOT",
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT",
    ],
)
def test_personal_memory_preserves_storage_root_validation(
    tmp_path: Path,
    setting: str,
) -> None:
    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("unchanged", encoding="utf-8")
    env = {setting: os.fspath(invalid_root)}
    # Compare the existing helper's exact rejection, including its error type.
    with pytest.raises((ResearchConfigurationError, ValueError)) as expected:
        research_storage_paths_from_env(env, cwd=tmp_path)
    with pytest.raises(type(expected.value)) as actual:
        build_personal_memory_runtime(env=env, cwd=tmp_path)
    assert type(actual.value) is type(expected.value)
    assert str(actual.value) == str(expected.value)
    assert invalid_root.read_text(encoding="utf-8") == "unchanged"
    assert not (tmp_path / ".newsroom").exists()


def test_personal_memory_composition_rejects_relative_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        personal_memory_database_path_from_env(
            {PERSONAL_MEMORY_PATH_ENV: "relative/personal-memory.sqlite3"},
            cwd=tmp_path,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_personal_memory_composition_rejects_explicit_path_through_junction_without_writes(
    tmp_path: Path,
) -> None:
    target, junction = _windows_junction(tmp_path, "explicit")
    sentinel = target / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    database_path = junction / "new-memory-dir" / "personal-memory.sqlite3"

    try:
        with pytest.raises(ValueError, match="symlinks or reparse points"):
            build_personal_memory_runtime(
                env={PERSONAL_MEMORY_PATH_ENV: str(database_path)}
            )
    finally:
        os.rmdir(junction)

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(item.name for item in target.iterdir()) == ["sentinel.txt"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_personal_memory_composition_rejects_default_research_root_through_junction_without_writes(
    tmp_path: Path,
) -> None:
    target, junction = _windows_junction(tmp_path, "default")
    sentinel = target / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    research_root = junction / "new-research-root"

    try:
        with pytest.raises(ValueError, match="symlinks or reparse points"):
            build_personal_memory_runtime(
                env={"NEWS_RESEARCH_ROOT": str(research_root)},
                cwd=tmp_path,
            )
    finally:
        os.rmdir(junction)

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(item.name for item in target.iterdir()) == ["sentinel.txt"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize(
    "explicit",
    [False, True],
    ids=["research-root", "explicit-path"],
)
def test_personal_memory_rejects_long_ancestor_junction_without_writes(
    tmp_path: Path,
    explicit: bool,
) -> None:
    long_root = _long_windows_research_root(tmp_path) / "long-junction-parent"
    assert len(os.fspath(long_root)) > 260
    storage_io_path(long_root).mkdir(parents=True)
    target, junction = _windows_junction(long_root, "long")
    sentinel = storage_io_path(target / "sentinel.txt")
    sentinel.write_text("unchanged", encoding="utf-8")
    database_path = junction / "new-memory-dir" / "personal-memory.sqlite3"
    env = (
        {PERSONAL_MEMORY_PATH_ENV: os.fspath(database_path)}
        if explicit
        else {"NEWS_RESEARCH_ROOT": os.fspath(database_path.parent)}
    )
    try:
        with pytest.raises(ValueError, match="symlinks or reparse points"):
            build_personal_memory_runtime(env=env, cwd=tmp_path)
    finally:
        os.rmdir(storage_io_path(junction))
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(item.name for item in storage_io_path(target).iterdir()) == [
        "sentinel.txt"
    ]


def _long_windows_research_root(tmp_path: Path) -> Path:
    research_root = tmp_path / "data" / "imports"
    segment = "personal-memory-data-import-" + ("x" * 32)
    while len(os.fspath(research_root / "personal-memory.sqlite3")) <= 300:
        research_root /= segment
    return research_root


def _windows_junction(tmp_path: Path, suffix: str) -> tuple[Path, Path]:
    target = tmp_path / f"junction-target-{suffix}"
    storage_io_path(target).mkdir()
    junction = tmp_path / f"junction-{suffix}"
    result = subprocess.run(
        [
            "cmd",
            "/c",
            "mklink",
            "/J",
            str(storage_io_path(junction)),
            str(storage_io_path(target)),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return target, junction
