from __future__ import annotations

import ast
from pathlib import Path
import tomllib

import scripts.dev as dev


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIVE_RESEARCH_TESTS = (
    "tests/backend/research/document/test_arxiv_latex_integration.py",
    "tests/backend/research/integration/test_chunk_paper_e2e.py",
    "tests/interfaces/composition/test_research_live_e2e.py",
)


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _has_module_live_marker(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ):
            continue
        if any(
            _qualified_name(candidate) == "pytest.mark.live_research_e2e"
            for candidate in ast.walk(value)
        ):
            return True
    return False


def test_ordinary_pytest_excludes_every_live_research_module() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    markers = config["tool"]["pytest"]["ini_options"]["markers"]

    assert "-m" in addopts
    marker_expression = addopts[addopts.index("-m") + 1]
    excluded = {term.strip() for term in marker_expression.split(" and ")}
    assert "not live_research_e2e" in excluded
    assert any(
        marker.partition(":")[0].strip() == "live_research_e2e"
        for marker in markers
    )
    assert [
        path
        for path in LIVE_RESEARCH_TESTS
        if not _has_module_live_marker(PROJECT_ROOT / path)
    ] == []


def test_live_research_command_explicitly_selects_all_live_modules() -> None:
    command = dev._rag_live_e2e_command()

    argument_pairs = set(zip(command, command[1:]))
    assert ("-m", "live_research_e2e") in argument_pairs
    assert ("-m", "not live_research_e2e") not in argument_pairs
    assert [path for path in LIVE_RESEARCH_TESTS if path not in command] == []
