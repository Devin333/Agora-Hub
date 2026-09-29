from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture._helpers import PROJECT_ROOT


PRODUCTION_ROOTS = ("backend", "framework", "infrastructure", "interfaces")

_EXECUTION_CONSTRUCTORS = {
    "ToolExecutor",
    "ToolBatchExecutor",
    "ResearchParserExecutionAdapter",
}
_EXECUTION_CONSTRUCTOR_CALLERS = {
    ("framework/agent/loop/runner.py", "ToolExecutor"),
    ("framework/execution_environment/composition.py", "ToolExecutor"),
    ("framework/harness/runtime/tool_result_adapter.py", "ToolExecutor"),
    ("framework/tool/inspection/testing.py", "ToolExecutor"),
    ("framework/tool/runtime/batch_executor.py", "ToolExecutor"),
    (
        "interfaces/composition/research.py",
        "ResearchParserExecutionAdapter",
    ),
}
_HARNESS_EXECUTION_CALLERS = _EXECUTION_CONSTRUCTOR_CALLERS - {
    ("framework/tool/inspection/testing.py", "ToolExecutor"),
}

_EXECUTION_DISPATCH_CALLERS = {
    ("framework/execution_environment/registry.py", "provider"),
    ("framework/tool/runtime/executor.py", "registry"),
    (
        "infrastructure/research/document_execution_adapter.py",
        "self._execution_environment",
    ),
}

_CHILD_RUNTIME_CONSTRUCTORS = {
    "ChildAgentSupervisor",
    "HarnessOwnedChildAgentRuntime",
    "SubAgentRuntime",
}
_CHILD_RUNTIME_CALLERS = {
    (
        "framework/harness/subagents/owned_runtime.py",
        "ChildAgentSupervisor",
    ),
    (
        "interfaces/composition/research.py",
        "SubAgentRuntime",
    ),
    (
        "interfaces/composition/research_child_runtime.py",
        "HarnessOwnedChildAgentRuntime",
    ),
}

_CHILD_LIFECYCLE_METHODS = {
    "spawn",
    "spawn_batch",
    "status",
    "wait",
    "cancel",
    "close",
    "heartbeat",
    "reclaim_stale",
    "recover",
    "start",
    "stop_admission",
    "cancel_active",
    "shutdown",
}
_CHILD_LIFECYCLE_MODULES = {
    "framework/harness/subagents/owned_runtime.py",
    "framework/harness/task_plan/parallel.py",
    "interfaces/composition/research_child_runtime.py",
}


def test_every_production_execution_constructor_is_inventory_bound() -> None:
    discovered = {
        (path, _call_name(call.func))
        for path, call in _production_calls()
        if _call_name(call.func) in _EXECUTION_CONSTRUCTORS
    }

    assert discovered == _EXECUTION_CONSTRUCTOR_CALLERS


def test_every_harness_managed_executor_receives_the_execution_port() -> None:
    violations: list[str] = []
    for path, call in _production_calls():
        constructor = _call_name(call.func)
        if (path, constructor) not in _HARNESS_EXECUTION_CALLERS:
            continue
        keywords = {keyword.arg for keyword in call.keywords}
        if constructor == "ToolExecutor" and path == (
            "framework/execution_environment/composition.py"
        ):
            if not any(keyword.arg is None for keyword in call.keywords):
                violations.append(f"{path}:{call.lineno} missing composition kwargs")
            continue
        if "execution_environment" not in keywords:
            violations.append(f"{path}:{call.lineno} missing execution_environment")

    assert violations == []


def test_physical_execution_dispatch_stays_inside_owned_ports() -> None:
    discovered: set[tuple[str, str]] = set()
    for path, call in _production_calls():
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "execute":
            continue
        receiver = ast.unparse(call.func.value)
        if receiver in {
            "provider",
            "registry",
            "self._execution_environment",
        }:
            discovered.add((path, receiver))

    assert discovered == _EXECUTION_DISPATCH_CALLERS


