"""Which functions of the tool implementations the tool tests never run.

A pytest plugin built on ``sys.monitoring`` (PEP 669). It records the first
start of every function defined in the watched source trees and, at the end
of the session, writes the functions that never started. Each function is
counted once and its event is then disabled, so the cost is one callback per
function, not per call.

The watched trees are the tool implementations and the code they call to
reach the outside world (egress, fetch policy, connectors, notification,
storage adapters the tools write through). The report is a worklist: every
entry is a code path no tool test exercises, to be either tested or shown to
be unreachable and removed.

Enable with ``pytest tests/tools -p tests.tools.harness.function_coverage
--tool-function-coverage=<report.json>`` or ``python -m scripts.dev
tool-stability --function-coverage <report.json>``.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import types
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Relative to the repository root. The first group defines the handlers of
# every registered tool (a contract test keeps it complete); the second is the
# code those handlers go through to reach the outside world. Builders,
# models and connectors no tool calls stay out, so a gap here is a tool gap.
TOOL_HANDLER_SOURCES = (
    "backend/layers/analysis/tools.py",
    "backend/layers/output/postgres_tools.py",
    "backend/layers/output/tools.py",
    "backend/layers/signal/connector_tools.py",
    "backend/layers/signal/tools.py",
    "framework/tool/builtin/artifact.py",
    "framework/tool/builtin/control.py",
    "framework/tool/builtin/memory.py",
    "infrastructure/tools/local_json_tools.py",
    "infrastructure/tools/notification_tools.py",
    "infrastructure/tools/qdrant_tools.py",
    "infrastructure/tools/web_search_tools.py",
)
TOOL_RUNTIME_SOURCES = (
    "framework/tool/runtime/errors.py",
    "framework/tool/runtime/executor.py",
    "framework/tool/runtime/batch_executor.py",
    "framework/tool/runtime/timeout.py",
    "infrastructure/external/egress.py",
    "infrastructure/external/fetch_deadline.py",
    "infrastructure/external/sources/arxiv.py",
    "infrastructure/external/sources/github.py",
    "infrastructure/external/sources/feed.py",
    "infrastructure/external/sources/html.py",
    "infrastructure/external/sources/fetch_policy.py",
    "infrastructure/storage/postgres/connect.py",
    "infrastructure/storage/postgres/transaction.py",
    "infrastructure/storage/vector/connection.py",
)
WATCHED_SOURCES = TOOL_HANDLER_SOURCES + TOOL_RUNTIME_SOURCES

_TOOL_ID_NAME = "newsroom-tool-function-coverage"
# sys.monitoring (PEP 669) arrived in Python 3.12; the project also runs on 3.11.
MONITORING_AVAILABLE = hasattr(sys, "monitoring")
MONITORING_UNAVAILABLE_REASON = "tool function coverage needs sys.monitoring (Python 3.12+)"


@dataclass(frozen=True, order=True)
class FunctionSite:
    path: str
    line: int
    qualname: str


def watched_files(root: Path = REPO_ROOT, sources: tuple[str, ...] = WATCHED_SOURCES) -> list[Path]:
    files: set[Path] = set()
    for source in sources:
        path = root / source
        if path.is_dir():
            files.update(candidate for candidate in path.rglob("*.py") if "__pycache__" not in candidate.parts)
        elif path.is_file():
            files.add(path)
        else:
            raise FileNotFoundError(f"watched source does not exist: {source}")
    return sorted(files)


def defined_functions(path: Path, root: Path = REPO_ROOT) -> dict[tuple[str, int], FunctionSite]:
    """Every function and method in ``path`` that has a body to run.

    Keyed by (absolute filename, first line), which is what a running code
    object reports. Left out: class bodies (they run on import, they are not
    behaviour), lambdas and comprehensions (named ``<...>``; their lines
    belong to the enclosing function), and declarations with nothing to run
    (a Protocol or abstract method whose body is only ``...``, ``pass``, a
    docstring or ``raise NotImplementedError``).
    """

    source = path.read_text(encoding="utf-8")
    declarations = _declaration_only_lines(ast.parse(source, filename=str(path)))
    code = compile(source, str(path), "exec")
    relative = path.relative_to(root).as_posix()
    sites: dict[tuple[str, int], FunctionSite] = {}
    pending = [code]
    while pending:
        current = pending.pop()
        for constant in current.co_consts:
            if not isinstance(constant, types.CodeType):
                continue
            pending.append(constant)
            if constant.co_name.startswith("<") and constant.co_name.endswith(">"):
                continue
            if not constant.co_flags & inspect.CO_OPTIMIZED:
                continue
            if constant.co_firstlineno in declarations:
                continue
            sites[(str(path), constant.co_firstlineno)] = FunctionSite(relative, constant.co_firstlineno, constant.co_qualname)
    return sites


def _declaration_only_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and all(_is_placeholder(stmt) for stmt in node.body):
            # A code object's first line is its first decorator's, if any.
            lines.add(min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]))
    return lines


def _is_placeholder(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return statement.value.value is Ellipsis or isinstance(statement.value.value, str)
    if isinstance(statement, ast.Raise) and statement.exc is not None:
        target = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
        return isinstance(target, ast.Name) and target.id == "NotImplementedError"
    return False


class FunctionCoverage:
    def __init__(self, files: list[Path], *, root: Path = REPO_ROOT) -> None:
        self._sites: dict[tuple[str, int], FunctionSite] = {}
        for path in files:
            self._sites.update(defined_functions(path, root=root))
        self._watched_files = {str(path) for path in files}
        self._started: set[tuple[str, int]] = set()
        self._tool_id: int | None = None

    def start(self) -> None:
        if not MONITORING_AVAILABLE:
            raise RuntimeError(f"{MONITORING_UNAVAILABLE_REASON}; running Python {sys.version.split()[0]}")
        monitoring = sys.monitoring
        for tool_id in range(monitoring.DEBUGGER_ID + 1, 6):
            if monitoring.get_tool(tool_id) is None:
                break
        else:
            raise RuntimeError("no free sys.monitoring tool id")
        monitoring.use_tool_id(tool_id, _TOOL_ID_NAME)
        monitoring.register_callback(tool_id, monitoring.events.PY_START, self._on_start)
        monitoring.set_events(tool_id, monitoring.events.PY_START)
        self._tool_id = tool_id

    def stop(self) -> None:
        if self._tool_id is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self._tool_id, monitoring.events.NO_EVENTS)
        monitoring.register_callback(self._tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(self._tool_id)
        self._tool_id = None

    def _on_start(self, code: types.CodeType, instruction_offset: int) -> Any:
        if code.co_filename in self._watched_files:
            self._started.add((code.co_filename, code.co_firstlineno))
        return sys.monitoring.DISABLE

    def report(self) -> dict[str, Any]:
        never_run = sorted(site for key, site in self._sites.items() if key not in self._started)
        by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for site in never_run:
            by_file[site.path].append({"line": site.line, "function": site.qualname})
        total = len(self._sites)
        run = total - len(never_run)
        return {
            "functions": total,
            "run": run,
            "never_run": len(never_run),
            "run_ratio": round(run / total, 4) if total else 1.0,
            "never_run_by_file": dict(sorted(by_file.items())),
        }


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--tool-function-coverage",
        metavar="REPORT",
        default=None,
        help="write the tool-implementation functions no test ran to this JSON file",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, parser: pytest.Parser, args: list[str]) -> None:
    # Before any conftest imports the tool modules: a function a module runs
    # while it is imported counts too.
    report_path = getattr(early_config.known_args_namespace, "tool_function_coverage", None)
    if not report_path:
        return
    if not MONITORING_AVAILABLE:
        raise pytest.UsageError(f"--tool-function-coverage: {MONITORING_UNAVAILABLE_REASON}")
    coverage = FunctionCoverage(watched_files())
    coverage.start()
    early_config.stash[_COVERAGE_KEY] = (coverage, Path(report_path))


def pytest_unconfigure(config: pytest.Config) -> None:
    entry = config.stash.get(_COVERAGE_KEY, None)
    if entry is None:
        return
    coverage, report_path = entry
    coverage.stop()
    report = coverage.report()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"\ntool function coverage: {report['run']}/{report['functions']} functions ran "
        f"({report['never_run']} never ran); report: {report_path}\n"
    )


_COVERAGE_KEY = pytest.StashKey[tuple[FunctionCoverage, Path]]()
