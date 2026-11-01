from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import is_dataclass, asdict
from typing import Any, cast

from framework.llm.models import LLMToolCall


class LLMToolSchemaError(ValueError):
    """Raised when tool schemas cannot be adapted safely."""


class LLMToolCallParseError(ValueError):
    """Raised when provider tool calls cannot be parsed safely."""


_UNSAFE_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = [_tool_to_mapping(tool) for tool in tools]
    name_map = openai_tool_name_map(tools)
    adapted_tools = []
    for tool in tools:
        if _is_openai_tool(tool):
            adapted_tools.append(deepcopy(tool))
            continue

        internal_name = _internal_tool_name(tool)
        provider_name = name_map[internal_name]
        description = _required_tool_description(tool)
        input_schema = _openai_parameters(tool.get("input_schema"))
        adapted_tools.append(
            {
                "type": "function",
                "function": {
                    "name": provider_name,
                    "description": description,
                    "parameters": deepcopy(input_schema),
                },
            }
        )
    return adapted_tools


def openai_tool_name_map(tools: list[dict[str, Any]]) -> dict[str, str]:
    tools = [_tool_to_mapping(tool) for tool in tools]
    internal_to_provider: dict[str, str] = {}
    provider_to_internal: dict[str, str] = {}
    for tool in tools:
        if _is_openai_tool(tool):
            provider_name = _openai_function_name(tool)
            metadata = tool.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            internal_name = str(metadata.get("internal_name") or provider_name)
        else:
            internal_name = _internal_tool_name(tool)
            provider_name = _provider_safe_tool_name(internal_name)

        existing_internal = provider_to_internal.get(provider_name)
        if existing_internal is not None and existing_internal != internal_name:
            raise LLMToolSchemaError(
                f"tool names collide after provider adaptation: {existing_internal}, {internal_name}"
            )
        internal_to_provider[internal_name] = provider_name
        provider_to_internal[provider_name] = internal_name
    return internal_to_provider


def parse_openai_tool_calls(
    raw_tool_calls: Any,
    tools: list[dict[str, Any]],
) -> list[LLMToolCall]:
    tools = [_tool_to_mapping(tool) for tool in tools]
    if raw_tool_calls in (None, []):
        return []
    if not isinstance(raw_tool_calls, list):
        raise LLMToolCallParseError("provider tool_calls must be a list")

    provider_to_internal = {
        provider_name: internal_name
        for internal_name, provider_name in openai_tool_name_map(tools).items()
    }
    parsed_calls = []
    for index, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, dict):
            raise LLMToolCallParseError("provider tool_call must be an object")
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, dict):
            raise LLMToolCallParseError("provider tool_call function must be an object")

        provider_name = function.get("name")
        if not isinstance(provider_name, str) or not provider_name:
            raise LLMToolCallParseError("provider tool_call missing function name")
        raw_arguments = function.get("arguments") or "{}"
        if not isinstance(raw_arguments, str):
            raise LLMToolCallParseError("provider tool_call arguments must be a JSON string")

        provider_tool_call_id = raw_tool_call.get("id")
        tool_call_id = str(provider_tool_call_id or f"tool_call_{index + 1}")
        tool_name = provider_to_internal.get(provider_name, provider_name)
        provider_id = str(provider_tool_call_id) if provider_tool_call_id else None
        metadata = {"provider_tool_name": provider_name}
        # Malformed arguments reject only this call; its siblings stay usable.
        arguments, argument_error = decode_tool_call_arguments(raw_arguments)
        if argument_error is not None:
            parsed_calls.append(
                LLMToolCall.with_invalid_arguments(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    raw_arguments=raw_arguments,
                    error=argument_error,
                    provider_tool_call_id=provider_id,
                    metadata=metadata,
                )
            )
            continue
        parsed_calls.append(
            LLMToolCall(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                raw_arguments=raw_arguments,
                provider_tool_call_id=provider_id,
                metadata=metadata,
            )
        )
    return parsed_calls


def decode_tool_call_arguments(raw_arguments: str) -> tuple[dict[str, Any], str | None]:
    """Decode provider tool arguments, returning a per-call error on failure."""

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {}, f"tool call arguments are not valid JSON: {exc.msg}"
    if not isinstance(arguments, dict):
        return {}, "tool call arguments must be a JSON object"
    return arguments, None


def _is_openai_tool(tool: dict[str, Any]) -> bool:
    return tool.get("type") == "function" and isinstance(tool.get("function"), dict)


def _openai_function_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") or {}
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise LLMToolSchemaError("OpenAI-compatible function tool requires a name")
    return name


def _internal_tool_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise LLMToolSchemaError("tool schema requires a name")
    return name


def _provider_safe_tool_name(internal_name: str) -> str:
    provider_name = _UNSAFE_TOOL_NAME_CHARS.sub("_", internal_name).strip("_")
    if not provider_name:
        raise LLMToolSchemaError(f"tool name cannot be adapted: {internal_name}")
    return provider_name[:64]


def _openai_parameters(input_schema: Any) -> dict[str, Any]:
    if not isinstance(input_schema, dict) or not input_schema:
        return {"type": "object", "properties": {}}
    parameters = deepcopy(input_schema)
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})
    _validate_json_schema_types(parameters)
    return parameters


def _tool_to_mapping(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return dict(tool)
    to_dict = getattr(tool, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return payload
    if is_dataclass(tool):
        payload = asdict(cast(Any, tool))
        if isinstance(payload, dict):
            return payload
    raise LLMToolSchemaError("tool schema must be an object")


def _required_tool_description(tool: dict[str, Any]) -> str:
    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        raise LLMToolSchemaError("tool schema requires a non-empty description")
    return description.strip()


def _validate_json_schema_types(schema: Any, *, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise LLMToolSchemaError(f"{path}: schema must be an object")
    schema_type = schema.get("type")
    if schema_type is not None:
        allowed_types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not allowed_types or not all(isinstance(item, str) for item in allowed_types):
            raise LLMToolSchemaError(f"{path}: type must be a string or array")
        supported = {"object", "array", "string", "integer", "number", "boolean", "null"}
        unsupported = [item for item in allowed_types if item not in supported]
        if unsupported:
            raise LLMToolSchemaError(
                f"{path}: unsupported parameter type: {', '.join(unsupported)}"
            )
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise LLMToolSchemaError(f"{path}.properties must be an object")
        for name, property_schema in properties.items():
            _validate_json_schema_types(property_schema, path=f"{path}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        _validate_json_schema_types(items, path=f"{path}.items")

