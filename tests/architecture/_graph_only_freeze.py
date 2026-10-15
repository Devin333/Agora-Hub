from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


BASELINE_SCHEMA = "newsroom.graph-only-freeze-baseline/v1"
DEFAULT_PRODUCTION_ROOTS = (
    "backend",
    "framework",
    "infrastructure",
    "interfaces",
    "scripts",
)
# Audit tools whose forbidden symbol tables intentionally contain retired names
# are not runtime callers or exports. Keep this allowlist exact and path scoped.
NON_RUNTIME_AUDIT_FILES = frozenset(
    {
        "scripts/graph_only_migration/deletion_proof.py",
        "scripts/graph_only_migration/zero_reference_scan.py",
    }
)
FORBIDDEN_NAMESPACE_PREFIXES = (
    "framework.workflow",
    "framework.harness.workflow",
)
HARNESS_WORKFLOW_FACADE_OWNER_MODULES = {
    "HarnessWorkflowGraphCompiler": "framework.harness.workflow.compiler",
    "HarnessWorkflowSpec": "framework.harness.workflow.spec",
}
CATEGORIES = (
    "namespace_files",
    "import_edges",
    "retired_symbols",
    "public_exports",
    "legacy_schema_writers",
)
LEGACY_SCHEMA_IDS = frozenset(
    {
        "newsroom.workflow_run_manifest.v1",
        "newsroom.workflow.event.v1",
        "newsroom.workflow-event/v1",
        "newsroom.workflow-operation/v1",
        "newsroom.workflow-event-projection/v1",
        "workflow-checkpoint/v0",
        "workflow-checkpoint/v1",
        "workflow-checkpoint/v2",
        "newsroom.harness-workflow-legacy/v1",
        "newsroom.harness-state-legacy/v1",
        "newsroom.harness-decision-legacy/v1",
        "newsroom.harness-checkpoint-legacy/v1",
        "newsroom.harness-event/v1",
    }
)
SCHEMA_SINK_FIELDS = frozenset(
    {"schema", "schema_id", "schema_version", "data_schema", "dataschema", "projection_schema"}
)
EXPLICIT_RETIRED_SYMBOLS = frozenset(
    {
        "AgentLoopStepRunner",
        "ArtifactStepRunner",
        "FunctionStepRunner",
        "HarnessWorkflowGraphCompiler",
        "HarnessWorkflowSpec",
        "HumanReviewStepRunner",
        "JoinStepRunner",
        "MemoryConsolidateStepRunner",
        "MemoryRecallStepRunner",
        "MemoryWriteStepRunner",
        "ParallelGroupStepRunner",
        "QualityGateStepRunner",
        "RouterStepRunner",
        "RunResult",
        "SkillStepRunner",
        "StepRunner",
        "StepRunnerRegistry",
        "SubworkflowStepRunner",
        "ToolBatchStepRunner",
        "ToolCallStepRunner",
        "WorkflowExecutor",
        "WorkflowRunner",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class FreezeBaselineError(ValueError):
    pass


@dataclass(frozen=True)
class ScanResult:
    active: dict[str, dict[str, int]]
    locations: dict[str, dict[str, int]]
    parse_failures: tuple[tuple[str, int, str], ...]


def scan_tree(
    project_root: Path,
    production_roots: Sequence[str] = DEFAULT_PRODUCTION_ROOTS,
) -> ScanResult:
    counters = {category: Counter[str]() for category in CATEGORIES}
    locations = {category: {} for category in CATEGORIES}
    parse_failures: list[tuple[str, int, str]] = []

    for path in _production_python_files(project_root, production_roots):
        relative_path = path.relative_to(project_root).as_posix()
        legacy_namespace_file = _legacy_namespace_for_path(relative_path)
        if legacy_namespace_file:
            _record(
                counters,
                locations,
                "namespace_files",
                relative_path,
                1,
            )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        except (SyntaxError, UnicodeError) as exc:
            parse_failures.append(
                (
                    relative_path,
                    int(getattr(exc, "lineno", 0) or 0),
                    str(exc),
                )
            )
            continue

        visitor = _FreezeVisitor(
            relative_path=relative_path,
            module_name=_module_name(relative_path),
            is_legacy_namespace=bool(legacy_namespace_file),
            legacy_schema_bindings=_legacy_schema_bindings(tree),
            counters=counters,
            locations=locations,
            docstrings=_docstring_node_ids(tree),
        )
        visitor.visit(tree)

    return ScanResult(
        active={
            category: dict(sorted(counters[category].items()))
            for category in CATEGORIES
        },
        locations=locations,
        parse_failures=tuple(sorted(parse_failures)),
    )


def capture_baseline(
    project_root: Path,
    *,
    source_commit: str,
    source_tree: str,
    production_roots: Sequence[str] = DEFAULT_PRODUCTION_ROOTS,
) -> dict[str, Any]:
    result = scan_tree(project_root, production_roots)
    if result.parse_failures:
        raise FreezeBaselineError(_format_parse_failures(result.parse_failures))
    return {
        "schema": BASELINE_SCHEMA,
        "captured_at": date.today().isoformat(),
        "source_commit": source_commit,
        "source_tree": source_tree,
        "generation": 1,
        "production_roots": list(production_roots),
        "forbidden_namespace_prefixes": list(FORBIDDEN_NAMESPACE_PREFIXES),
        "legacy_schema_ids": sorted(LEGACY_SCHEMA_IDS),
        "active": result.active,
        "retired": [],
        "migration_reader_exceptions": [],
    }


def update_baseline(
    previous: Mapping[str, Any],
    current: ScanResult,
    *,
    source_commit: str,
    source_tree: str,
) -> dict[str, Any]:
    validate_baseline(previous)
    if current.parse_failures:
        raise FreezeBaselineError(_format_parse_failures(current.parse_failures))

    retired = [dict(item) for item in previous["retired"]]
    for category in CATEGORIES:
        previous_rows = _comparison_rows(
            category,
            previous["active"][category],
        )
        current_rows = _comparison_rows(category, current.active[category])
        additions = {
            key: count
            for key, count in current_rows.items()
            if key not in previous_rows or count > previous_rows[key]
        }
        if additions:
            raise FreezeBaselineError(
                f"baseline update is not subtract-only for {category}: "
                f"{sorted(additions)}"
            )
        for key, previous_count in previous_rows.items():
            removed_count = previous_count - current_rows.get(key, 0)
            if removed_count > 0:
                retired.append(
                    {
                        "category": category,
                        "key": key,
                        "count": removed_count,
                        "generation": int(previous["generation"]) + 1,
                    }
                )

    updated = dict(previous)
    updated.update(
        {
            "captured_at": date.today().isoformat(),
            "source_commit": source_commit,
            "source_tree": source_tree,
            "generation": int(previous["generation"]) + 1,
            "active": current.active,
            "retired": retired,
        }
    )
    validate_baseline(updated)
    return updated


def verify_tree(project_root: Path, baseline: Mapping[str, Any]) -> list[str]:
    validate_baseline(baseline)
    result = scan_tree(project_root, tuple(baseline["production_roots"]))
    violations = [
        _diagnostic(
            "graph_only_freeze_source_parse_failed",
            path,
            "<module>",
            f"line={line}:{message}",
        )
        for path, line, message in result.parse_failures
    ]

    for category in CATEGORIES:
        expected = _comparison_rows(category, baseline["active"][category])
        actual = _comparison_rows(category, result.active[category])
        for key, count in actual.items():
            expected_count = expected.get(key)
            if expected_count is None or count > expected_count:
                violations.append(
                    _diagnostic_for_growth(
                        category,
                        key,
                        count,
                        expected,
                        result.locations[category].get(key, 0),
                    )
                )
        for key, expected_count in expected.items():
            actual_count = actual.get(key, 0)
            if actual_count < expected_count:
                path, scope = _path_and_scope(category, key)
                violations.append(
                    _diagnostic(
                        "graph_only_freeze_baseline_not_monotonic",
                        path,
                        scope,
                        f"fixture={expected_count},source={actual_count},key={key}",
                    )
                )
    return sorted(violations)


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FreezeBaselineError(f"cannot load freeze baseline {path}: {exc}") from exc
    validate_baseline(payload)
    return payload


def validate_baseline(payload: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "captured_at",
        "source_commit",
        "source_tree",
        "generation",
        "production_roots",
        "forbidden_namespace_prefixes",
        "legacy_schema_ids",
        "active",
        "retired",
        "migration_reader_exceptions",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise FreezeBaselineError(
            f"freeze baseline fields must be exactly {sorted(required)}"
        )
    if payload["schema"] != BASELINE_SCHEMA:
        raise FreezeBaselineError(f"unsupported freeze baseline schema: {payload['schema']!r}")
    for field in ("captured_at", "source_commit", "source_tree"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise FreezeBaselineError(f"freeze baseline {field} must be populated")
    if not re.fullmatch(r"[0-9a-f]{40}", payload["source_commit"]):
        raise FreezeBaselineError("freeze baseline source commit is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", payload["source_tree"]):
        raise FreezeBaselineError("freeze baseline source tree is invalid")
    if not isinstance(payload["generation"], int) or payload["generation"] < 1:
        raise FreezeBaselineError("freeze baseline generation must be a positive integer")
    if tuple(payload["production_roots"]) != DEFAULT_PRODUCTION_ROOTS:
        raise FreezeBaselineError("freeze baseline production roots are invalid")
    if tuple(payload["forbidden_namespace_prefixes"]) != FORBIDDEN_NAMESPACE_PREFIXES:
        raise FreezeBaselineError("freeze baseline namespace prefixes are invalid")
    if frozenset(payload["legacy_schema_ids"]) != LEGACY_SCHEMA_IDS:
        raise FreezeBaselineError("freeze baseline legacy schema ids are invalid")
    if not isinstance(payload["active"], Mapping) or set(payload["active"]) != set(CATEGORIES):
        raise FreezeBaselineError("freeze baseline categories are invalid")
    for category in CATEGORIES:
        rows = payload["active"][category]
        if not isinstance(rows, Mapping):
            raise FreezeBaselineError(f"freeze baseline {category} must be an object")
        for key, count in rows.items():
            if not isinstance(key, str) or not key:
                raise FreezeBaselineError(f"freeze baseline {category} has an invalid key")
            if not isinstance(count, int) or count < 1:
                raise FreezeBaselineError(
                    f"freeze baseline {category} count must be positive: {key}"
                )
    _validate_retired(payload["retired"], payload["generation"])
    _validate_migration_reader_exceptions(payload["migration_reader_exceptions"])


class _FreezeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        relative_path: str,
        module_name: str,
        is_legacy_namespace: bool,
        legacy_schema_bindings: frozenset[str],
        counters: dict[str, Counter[str]],
        locations: dict[str, dict[str, int]],
        docstrings: frozenset[int],
    ) -> None:
        self.relative_path = relative_path
        self.module_name = module_name
        self.is_legacy_namespace = is_legacy_namespace
        self.legacy_schema_bindings = legacy_schema_bindings
        self.counters = counters
        self.locations = locations
        self.docstrings = docstrings
        self.scope = ["<module>"]

    @property
    def lexical_scope(self) -> str:
        return ".".join(self.scope)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_symbols(node.name, "class_definition", node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._record_symbols(node.name, "function_definition", node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _matches_namespace(alias.name):
                symbol = "*" if alias.asname is None else f"* as {alias.asname}"
                self._record_import(alias.name, (symbol,), node)
            self._record_symbols(alias.name, "import", node)
            if alias.asname:
                self._record_symbols(alias.asname, "import_alias", node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolve_import_from(self.module_name, self.relative_path, node)
        symbols = tuple(
            sorted(
                alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
                for alias in node.names
            )
        )
        if module and _matches_namespace(module):
            self._record_import(module, symbols, node)
        for alias in node.names:
            self._record_symbols(alias.name, "import", node)
            if alias.asname:
                self._record_symbols(alias.asname, "import_alias", node)

    def visit_Name(self, node: ast.Name) -> None:
        self._record_symbols(node.id, f"name_{node.ctx.__class__.__name__.lower()}", node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._record_symbols(node.attr, "attribute", node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            self._record_symbols(node.value, "string_literal", node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_public_exports(target, node.value, node)
            field = _schema_sink_target(target)
            if field and _expr_mentions_legacy_schema(node.value, self.legacy_schema_bindings):
                self._record_schema_writer(f"assignment:{field}", node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_public_exports(node.target, node.value, node)
            field = _schema_sink_target(node.target)
            if field and _expr_mentions_legacy_schema(node.value, self.legacy_schema_bindings):
                self._record_schema_writer(f"annotated_assignment:{field}", node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_public_exports(node.target, node.value, node)
        field = _schema_sink_target(node.target)
        if field and _expr_mentions_legacy_schema(node.value, self.legacy_schema_bindings):
            self._record_schema_writer(f"augmented_assignment:{field}", node)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            field = _constant_string(key)
            if field in SCHEMA_SINK_FIELDS and _expr_mentions_legacy_schema(
                value, self.legacy_schema_bindings
            ):
                self._record_schema_writer(f"dict_field:{field}", node)
            # A lowerCamel boolean field describes data/policy, not a symbol
            # binding. Visit every value independently: a real reflection value
            # in the same dictionary must still fail the freeze gate.
            if key is not None and not (
                not self.is_legacy_namespace
                and _is_lower_camel_boolean_data_key(key, value)
            ):
                self.visit(key)
            self.visit(value)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg in SCHEMA_SINK_FIELDS and _expr_mentions_legacy_schema(
                keyword.value, self.legacy_schema_bindings
            ):
                self._record_schema_writer(f"call_keyword:{keyword.arg}", node)
        call_name = (_call_name(node.func) or "").lower()
        if _is_schema_writer_call(call_name) and any(
            _expr_mentions_legacy_schema(argument, self.legacy_schema_bindings)
            for argument in node.args
        ):
            self._record_schema_writer(f"call_argument:{call_name}", node)
        self.generic_visit(node)

    def _record_import(
        self,
        module: str,
        symbols: tuple[str, ...],
        node: ast.AST,
    ) -> None:
        key = "|".join(
            (self.relative_path, self.lexical_scope, module, ",".join(symbols))
        )
        _record(self.counters, self.locations, "import_edges", key, node.lineno)

    def _record_symbols(self, value: str, kind: str, node: ast.AST) -> None:
        for symbol in sorted(set(_IDENTIFIER.findall(value))):
            if not _is_retired_symbol(symbol, self.is_legacy_namespace):
                continue
            key = "|".join(
                (
                    self.relative_path,
                    self.lexical_scope,
                    kind,
                    symbol,
                )
            )
            _record(self.counters, self.locations, "retired_symbols", key, node.lineno)

    def _record_public_exports(
        self,
        target: ast.expr,
        value: ast.expr,
        node: ast.AST,
    ) -> None:
        if not isinstance(target, ast.Name) or target.id != "__all__":
            return
        for symbol in _literal_strings(value):
            if self.is_legacy_namespace or _is_retired_symbol(symbol, False):
                key = f"{self.relative_path}|{symbol}"
                _record(self.counters, self.locations, "public_exports", key, node.lineno)

    def _record_schema_writer(self, kind: str, node: ast.AST) -> None:
        key = "|".join(
            (self.relative_path, self.lexical_scope, kind)
        )
        _record(
            self.counters,
            self.locations,
            "legacy_schema_writers",
            key,
            node.lineno,
        )


def _production_python_files(
    project_root: Path,
    production_roots: Sequence[str],
) -> tuple[Path, ...]:
    return tuple(
        path
        for root_name in production_roots
        for path in sorted((project_root / root_name).rglob("*.py"))
        if path.is_file()
        and path.relative_to(project_root).as_posix() not in NON_RUNTIME_AUDIT_FILES
        and "__pycache__" not in path.parts
    )


def _legacy_namespace_for_path(relative_path: str) -> str | None:
    module = _module_name(relative_path)
    return next(
        (prefix for prefix in FORBIDDEN_NAMESPACE_PREFIXES if _matches_module(module, prefix)),
        None,
    )


def _module_name(relative_path: str) -> str:
    path = Path(relative_path).with_suffix("")
    parts = list(path.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import_from(
    module_name: str,
    relative_path: str,
    node: ast.ImportFrom,
) -> str | None:
    if node.level == 0:
        return node.module
    package_parts = module_name.split(".")
    if not relative_path.endswith("/__init__.py") and package_parts:
        package_parts.pop()
    remove_count = node.level - 1
    if remove_count > len(package_parts):
        return None
    if remove_count:
        package_parts = package_parts[:-remove_count]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def _matches_namespace(module: str) -> bool:
    return any(_matches_module(module, prefix) for prefix in FORBIDDEN_NAMESPACE_PREFIXES)


def _matches_module(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def _is_lower_camel_boolean_data_key(key: ast.expr, value: ast.expr) -> bool:
    """Exclude only literal boolean data keys, never reflection expressions."""
    field = _constant_string(key)
    return (
        field is not None
        and re.fullmatch(r"[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+", field) is not None
        and isinstance(value, ast.Constant)
        and type(value.value) is bool
        and field not in EXPLICIT_RETIRED_SYMBOLS
        and not field.startswith("LEGACY_")
        and not field.endswith(("Runner", "Executor"))
    )


def _is_retired_symbol(symbol: str, is_legacy_namespace: bool) -> bool:
    return (
        symbol in EXPLICIT_RETIRED_SYMBOLS
        or "Workflow" in symbol
        or symbol.startswith("LEGACY_")
        or is_legacy_namespace and symbol.endswith("StepRunner")
    )


def _legacy_schema_bindings(tree: ast.AST) -> frozenset[str]:
    bindings = {name for name in _all_identifiers(tree) if name.startswith("LEGACY_")}
    assignments: list[tuple[tuple[str, ...], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and _matches_namespace(node.module):
            for alias in node.names:
                if "SCHEMA" in alias.name:
                    bindings.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = tuple(
                name
                for target in targets
                for name in _assigned_names(target)
            )
            assignments.append((names, value))

    changed = True
    while changed:
        changed = False
        for names, value in assignments:
            if not names or not _expr_mentions_legacy_schema(value, frozenset(bindings)):
                continue
            for name in names:
                if name not in bindings:
                    bindings.add(name)
                    changed = True
    return frozenset(bindings)


def _expr_mentions_legacy_schema(node: ast.AST, bindings: frozenset[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value in LEGACY_SCHEMA_IDS:
                return True
        elif isinstance(child, ast.Name) and child.id in bindings:
            return True
        elif isinstance(child, ast.Attribute) and child.attr in bindings:
            return True
    return False


def _schema_sink_target(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name) and node.id in SCHEMA_SINK_FIELDS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in SCHEMA_SINK_FIELDS:
        return node.attr
    if isinstance(node, ast.Subscript):
        field = _constant_string(node.slice)
        if field in SCHEMA_SINK_FIELDS:
            return field
    return None


def _is_schema_writer_call(call_name: str) -> bool:
    return call_name.startswith(
        (
            "append",
            "build",
            "create",
            "emit",
            "publish",
            "put",
            "register",
            "save",
            "serialize",
            "update",
            "write",
        )
    )


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_strings(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _assigned_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for item in node.elts for name in _assigned_names(item))
    return ()


def _all_identifiers(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr


def _docstring_node_ids(tree: ast.AST) -> frozenset[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return frozenset(ids)


def _record(
    counters: dict[str, Counter[str]],
    locations: dict[str, dict[str, int]],
    category: str,
    key: str,
    line: int,
) -> None:
    counters[category][key] += 1
    locations[category].setdefault(key, line)


def _diagnostic_for_growth(
    category: str,
    key: str,
    count: int,
    expected: Mapping[str, int],
    line: int,
) -> str:
    path, scope = _path_and_scope(category, key)
    if category == "namespace_files":
        code = "graph_only_freeze_new_legacy_namespace_file"
    elif category == "import_edges":
        prefix = "|".join(key.split("|")[:3]) + "|"
        if "migration" in path.lower() or "migrator" in path.lower():
            code = "graph_only_freeze_unregistered_migration_reader"
        elif any(existing.startswith(prefix) for existing in expected):
            code = "graph_only_freeze_legacy_import_edge_expanded"
        else:
            code = "graph_only_freeze_new_legacy_import_edge"
    elif category == "public_exports":
        code = "graph_only_freeze_new_legacy_public_export"
    elif category == "legacy_schema_writers":
        code = "graph_only_freeze_new_legacy_schema_writer"
    elif "WorkflowRunner" in key:
        code = "graph_only_freeze_new_workflow_runner_symbol"
    elif "WorkflowExecutor" in key:
        code = "graph_only_freeze_new_workflow_executor_symbol"
    elif "string_literal" in key:
        code = "graph_only_freeze_new_legacy_reflection_binding"
    else:
        code = "graph_only_freeze_new_legacy_registry_binding"
    return _diagnostic(code, path, scope, f"line={line},count={count},key={key}")


def _comparison_rows(
    category: str,
    rows: Mapping[str, int],
) -> dict[str, int]:
    if category != "import_edges":
        return dict(rows)
    normalized: Counter[str] = Counter()
    for key, count in rows.items():
        parts = key.split("|", maxsplit=3)
        if len(parts) != 4 or not parts[3]:
            raise FreezeBaselineError(f"invalid import edge key: {key!r}")
        for symbol in parts[3].split(","):
            if not symbol:
                raise FreezeBaselineError(f"invalid import edge symbol: {key!r}")
            module = _comparison_import_module(parts[2], symbol)
            normalized["|".join((*parts[:2], module, symbol))] += count
    return dict(sorted(normalized.items()))


def _comparison_import_module(module: str, symbol: str) -> str:
    owner_module = HARNESS_WORKFLOW_FACADE_OWNER_MODULES.get(symbol)
    if owner_module is not None and module in {
        "framework.harness.workflow",
        owner_module,
    }:
        return "framework.harness.workflow"
    return module


def _path_and_scope(category: str, key: str) -> tuple[str, str]:
    parts = key.split("|")
    if category in {"namespace_files", "public_exports"}:
        return parts[0], "<module>"
    return parts[0], parts[1] if len(parts) > 1 else "<module>"


def _diagnostic(code: str, path: str, scope: str, detail: str) -> str:
    return f"{code}|path={path}|scope={scope}|{detail}"


def _format_parse_failures(failures: Sequence[tuple[str, int, str]]) -> str:
    return "; ".join(f"{path}:{line}:{message}" for path, line, message in failures)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FreezeBaselineError(f"duplicate JSON key in freeze baseline: {key}")
        result[key] = value
    return result


def _validate_retired(value: Any, generation: int) -> None:
    if not isinstance(value, list):
        raise FreezeBaselineError("freeze baseline retired rows must be an array")
    seen: set[tuple[str, str, int]] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {
            "category",
            "key",
            "count",
            "generation",
        }:
            raise FreezeBaselineError("freeze baseline retired row is invalid")
        if not isinstance(row["category"], str) or not isinstance(row["key"], str):
            raise FreezeBaselineError("retired freeze row identity is invalid")
        if not isinstance(row["generation"], int):
            raise FreezeBaselineError("retired freeze row generation is invalid")
        identity = (row["category"], row["key"], row["generation"])
        if identity in seen:
            raise FreezeBaselineError(f"duplicate retired freeze row: {identity}")
        seen.add(identity)
        if row["category"] not in CATEGORIES:
            raise FreezeBaselineError(f"invalid retired category: {row['category']}")
        if not isinstance(row["count"], int) or row["count"] < 1:
            raise FreezeBaselineError("retired freeze row count must be positive")
        if not isinstance(row["generation"], int) or not 2 <= row["generation"] <= generation:
            raise FreezeBaselineError("retired freeze row generation is invalid")


def _validate_migration_reader_exceptions(value: Any) -> None:
    if not isinstance(value, list):
        raise FreezeBaselineError("migration reader exceptions must be an array")
    required = {
        "exact_path",
        "owner",
        "legacy_schema_ids",
        "source_path_constraints",
        "input_checksum_policy",
        "zero_live_side_effect_test",
        "removal_task",
        "expiration_condition",
    }
    for row in value:
        if not isinstance(row, Mapping) or set(row) != required:
            raise FreezeBaselineError("migration reader exception fields are invalid")
        if any(field_value in (None, "", []) for field_value in row.values()):
            raise FreezeBaselineError("migration reader exception fields must be populated")
        exact_path = row["exact_path"]
        if not isinstance(exact_path, str) or any(token in exact_path for token in ("*", "?", "[")):
            raise FreezeBaselineError("migration reader exception path must be exact")
        schema_ids = row["legacy_schema_ids"]
        if not isinstance(schema_ids, list) or not set(schema_ids).issubset(LEGACY_SCHEMA_IDS):
            raise FreezeBaselineError("migration reader exception schema ids are invalid")


def _write_payload(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Graph-only subtract-only freeze baseline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "update"):
        child = subparsers.add_parser(command)
        child.add_argument("--project-root", type=Path, required=True)
        child.add_argument("--baseline", type=Path, required=True)
        child.add_argument("--source-commit", required=True)
        child.add_argument("--source-tree", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-root", type=Path, required=True)
    verify.add_argument("--baseline", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        if args.baseline.exists():
            raise FreezeBaselineError(f"refusing to replace existing baseline: {args.baseline}")
        payload = capture_baseline(
            args.project_root,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
        )
        _write_payload(args.baseline, payload)
        return 0
    if args.command == "update":
        previous = load_baseline(args.baseline)
        current = scan_tree(args.project_root, tuple(previous["production_roots"]))
        payload = update_baseline(
            previous,
            current,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
        )
        _write_payload(args.baseline, payload)
        return 0
    violations = verify_tree(args.project_root, load_baseline(args.baseline))
    if violations:
        sys.stderr.write("\n".join(violations) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
