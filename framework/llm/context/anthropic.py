"""Translate an ``LLMRequest`` (OpenAI-shaped messages) into an Anthropic Messages body.

Requests across the codebase carry Chat Completions messages: ``system`` /
``user`` / ``assistant`` (optionally with ``tool_calls``) / ``tool``. The
Messages API instead takes a top-level ``system``, content blocks, ``tool_use``
blocks on the assistant turn and ``tool_result`` blocks on the following user
turn, and an explicit ``max_tokens``.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from framework.llm.clients.tool_adapters import (
    LLMToolSchemaError,
    openai_tool_name_map,
    to_openai_tools,
)
from framework.llm.context.normalization import NormalizedLLMRequest
from framework.llm.models.reasoning import ReasoningEffort
from framework.llm.models.request import LLMRequest

ANTHROPIC_MESSAGES_NORMALIZER_REVISION = "anthropic-messages-v1"
# Used when the caller sets no output cap; Messages requires one.
DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS = 16_000
# Assistant messages may carry the provider's own content blocks so thinking and
# tool_use blocks are replayed unchanged on the next turn.
ANTHROPIC_CONTENT_FIELD = "anthropic_content"


class AnthropicThinkingOff(str, Enum):
    """How a Claude model is asked to stop thinking, when it allows that at all."""

    DISABLED = "disabled"
    BETWEEN_TOOLS = "between_tools"


class AnthropicRequestError(ValueError):
    """The request cannot be expressed for this Claude model."""


@dataclass(frozen=True)
class AnthropicModelOptions:
    reasoning_effort: ReasoningEffort | None = None
    # None means this model cannot run with thinking switched off.
    thinking_off: AnthropicThinkingOff | None = None
    default_max_output_tokens: int = DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", ReasoningEffort.parse(self.reasoning_effort))
        if self.thinking_off is not None:
            object.__setattr__(self, "thinking_off", AnthropicThinkingOff(self.thinking_off))
        if self.default_max_output_tokens < 1:
            raise ValueError("default_max_output_tokens must be positive")


_ANTHROPIC_EFFORT = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "medium",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "xhigh",
    ReasoningEffort.MAX: "max",
}


@dataclass(frozen=True)
class AnthropicMessagesRequestNormalizer:
    options: AnthropicModelOptions = AnthropicModelOptions()
    revision: str = ANTHROPIC_MESSAGES_NORMALIZER_REVISION

    def normalize(self, request: LLMRequest, *, provider: str, model: str) -> NormalizedLLMRequest:
        normalized_request = request.clone(model=model)
        return NormalizedLLMRequest(
            request=normalized_request,
            payload=build_anthropic_messages_payload(normalized_request, model=model, options=self.options),
            provider=provider,
            normalizer_revision=self.revision,
        )


def build_anthropic_messages_payload(
    request: LLMRequest,
    *,
    model: str,
    options: AnthropicModelOptions | None = None,
) -> dict[str, Any]:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    options = options or AnthropicModelOptions()
    tools = [_anthropic_tool(tool) for tool in to_openai_tools(request.tools)] if request.tools else []
    name_map = openai_tool_name_map(request.tools) if request.tools else {}
    system, messages = _translate_messages(request.messages, name_map)
    if not messages:
        raise AnthropicRequestError("Anthropic Messages requires at least one user or assistant message")
    payload: dict[str, Any] = {
        "model": model.strip(),
        "max_tokens": request.max_tokens or options.default_max_output_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    output_config: dict[str, Any] = {}
    effort = request.reasoning_effort or options.reasoning_effort
    if effort is ReasoningEffort.OFF:
        if options.thinking_off is None:
            raise AnthropicRequestError(f"{model} cannot run with thinking switched off")
        payload["thinking"] = {"type": options.thinking_off.value}
    elif effort is not None:
        payload["thinking"] = {"type": "adaptive"}
        output_config["effort"] = _ANTHROPIC_EFFORT[effort]
    output_format = _output_format(request)
    if output_format is not None:
        output_config["format"] = output_format
    if output_config:
        payload["output_config"] = output_config
    return payload


def _output_format(request: LLMRequest) -> dict[str, Any] | None:
    """Native schema enforcement only for a projection that was released for it.

    The local JSON gate sends no wire constraint: the prompt asks for JSON and the
    client validates the decoded object against the contract.
    """
    projection = request.provider_schema_projection()
    if projection is None or projection.mode == "json_object_local_gate":
        return None
    if projection.provider_schema is None:
        raise ValueError("provider schema projection is missing its schema")
    return {"type": "json_schema", "schema": deepcopy(projection.provider_schema)}


def _anthropic_tool(openai_tool: dict[str, Any]) -> dict[str, Any]:
    function = openai_tool.get("function")
    if not isinstance(function, dict):
        raise LLMToolSchemaError("tool must be a function tool")
    tool: dict[str, Any] = {
        "name": function["name"],
        "input_schema": deepcopy(function.get("parameters") or {"type": "object", "properties": {}}),
    }
    if function.get("description"):
        tool["description"] = function["description"]
    return tool


def _translate_messages(
    raw_messages: list[dict[str, Any]],
    name_map: dict[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    started = False
    for message in raw_messages:
        role = message.get("role")
        if role == "system":
            text = _text_content(message.get("content"))
            if not started:
                if text:
                    system_parts.append(text)
                continue
            # A later system message stays in place as an operator instruction.
            _append(messages, "system", [{"type": "text", "text": text}])
            continue
        started = True
        if role == "user":
            _append(messages, "user", _user_blocks(message.get("content")))
        elif role == "assistant":
            _append(messages, "assistant", _assistant_blocks(message, name_map))
        elif role == "tool":
            _append(messages, "user", [_tool_result_block(message)])
        else:
            raise AnthropicRequestError(f"unsupported message role: {role!r}")
    return "\n\n".join(system_parts), messages


def _append(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return
    messages.append({"role": role, "content": list(blocks)})


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise AnthropicRequestError("user message content must be text or a list of parts")
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise AnthropicRequestError("message content part must be an object")
        kind = part.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            blocks.append(_image_block(part.get("image_url")))
        elif kind in {"image", "document"}:
            blocks.append(deepcopy(part))
        else:
            raise AnthropicRequestError(f"unsupported message content part: {kind!r}")
    return blocks


def _image_block(image_url: Any) -> dict[str, Any]:
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        raise AnthropicRequestError("image_url part requires a url")
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header.removeprefix("data:").split(";", 1)[0]
        if ";base64" not in header or not media_type or not data:
            raise AnthropicRequestError("image data URLs must be base64 encoded")
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _assistant_blocks(message: dict[str, Any], name_map: dict[str, str]) -> list[dict[str, Any]]:
    provider_content = message.get(ANTHROPIC_CONTENT_FIELD)
    if isinstance(provider_content, list):
        return deepcopy(provider_content)
    blocks: list[dict[str, Any]] = []
    text = _text_content(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise AnthropicRequestError("assistant tool_calls entries must be objects")
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") or call.get("name") or call.get("tool_name")
        if not isinstance(name, str) or not name:
            raise AnthropicRequestError("assistant tool call requires a name")
        call_id = call.get("id") or call.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise AnthropicRequestError("assistant tool call requires an id")
        blocks.append({
            "type": "tool_use",
            "id": call_id,
            "name": name_map.get(name, name),
            "input": _tool_input(function.get("arguments", call.get("arguments"))),
        })
    return blocks


def _tool_input(arguments: Any) -> dict[str, Any]:
    if arguments in (None, ""):
        return {}
    if isinstance(arguments, dict):
        return deepcopy(arguments)
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise AnthropicRequestError("assistant tool call arguments are not valid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise AnthropicRequestError("assistant tool call arguments must be a JSON object")


def _tool_result_block(message: dict[str, Any]) -> dict[str, Any]:
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise AnthropicRequestError("tool message requires tool_call_id")
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": _text_content(message.get("content")),
    }
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and metadata.get("is_error") is True:
        block["is_error"] = True
    return block


def _text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


__all__ = [
    "ANTHROPIC_CONTENT_FIELD",
    "ANTHROPIC_MESSAGES_NORMALIZER_REVISION",
    "DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS",
    "AnthropicMessagesRequestNormalizer",
    "AnthropicModelOptions",
    "AnthropicRequestError",
    "AnthropicThinkingOff",
    "build_anthropic_messages_payload",
]
