from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

FRONTEND_SOURCE_PATHS = (
    "frontend/src/features/news/lib/news-filters.ts",
    "frontend/src/features/news/lib/__tests__/news-filters.test.ts",
    "frontend/src/features/studio/artifacts/lib/artifact-adapter.ts",
    "frontend/src/features/studio/boards/lib/board-adapter.ts",
    "frontend/src/features/studio/boards/lib/board-fallback-data.ts",
    "frontend/src/features/studio/evidence/lib/evidence-adapter.ts",
    "frontend/src/features/studio/quality/lib/quality-gate-adapter.test.ts",
    "frontend/src/features/studio/quality/lib/quality-gate-adapter.ts",
    "frontend/src/features/studio/runs/lib/agent-run-adapter.ts",
    "frontend/src/features/studio/runs/lib/mock-agent-runs.ts",
    "frontend/src/features/studio/runs/lib/run-center-adapter.ts",
    "frontend/src/features/studio/runs/lib/run-format.ts",
    "frontend/src/features/studio/runs/lib/run-test-utils.ts",
    "frontend/src/features/studio/runs/lib/workflow-layout.ts",
    "frontend/src/features/studio/shared/lib/studio-navigation.ts",
)

EXPECTED_IGNORED_PATHS = (
    "lib/python-package.py",
    "backend/lib/python-package.py",
    "lib64/python-package.py",
    "build/package.whl",
    "venv/pyvenv.cfg",
    ".tmp/cache/result.json",
    "frontend/node_modules/package/index.js",
)


def _git_check_ignore(repository: Path, relative_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative_path],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def _fresh_repository_with_production_ignore(tmp_path: Path) -> Path:
    repository = tmp_path / "source-tracking-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copy2(PROJECT_ROOT / ".gitignore", repository / ".gitignore")

    for relative_path in (*FRONTEND_SOURCE_PATHS, *EXPECTED_IGNORED_PATHS):
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    return repository


def test_production_gitignore_tracks_required_frontend_feature_libraries(tmp_path: Path) -> None:
    repository = _fresh_repository_with_production_ignore(tmp_path)

    unexpectedly_ignored = {
        path: result.stderr.strip() or f"git check-ignore exited {result.returncode}"
        for path in FRONTEND_SOURCE_PATHS
        if (result := _git_check_ignore(repository, path)).returncode != 1
    }

    assert unexpectedly_ignored == {}


def test_production_gitignore_keeps_generated_and_python_library_paths_ignored(tmp_path: Path) -> None:
    repository = _fresh_repository_with_production_ignore(tmp_path)

    unexpectedly_tracked = {
        path: result.stderr.strip()
        for path in EXPECTED_IGNORED_PATHS
        if (result := _git_check_ignore(repository, path)).returncode != 0
    }

    assert unexpectedly_tracked == {}


def test_frontend_source_tracking_contract_names_existing_assets() -> None:
    missing_assets = [path for path in FRONTEND_SOURCE_PATHS if not (PROJECT_ROOT / path).is_file()]

    assert missing_assets == []
