from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import PROJECT_ROOT


_PORT_MODULE = "backend.research.ports.qasper_evaluation"
_PORT_PATH = PROJECT_ROOT / "backend" / "research" / "ports" / "qasper_evaluation.py"
_ADAPTER_PATH = PROJECT_ROOT / "infrastructure" / "research" / "qasper_evaluation.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _annotations(tree: ast.AST) -> list[ast.expr]:
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.extend(
                argument.annotation
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                if argument.annotation is not None
            )
            if node.args.vararg is not None and node.args.vararg.annotation is not None:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
                annotations.append(node.args.kwarg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
    return annotations


def test_qasper_storage_adapter_uses_only_its_storage_facing_port() -> None:
    research_imports = {
        module for module in _imported_modules(_ADAPTER_PATH) if module.startswith("backend.research")
    }

    assert research_imports == {_PORT_MODULE}


def test_qasper_storage_port_exposes_only_transport_and_stdlib_types() -> None:
    tree = _tree(_PORT_PATH)
    assert _imported_modules(_PORT_PATH) == {"__future__", "dataclasses", "typing"}

    allowed_annotation_names = {
        "BinaryIO",
        "ContextManager",
        "Mapping",
        "QasperRawCodeChange",
        "QasperRawCodeIdentity",
        "QasperRawSource",
        "Sequence",
        "bytes",
        "str",
        "tuple",
    }
    annotation_names = {
        node.id
        for annotation in _annotations(tree)
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name)
    }

    assert annotation_names <= allowed_annotation_names
    assert not any(module == "numpy" or module.startswith("numpy.") for module in _imported_modules(_PORT_PATH))
