from __future__ import annotations

import textwrap
import types
from pathlib import Path

import pytest

from tests.tools.harness.function_coverage import (
    MONITORING_AVAILABLE,
    MONITORING_UNAVAILABLE_REASON,
    REPO_ROOT,
    TOOL_HANDLER_SOURCES,
    FunctionCoverage,
    defined_functions,
    watched_files,
)
from tests.tools.harness.registry import ToolEnvironment

needs_monitoring = pytest.mark.skipif(not MONITORING_AVAILABLE, reason=MONITORING_UNAVAILABLE_REASON)


def _executor_file(executor: object) -> str:
    code = getattr(executor, "__code__", None) or getattr(getattr(type(executor), "__call__", None), "__code__", None)
    assert isinstance(code, types.CodeType), f"cannot locate the code of {executor!r}"
    return Path(code.co_filename).resolve().relative_to(REPO_ROOT).as_posix()


def test_every_registered_tool_handler_is_watched(tool_env: ToolEnvironment) -> None:
    handler_files = {_executor_file(registered.executor) for registered in tool_env.registry.list_registered_tools()}

    assert handler_files <= set(TOOL_HANDLER_SOURCES), sorted(handler_files - set(TOOL_HANDLER_SOURCES))


def test_watched_sources_all_exist() -> None:
    assert len(watched_files()) >= len(TOOL_HANDLER_SOURCES)


def _module(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "pkg" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_defined_functions_are_the_bodies_that_can_run(tmp_path: Path) -> None:
    path = _module(
        tmp_path,
        """
        from typing import Protocol

        def outer():
            def inner():
                return 1
            return inner

        class Box:
            def method(self):
                return [x for x in range(2)]

            @staticmethod
            def decorated():
                return 2

        class Store(Protocol):
            def save(self, record): ...

            def load(self):
                \"\"\"Declared only.\"\"\"

            def todo(self):
                raise NotImplementedError

        handler = lambda args: args
        """,
    )

    names = {site.qualname for site in defined_functions(path, root=tmp_path).values()}

    assert names == {"outer", "outer.<locals>.inner", "Box.method", "Box.decorated"}


@needs_monitoring
def test_only_functions_that_started_are_counted_as_run(tmp_path: Path) -> None:
    path = _module(
        tmp_path,
        """
        def called():
            return "yes"

        def never_called():
            return "no"
        """,
    )
    coverage = FunctionCoverage([path], root=tmp_path)
    namespace: dict[str, object] = {}

    coverage.start()
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
        namespace["called"]()  # type: ignore[operator]
        namespace["called"]()  # type: ignore[operator]
    finally:
        coverage.stop()

    report = coverage.report()
    assert (report["functions"], report["run"], report["never_run"]) == (2, 1, 1)
    [entry] = report["never_run_by_file"].values()
    assert [item["function"] for item in entry] == ["never_called"]


@needs_monitoring
def test_coverage_releases_its_monitoring_tool_id(tmp_path: Path) -> None:
    import sys

    coverage = FunctionCoverage([_module(tmp_path, "def f():\n    return 1\n")], root=tmp_path)
    before = [sys.monitoring.get_tool(tool_id) for tool_id in range(6)]

    coverage.start()
    coverage.stop()

    assert [sys.monitoring.get_tool(tool_id) for tool_id in range(6)] == before


@pytest.mark.skipif(MONITORING_AVAILABLE, reason="only meaningful where sys.monitoring is missing")
def test_without_monitoring_coverage_refuses_to_start_instead_of_reporting_nothing(tmp_path: Path) -> None:
    coverage = FunctionCoverage([_module(tmp_path, "def f():\n    return 1\n")], root=tmp_path)

    with pytest.raises(RuntimeError, match=r"Python 3\.12\+"):
        coverage.start()


def test_missing_watched_source_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does-not-exist.py"):
        watched_files(root=tmp_path, sources=("does-not-exist.py",))