def test_child_runtime_construction_is_harness_owned_or_candidate_only() -> None:
    discovered = {
        (path, _call_name(call.func))
        for path, call in _production_calls()
        if _call_name(call.func) in _CHILD_RUNTIME_CONSTRUCTORS
    }

    assert discovered == _CHILD_RUNTIME_CALLERS

    research = PROJECT_ROOT / "interfaces" / "composition" / "research.py"
    tree = ast.parse(research.read_text(encoding="utf-8"), filename=str(research))
    factory = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "dynamic_task_plan_runner_factory"
    )
    stage = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call)
        and _call_name(node.func) == "ResearchAnalysisTaskPlanStageWorker"
    )
    bindings = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in stage.keywords
        if keyword.arg is not None
    }
    assert bindings["worker_executor"] == "task_plan_worker_executor"
    assert bindings["worker_result_recovery"] == (
        "task_plan_worker_executor.recover"
    )
    assert bindings["store"] == "dynamic_task_plan_store"
    assert bindings["policy"] == "policy"
    assert bindings["parallel_coordinator"] == (
        "child_runtime_binding.parallel_coordinator"
    )
    assert bindings["child_agent_supervisor"] == (
        "child_runtime_binding.supervisor"
    )

    assignments = {
        node.targets[0].id: node.value
        for node in factory.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    policy_call = assignments["policy"]
    assert isinstance(policy_call, ast.Call)
    assert _call_name(policy_call.func) == "build_research_analysis_task_plan_policy"

    executor_call = assignments["task_plan_worker_executor"]
    assert isinstance(executor_call, ast.Call)
    assert _call_name(executor_call.func) == "HarnessSubAgentTaskExecutor"
    executor_bindings = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in executor_call.keywords
        if keyword.arg is not None
    }
    assert executor_bindings == {
        "store": "dynamic_task_plan_store",
        "runtime": "subagent_runtime",
        "ref_admission_service": "dynamic_ref_admission_service",
        "task_policy": "policy",
    }

    runtime_call = assignments["subagent_runtime"]
    assert isinstance(runtime_call, ast.Call)
    assert _call_name(runtime_call.func) == "SubAgentRuntime"
    runtime_bindings = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in runtime_call.keywords
        if keyword.arg is not None
    }
    assert runtime_bindings["workers"] == (
        "{RESEARCH_DYNAMIC_SUBAGENT_IDS[capability]: worker "
        "for capability, worker in task_workers.items()}"
    )
    assert runtime_bindings["transcript_store"] == "subagent_transcript_store"
    assert runtime_bindings["result_ref_authority"] == "result_ref_authority"


def test_subagent_runtime_has_no_child_lifecycle_authority() -> None:
    runtime = PROJECT_ROOT / "framework" / "harness" / "subagents" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"), filename=str(runtime))
    subagent_runtime = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SubAgentRuntime"
    )
    init = next(
        node
        for node in subagent_runtime.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    arguments = {
        argument.arg
        for argument in (*init.args.args, *init.args.kwonlyargs)
    }
    assert "child_supervisor" not in arguments
    assert "parallel_coordinator" not in arguments

    forbidden_calls = [
        f"{ast.unparse(node.func.value)}.{node.func.attr}"
        for node in ast.walk(subagent_runtime)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CHILD_LIFECYCLE_METHODS
    ]
    assert forbidden_calls == []

    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "ChildAgentSupervisor" not in imports
    assert "ParallelAgentCoordinator" not in imports


def test_child_lifecycle_calls_stay_inside_harness_owned_modules() -> None:
    violations: list[str] = []
    discovered_modules: set[str] = set()
    for path, call in _production_calls():
        if not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr not in _CHILD_LIFECYCLE_METHODS:
            continue
        receiver = ast.unparse(call.func.value)
        if not _is_child_lifecycle_receiver(receiver):
            continue
        discovered_modules.add(path)
        if path not in _CHILD_LIFECYCLE_MODULES:
            violations.append(f"{path}:{call.lineno} {receiver}.{call.func.attr}")

    assert violations == []
    assert discovered_modules == _CHILD_LIFECYCLE_MODULES


def test_nougat_default_path_is_fail_closed_without_host_process_fallback() -> None:
    compiler = PROJECT_ROOT / "backend" / "research" / "document" / "pdf_compiler.py"
    tree = ast.parse(compiler.read_text(encoding="utf-8"), filename=str(compiler))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_nougat"
    )

    assert all(
        not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        )
        for node in ast.walk(function)
    )
    raised = {
        _call_name(node.exc.func)
        for node in ast.walk(function)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    }
    assert "ExecutionEnvironmentUnavailableError" in raised


def _production_calls() -> tuple[tuple[str, ast.Call], ...]:
    calls: list[tuple[str, ast.Call]] = []
    for root_name in PRODUCTION_ROOTS:
        root = PROJECT_ROOT / root_name
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            calls.extend(
                (relative, node)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            )
    return tuple(calls)


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_child_lifecycle_receiver(receiver: str) -> bool:
    normalized = receiver.lower()
    return any(
        token in normalized
        for token in (
            "child_supervisor",
            "owned_runtime",
            "child_runtime",
            "supervisor",
        )
    )
