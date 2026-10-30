from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


DELEGATE_BATCH_SCHEMA_VERSION = "newsroom.agent.delegate-batch/v1"
MAX_DELEGATE_BATCH_TASKS = 32


@dataclass(frozen=True)
class DelegateBatchProposal:
    """Untrusted logical child proposal emitted by the parent model."""

    logical_task_id: str
    objective: str
    capability_hint: str
    input_refs: tuple[str, ...]
    output_role: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("logical_task_id", "objective", "capability_hint", "output_role"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"delegate_batch {field_name} must be a non-empty canonical string")
        for field_name in ("input_refs", "depends_on"):
            values = tuple(getattr(self, field_name))
            if any(not isinstance(value, str) or not value.strip() or value != value.strip() for value in values):
                raise ValueError(f"delegate_batch {field_name} must contain canonical strings")
            if len(values) != len(set(values)):
                raise ValueError(f"delegate_batch {field_name} must not contain duplicates")
            object.__setattr__(self, field_name, values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_task_id": self.logical_task_id,
            "objective": self.objective,
            "capability_hint": self.capability_hint,
            "input_refs": list(self.input_refs),
            "output_role": self.output_role,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DelegateBatchProposal":
        expected = {
            "logical_task_id",
            "objective",
            "capability_hint",
            "input_refs",
            "output_role",
            "depends_on",
        }
        if set(value) != expected:
            unexpected = sorted(set(value) - expected)
            missing = sorted(expected - set(value))
            raise ValueError(
                "delegate_batch child proposal has unsupported fields "
                f"{unexpected} or missing fields {missing}"
            )
        input_refs = value["input_refs"]
        depends_on = value["depends_on"]
        if not isinstance(input_refs, list) or not isinstance(depends_on, list):
            raise ValueError("delegate_batch input_refs and depends_on must be arrays")
        return cls(
            logical_task_id=value["logical_task_id"],
            objective=value["objective"],
            capability_hint=value["capability_hint"],
            input_refs=tuple(input_refs),
            output_role=value["output_role"],
            depends_on=tuple(depends_on),
        )


@dataclass(frozen=True)
class DelegateBatchCandidate:
    """Versioned, bounded candidate. It carries no execution authority."""

    correlation_id: str
    tasks: tuple[DelegateBatchProposal, ...]
    parallelism_hint: int | None = None
    schema_version: str = DELEGATE_BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip() or self.correlation_id != self.correlation_id.strip():
            raise ValueError("delegate_batch correlation_id must be a non-empty canonical string")
        tasks = tuple(self.tasks)
        if not tasks or len(tasks) > MAX_DELEGATE_BATCH_TASKS:
            raise ValueError(f"delegate_batch must contain between 1 and {MAX_DELEGATE_BATCH_TASKS} tasks")
        if not all(isinstance(item, DelegateBatchProposal) for item in tasks):
            raise TypeError("delegate_batch tasks must be DelegateBatchProposal values")
        task_ids = tuple(item.logical_task_id for item in tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("delegate_batch logical_task_id values must be unique")
        task_id_set = set(task_ids)
        for task in tasks:
            if task.logical_task_id in task.depends_on or not set(task.depends_on).issubset(task_id_set):
                raise ValueError("delegate_batch dependencies must reference other tasks in the same candidate")
        _validate_delegate_batch_dag(tasks)
        if self.parallelism_hint is not None and (
            isinstance(self.parallelism_hint, bool)
            or not isinstance(self.parallelism_hint, int)
            or self.parallelism_hint < 1
            or self.parallelism_hint > MAX_DELEGATE_BATCH_TASKS
        ):
            raise ValueError("delegate_batch parallelism_hint must be a bounded positive integer")
        if self.schema_version != DELEGATE_BATCH_SCHEMA_VERSION:
            raise ValueError("unsupported delegate_batch schema_version")
        object.__setattr__(self, "tasks", tasks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "correlation_id": self.correlation_id,
            "tasks": [task.to_dict() for task in self.tasks],
            "parallelism_hint": self.parallelism_hint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DelegateBatchCandidate":
        required = {"schema_version", "correlation_id", "tasks"}
        allowed = required | {"parallelism_hint"}
        if not required.issubset(value) or not set(value).issubset(allowed):
            unexpected = sorted(set(value) - allowed)
            missing = sorted(required - set(value))
            raise ValueError(
                "delegate_batch has forbidden control or unsupported fields "
                f"{unexpected} or missing fields {missing}"
            )
        tasks = value["tasks"]
        if not isinstance(tasks, list) or not all(isinstance(item, Mapping) for item in tasks):
            raise ValueError("delegate_batch tasks must be an array of objects")
        return cls(
            schema_version=value["schema_version"],
            correlation_id=value["correlation_id"],
            tasks=tuple(DelegateBatchProposal.from_dict(item) for item in tasks),
            parallelism_hint=value.get("parallelism_hint"),
        )


def _validate_delegate_batch_dag(tasks: tuple[DelegateBatchProposal, ...]) -> None:
    dependencies = {task.logical_task_id: set(task.depends_on) for task in tasks}
    resolved: set[str] = set()
    while dependencies:
        ready = sorted(task_id for task_id, task_dependencies in dependencies.items() if task_dependencies.issubset(resolved))
        if not ready:
            raise ValueError("delegate_batch dependencies must form a DAG")
        resolved.update(ready)
        for task_id in ready:
            dependencies.pop(task_id)



@dataclass(frozen=True)
class AgentToolRequest:
    """One tool invocation requested by the model in a single turn.

    ``argument_error`` marks a request whose arguments could not be decoded;
    it is rejected on its own without invoking the tool, so the remaining
    requests of the same turn still run.
    """

    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    argument_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str):
            raise TypeError("tool request tool_name must be a string")
        if not isinstance(self.tool_args, Mapping):
            raise TypeError("tool request tool_args must be an object")
        object.__setattr__(self, "tool_args", dict(self.tool_args))
        if self.tool_call_id is not None:
            object.__setattr__(self, "tool_call_id", str(self.tool_call_id) or None)
        if self.argument_error is not None:
            object.__setattr__(self, "argument_error", str(self.argument_error) or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_args": dict(self.tool_args),
            "tool_call_id": self.tool_call_id,
            "argument_error": self.argument_error,
        }


class AgentActionType(str, Enum):
    FINAL = "final"
    TOOL_CALL = "tool_call"
    SKILL_CALL = "skill_call"
    ASK_CLARIFICATION = "ask_clarification"
    DELEGATE = "delegate"
    DELEGATE_BATCH = "delegate_batch"
    THINK = "think"


@dataclass(frozen=True)
class AgentAction:
    action_type: AgentActionType | str
    content: str | None = None
    output: dict[str, Any] | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    subagent_id: str | None = None
    subagent_task: str | None = None
    handoff_reason: str | None = None
    delegate_batch: DelegateBatchCandidate | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[AgentToolRequest, ...] = ()

    def __post_init__(self) -> None:
        tool_calls = tuple(self.tool_calls)
        if any(not isinstance(item, AgentToolRequest) for item in tool_calls):
            raise TypeError("tool_calls must contain AgentToolRequest values")
        if tool_calls and (self.tool_name is not None or self.tool_args):
            raise ValueError("tool_call action uses either tool_calls or tool_name, not both")
        object.__setattr__(self, "tool_calls", tool_calls)

    def is_final(self) -> bool:
        return _action_type_value(self.action_type) in {"final", "final_output"}

    def is_tool_call(self) -> bool:
        return _action_type_value(self.action_type) == "tool_call"

    def tool_requests(self) -> tuple[AgentToolRequest, ...]:
        """Every tool invocation of this action, in the order the model requested."""

        if not self.is_tool_call():
            return ()
        if self.tool_calls:
            return self.tool_calls
        tool_call_id = self.metadata.get("tool_call_id")
        return (
            AgentToolRequest(
                tool_name=self.tool_name or "",
                tool_args=self.tool_args,
                tool_call_id=str(tool_call_id) if tool_call_id else None,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": _action_type_value(self.action_type),
            "content": self.content,
            "output": self.output,
            "tool_name": self.tool_name,
            "tool_args": dict(self.tool_args),
            "subagent_id": self.subagent_id,
            "subagent_task": self.subagent_task,
            "handoff_reason": self.handoff_reason,
            "delegate_batch": self.delegate_batch.to_dict() if self.delegate_batch is not None else None,
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def final(cls, content: str) -> "AgentAction":
        return cls(action_type=AgentActionType.FINAL, content=content, output={"content": content})

    @classmethod
    def tool_call(cls, tool_name: str, tool_args: dict[str, Any]) -> "AgentAction":
        return cls(action_type=AgentActionType.TOOL_CALL, tool_name=tool_name, tool_args=dict(tool_args))

    @classmethod
    def tool_batch(cls, requests: list[AgentToolRequest] | tuple[AgentToolRequest, ...]) -> "AgentAction":
        if not requests:
            raise ValueError("tool batch requires at least one tool request")
        return cls(action_type=AgentActionType.TOOL_CALL, tool_calls=tuple(requests))


def _action_type_value(action_type: AgentActionType | str) -> str:
    return action_type.value if isinstance(action_type, AgentActionType) else str(action_type)
