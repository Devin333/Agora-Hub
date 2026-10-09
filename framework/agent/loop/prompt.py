from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from framework.agent.models import AgentSpec
from framework.events.canonical import checksum_for
from framework.llm.models import LLMRequest
from framework.shared.json import to_jsonable as to_json_safe


_PREPARED_INPUT_BLOCK = "Prepared input projection JSON:"


class PreparedInputProjectionError(ValueError):
    """Raised when a prepared application projection is altered or omitted."""


@dataclass(frozen=True, slots=True)
class PreparedInputProjection:
    """Framework-neutral input prepared by an owning application.

    The framework does not interpret the payload. It adds a checksum-bound
    envelope to every logical request and verifies that the full serialized
    inputs object reached the final request before model dispatch.
    """

    projection_id: str
    projection_revision: str
    payload: Mapping[str, Any]
    input_key: str = "prepared_input"
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("projection_id", "projection_revision", "input_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty canonical text")
        if not isinstance(self.payload, Mapping):
            raise TypeError("prepared input projection payload must be a mapping")
        payload = _freeze_json_value(to_json_safe(dict(self.payload)))
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError("prepared input projection payload is required")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(
            self,
            "checksum",
            checksum_for(
                {
                    "projection_id": self.projection_id,
                    "projection_revision": self.projection_revision,
                    "payload": _thaw_json_value(payload),
                    "input_key": self.input_key,
                }
            ),
        )

    def envelope(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "projection_revision": self.projection_revision,
            "projection_checksum": self.checksum,
            "payload": _thaw_json_value(self.payload),
        }

    def project(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        projected = dict(to_json_safe(dict(inputs)))
        if self.input_key in projected:
            raise PreparedInputProjectionError(
                f"inputs already contain reserved prepared projection key {self.input_key!r}"
            )
        projected[self.input_key] = self.envelope()
        return projected

    def serialized_inputs(self, inputs: Mapping[str, Any]) -> str:
        return json.dumps(
            self.project(inputs),
            ensure_ascii=False,
            sort_keys=True,
        )

    def validate_request(
        self,
        request: LLMRequest,
        *,
        inputs: Mapping[str, Any],
    ) -> None:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be LLMRequest")
        metadata = request.metadata
        expected_metadata = self.request_metadata()
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise PreparedInputProjectionError(
                "final model request dropped prepared input projection identity"
            )
        serialized_inputs = self.serialized_inputs(inputs)
        contents = [str(message.get("content", "")) for message in request.messages]
        if not any(serialized_inputs in content for content in contents):
            raise PreparedInputProjectionError(
                "final model request dropped or altered the prepared input projection"
            )
        blocks: list[Any] = []
        marker = f"{_PREPARED_INPUT_BLOCK}\n"
        for content in contents:
            for raw_block in content.split(marker)[1:]:
                encoded = raw_block.split("\n", 1)[0]
                try:
                    decoded = json.loads(encoded)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise PreparedInputProjectionError(
                        "final model request contains an invalid prepared input block"
                    ) from exc
                blocks.append(decoded)
        if blocks != [self.envelope()]:
            raise PreparedInputProjectionError(
                "final model request prepared input block is missing or ambiguous"
            )

    def request_metadata(self) -> dict[str, str]:
        return {
            "prepared_input_projection_id": self.projection_id,
            "prepared_input_projection_revision": self.projection_revision,
            "prepared_input_projection_checksum": self.checksum,
        }


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


class PromptBuilder:
    def build(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        *,
        feedback: str | None = None,
        tool_observations: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        memory_context: str | None = None,
        skill_prompt_section: str | None = None,
        prepared_input_projection: PreparedInputProjection | None = None,
    ) -> LLMRequest:
        if prepared_input_projection is not None and not isinstance(
            prepared_input_projection, PreparedInputProjection
        ):
            raise TypeError(
                "prepared_input_projection must be PreparedInputProjection"
            )
        prompt_inputs = (
            prepared_input_projection.project(inputs)
            if prepared_input_projection is not None
            else dict(inputs)
        )
        serialized_inputs = json.dumps(
            to_json_safe(prompt_inputs), ensure_ascii=False, sort_keys=True
        )
        system = agent.system_prompt_template.format(
            role=agent.role,
            instructions=agent.instructions,
        )
        user = agent.task_prompt_template.format(
            goal=agent.goal,
            inputs=serialized_inputs,
        )
        if prepared_input_projection is not None:
            user += f"\n{_PREPARED_INPUT_BLOCK}\n"
            user += json.dumps(
                prepared_input_projection.envelope(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if memory_context:
            user += "\nMemory context:\n"
            user += memory_context
        if skill_prompt_section:
            user += "\n"
            user += skill_prompt_section
        if tool_observations:
            user += "\nTool observations: "
            user += json.dumps(
                to_json_safe(tool_observations),
                ensure_ascii=False,
                sort_keys=True,
            )
        if feedback:
            user += f"\nJudge feedback: {feedback}"

        metadata = {"agent_id": agent.agent_id}
        if prepared_input_projection is not None:
            metadata.update(prepared_input_projection.request_metadata())
        request = LLMRequest(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=tools or [],
            metadata=metadata,
            output_schema=agent.output_schema,
            output_schema_name=f"{agent.agent_id.replace('.', '_')}_output",
        )
        if prepared_input_projection is not None:
            prepared_input_projection.validate_request(request, inputs=inputs)
        return request


__all__ = [
    "PreparedInputProjection",
    "PreparedInputProjectionError",
    "PromptBuilder",
]
