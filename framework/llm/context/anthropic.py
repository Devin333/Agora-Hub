"""Translate an ``LLMRequest`` (OpenAI-shaped messages) into an Anthropic Messages body.

Requests across the codebase carry Chat Completions messages: ``system`` /
``user`` / ``assistant`` (optionally with ``tool_calls``) / ``tool``. The
Messages API instead takes a top-level ``system``, content blocks, ``tool_use``
blocks on the assistant turn and ``tool_result`` blocks on the following user
turn, and an explicit ``max_tokens``. Claude models reject non-default
sampling parameters, so ``temperature`` is never sent.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from framework.llm.clients.tool_adapters import (
    LLMToolSchemaError,
    openai_tool_name_map,
    to_openai_tools,
)
from framework.llm.context.normalization import (
    NormalizedLLMRequest,
    structured_output_projected_messages,
)
from framework.llm.models.reasoning import ReasoningSetting, ReasoningWire
from framework.llm.models.request import LLMRequest

ANTHROPIC_MESSAGES_NORMALIZER_REVISION = "anthropic-messages-v1"
# Used when neither the caller nor the model sets an output cap; Messages requires one.
DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS = 16_000


class AnthropicRequestError(ValueError):
    """The request cannot be expressed in the Messages API."""


@dataclass(frozen=True)
class AnthropicMessagesRequestNormalizer:
    reasoning: ReasoningSetting | None = None
    max_output_tokens: int | None = None
    revision: str = ANTHROPIC_MESSAGES_NORMALIZER_REVISION

    def normalize(self, request: LLMRequest, *, provider: str, model: str) -> NormalizedLLMRequest:
        normalized_request = request.clone(model=model)
        return NormalizedLLMRequest(
            request=normalized_request,
            payload=build_anthropic_messages_payload(
                normalized_request,
                model=model,
                reasoning=self.reasoning,
                max_output_tokens=self.max_output_tokens,
            ),
            provider=provider,
            normalizer_revision=self.revision,
        )


def build_anthropic_messages_payload(
    request: LLMRequest,
    *,
    model: str,
    reasoning: ReasoningSetting | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Messages body for ``request``.

    ``reasoning`` must use the Anthropic wire; its tier widens the output limit
    by the reasoning allowance, never past ``max_output_tokens``. The local
    JSON gate travels as a system instruction (see the structured-output
    request adapters); no native schema constraint is sent.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if reasoning is not None and reasoning.wire not in (ReasoningWire.ANTHROPIC_EFFORT, ReasoningWire.NONE):
        raise AnthropicRequestError("Anthropic Messages takes only the anthropic_effort reasoning wire")
    tools = [_anthropic_tool(tool) for tool in to_openai_tools(request.tools)] if request.tools else []
    name_map = openai_tool_name_map(request.tools) if request.tools else {}
    system, messages = _translate_messages(structured_output_projected_messages(request), name_map)
    if not messages:
        raise AnthropicRequestError("Anthropic Messages requires at least one user or assistant message")
    visible = request.max_tokens or max_output_tokens or DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS
    if reasoning is not None:
        limit = reasoning.output_tokens(visible, ceiling=max_output_tokens)
    else:
        limit = min(visible, max_output_tokens) if max_output_tokens is not None else visible
    payload: dict[str, Any] = {
        "model": model.strip(),
        "max_tokens": limit,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    if reasoning is not None and reasoning.wire is ReasoningWire.ANTHROPIC_EFFORT:
        payload.update(reasoning.anthropic_fields())
    return payload


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
    for message in raw_messages:
        role = message.get("role")
        if role == "system":
            # Messages has one system prompt; later system turns join it in order.
            text = _text_content(message.get("content"))
            if text:
                system_parts.append(text)
            continue
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
    "ANTHROPIC_MESSAGES_NORMALIZER_REVISION",
    "DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS",
    "AnthropicMessagesRequestNormalizer",
    "AnthropicRequestError",
    "build_anthropic_messages_payload",
]
