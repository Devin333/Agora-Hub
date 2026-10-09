from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_FILES = (
    ROOT / "framework/agent/loop/prompt.py",
    ROOT / "framework/agent/loop/loop.py",
    ROOT / "framework/agent/loop/runner.py",
    ROOT / "framework/harness/subagents/agent_runner.py",
)


def test_generic_prepared_input_framework_has_no_research_imports() -> None:
    for path in FRAMEWORK_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not any(name.startswith("backend.research") for name in imports), path


RESEARCH_INPUT_FILES = (
    ROOT / "backend/research/domain/input_contract.py",
    ROOT / "backend/research/application/research_input_contract.py",
    ROOT / "backend/research/application/single_paper_runtime.py",
    ROOT / "backend/research/application/llm_client.py",
)


def test_research_input_contract_does_not_depend_on_interface_infrastructure_or_legacy() -> None:
    forbidden = ("interfaces", "infrastructure", "backend.boards.paper_radar")
    for path in RESEARCH_INPUT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ]
        assert not any(name == prefix or name.startswith(prefix + ".") for name in imports for prefix in forbidden), path
