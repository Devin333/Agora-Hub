"""Native Anthropic Messages API client implementing ``LLMClient``."""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import chain
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from framework.llm.clients.provider_errors import (
    LLMConfigurationError,
    LLMProviderContextOverflow,
    LLMProviderError,
    decode_provider_error_payload,
    error_type_from_http_status,
    network_failure_diagnostic,
    provider_failure_diagnostic,
    provider_overflow_token_diagnostics,
    read_bounded_http_error_body,
)
from framework.llm.clients.structured_execution import (
    StructuredOutputExecutor,
    expects_structured_output,
)
from framework.llm.clients.tool_adapters import (
    LLMToolCallParseError,
    LLMToolSchemaError,
    openai_tool_name_map,
)
from framework.llm.clients.transport import (
    LLMRetryPolicy,
    Sleep,
    StreamTransport,
    Transport,
    urlopen_stream_transport,
    urlopen_transport,
)
from framework.llm.context.anthropic import (
    ANTHROPIC_CONTENT_FIELD,
    AnthropicModelOptions,
    AnthropicRequestError,
    build_anthropic_messages_payload,
)
from framework.llm.models import LLMRequest, LLMResponse, LLMStreamEvent, LLMToolCall, TokenUsage
from framework.llm.structured_output import (
    LLMStructuredOutputProjectionError,
    LLMStructuredOutputSchemaError,
    ProviderSchemaProjection,
    ProviderStructuredOutputCapability,
)
from framework.shared.secret_text import SecretText

ANTHROPIC_API_VERSION = "2026-10-21"
ANTHROPIC_PROVIDER = "anthropic"
# 529 is Anthropic's "overloaded".
ANTHROPIC_RETRYABLE_STATUS_CODES = (408, 409, 429, 500, 502, 503, 504, 529)
_SUPPORTED_PROJECTION_MODES = frozenset({"native_strict", "json_object_local_gate"})
_PROMPT_TOO_LONG = re.compile(r"prompt is too long|context (?:window|length)|too many (?:input )?tokens", re.I)
_JSON_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n\s*```\s*\Z", re.S)


@dataclass(frozen=True)
class AnthropicMessagesConfig:
    base_url: str
    model: str
    api_key: SecretText = field(repr=False)
    options: AnthropicModelOptions = field(default_factory=AnthropicModelOptions)
    timeout_seconds: float = 120.0
    provider: str = ANTHROPIC_PROVIDER
    api_version: str = ANTHROPIC_API_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, SecretText):
            object.__setattr__(self, "api_key", SecretText(self.api_key))
        if not self.base_url.strip() or not self.model.strip():
            raise LLMConfigurationError("Anthropic base_url and model are required")


class AnthropicMessagesClient:
    def __init__(
        self,
        config: AnthropicMessagesConfig,
        *,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        retry_policy: LLMRetryPolicy | None = None,
        structured_output_capability: ProviderStructuredOutputCapability | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or urlopen_transport
        self._stream_transport = stream_transport or urlopen_stream_transport
        self._retry_policy = retry_policy or LLMRetryPolicy(retryable_status_codes=ANTHROPIC_RETRYABLE_STATUS_CODES)
        self._sleep = sleep or time.sleep
        self._structured = StructuredOutputExecutor(
            provider=config.provider,
            model=config.model,
            capability=structured_output_capability,
            supported_projection_modes=_SUPPORTED_PROJECTION_MODES,
            native_json_mode=_uses_native_schema,
            adapter_name="Anthropic Messages",
        )

    @staticmethod
    def supports_structured_output_projection(projection: ProviderSchemaProjection) -> bool:
        return projection.mode in _SUPPORTED_PROJECTION_MODES

    def complete(self, request: LLMRequest) -> LLMResponse:
        execution_request, contract, projection, payload = self._prepare(request, streaming=False)
        max_attempts = min(
            self._retry_policy.max_attempts,
            request.max_transport_attempts or self._retry_policy.max_attempts,
        )
        deadline = time.monotonic() + request.timeout_seconds if request.timeout_seconds is not None else None
        for attempt in range(1, max_attempts + 1):
            try:
                remaining = (
                    self.config.timeout_seconds
                    if deadline is None
                    else min(self.config.timeout_seconds, deadline - time.monotonic())
                )
                if remaining <= 0:
                    raise TimeoutError("LLM request deadline exhausted")
                raw_body = self._transport(self._http_request(payload), remaining)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("LLM response arrived after request deadline")
            except HTTPError as exc:
                error = self._error_from_http(exc, attempts=attempt)
            except (TimeoutError, URLError, OSError) as exc:
                error = self._error_from_network(exc, attempts=attempt)
            else:
                message = self._decode_object(raw_body, attempts=attempt, stage="response_json_decode")
                return self._response(message, request=execution_request, attempts=attempt,
                                      contract=contract, projection=projection, raw_body=raw_body)
            if not error.retryable or attempt >= max_attempts:
                raise error
            delay = self._retry_policy.delay_after_attempt(attempt)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise error
            self._sleep(delay)
        raise LLMProviderError(
            f"{self.config.provider} request failed before sending",
            provider=self.config.provider,
            attempts=max_attempts,
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        execution_request, contract, projection, payload = self._prepare(request, streaming=True)
        payload["stream"] = True
        timeout = (
            min(self.config.timeout_seconds, request.timeout_seconds)
            if request.timeout_seconds is not None
            else self.config.timeout_seconds
        )
        try:
            lines = iter(self._stream_transport(self._http_request(payload), timeout))
            first_line = next(lines)
        except StopIteration:
            first_line = None
        except HTTPError as exc:
            raise self._error_from_http(exc, attempts=1) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise self._error_from_network(exc, attempts=1) from exc

        structured_requested = expects_structured_output(execution_request)
        identity = execution_request.execution_identity
        yield LLMStreamEvent(
            event_type="message_start",
            metadata={"provider": self.config.provider, "model": self.config.model,
                      "provisional": structured_requested},
            execution_identity=identity,
        )
        accumulator = _MessageAccumulator()
        source = lines if first_line is None else chain((first_line,), lines)
        try:
            for event_name, data in _server_sent_events(source):
                if event_name == "ping":
                    continue
                event = self._decode_object(data.encode("utf-8"), attempts=1, stage="stream_json_decode")
                if event.get("type") == "error":
                    raise self._stream_error(event)
                text_delta = accumulator.apply(event)
                if text_delta:
                    yield LLMStreamEvent(
                        event_type="text_delta",
                        text_delta=text_delta,
                        metadata={"provisional": structured_requested},
                        execution_identity=identity,
                    )
                if accumulator.finished:
                    break
        except HTTPError as exc:
            raise self._error_from_http(exc, attempts=1) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise self._error_from_network(exc, attempts=1) from exc
        except UnicodeDecodeError as exc:
            raise self._shape_error("stream chunk is not valid UTF-8", attempts=1) from exc
        except LLMToolCallParseError as exc:
            raise self._request_error(f"tool call parse failed: {exc}", "schema_error") from exc

        if not accumulator.finished:
            raise LLMProviderError(
                f"{self.config.provider} stream ended before a terminal marker",
                provider=self.config.provider,
                model=self.config.model,
                error_type="provider_stream_incomplete",
                retryable=False,
                attempts=1,
            )
        message = accumulator.message()
        self._raise_for_refusal(message, attempts=1)
        yield LLMStreamEvent(
            event_type="usage_delta",
            usage_delta=_usage(message.get("usage")),
            metadata={"provisional": structured_requested},
            execution_identity=identity,
        )
        tool_calls = self._tool_calls(message, execution_request, attempts=1)
        for tool_call in tool_calls:
            yield LLMStreamEvent(event_type="tool_call_complete", tool_call=tool_call, execution_identity=identity)
        structured_output = None
        if structured_requested:
            structured_output = self._structured.parse(
                _structured_text(_text(message), projection), attempts=1, contract=contract
            )
        yield LLMStreamEvent(
            event_type="message_complete",
            structured_output=structured_output,
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "response_id": message.get("id"),
                "finish_reason": _finish_reason(message.get("stop_reason")),
                "attempts": 1,
                "retry_count": 0,
                "structured_output_requested": structured_requested,
                "response_format_mode": projection.mode if projection is not None else None,
                "tool_count": len(execution_request.tools),
                "provisional": False,
                ANTHROPIC_CONTENT_FIELD: message.get("content") or [],
                **self._structured.validation_metadata(
                    execution_request, contract=contract, projection=projection,
                    structured_output=structured_output,
                ),
            },
            execution_identity=identity,
        )

    def _prepare(self, request: LLMRequest, *, streaming: bool):
        try:
            execution_request, contract, projection = self._structured.resolve(request, streaming=streaming)
            payload = build_anthropic_messages_payload(
                execution_request,
                model=execution_request.model or self.config.model,
                options=self.config.options,
            )
        except LLMToolSchemaError as exc:
            raise self._request_error(f"request tool schema is invalid: {exc}", "schema_error") from exc
        except LLMStructuredOutputSchemaError as exc:
            raise LLMProviderError(
                f"{self.config.provider} structured output schema failed preflight",
                provider=self.config.provider,
                model=self.config.model,
                error_type="structured_output_schema_error",
                retryable=False,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
            ) from exc
        except LLMStructuredOutputProjectionError as exc:
            raise self._structured.projection_error(exc) from exc
        except AnthropicRequestError as exc:
            raise self._request_error(str(exc), "invalid_request") from exc
        return execution_request, contract, projection, payload

    def _http_request(self, payload: dict[str, Any]) -> Request:
        return Request(
            url=f"{self.config.base_url.rstrip('/')}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": self.config.api_key.reveal(),
                "anthropic-version": self.config.api_version,
                "content-type": "application/json",
            },
            method="POST",
        )

    def _response(
        self,
        message: dict[str, Any],
        *,
        request: LLMRequest,
        attempts: int,
        contract,
        projection: ProviderSchemaProjection | None,
        raw_body: bytes,
    ) -> LLMResponse:
        if message.get("type") != "message" or not isinstance(message.get("content"), list):
            raise self._shape_error("response is not a message", attempts=attempts, raw_body=raw_body)
        self._raise_for_refusal(message, attempts=attempts)
        tool_calls = self._tool_calls(message, request, attempts=attempts)
        content = _text(message)
        structured_output = None
        if expects_structured_output(request):
            structured_output = self._structured.parse(
                _structured_text(content, projection), attempts=attempts, contract=contract
            )
        return LLMResponse(
            content=content,
            usage=_usage(message.get("usage")),
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "response_id": message.get("id"),
                "finish_reason": _finish_reason(message.get("stop_reason")),
                "attempts": attempts,
                "retry_count": attempts - 1,
                ANTHROPIC_CONTENT_FIELD: message["content"],
                **self._structured.validation_metadata(
                    request, contract=contract, projection=projection, structured_output=structured_output
                ),
            },
            structured_output=structured_output,
            tool_calls=tool_calls,
            model=self.config.model,
            raw=dict(message),
            execution_identity=request.execution_identity,
        )

    def _tool_calls(self, message: dict[str, Any], request: LLMRequest, *, attempts: int) -> list[LLMToolCall]:
        provider_to_internal = {
            provider: internal for internal, provider in openai_tool_name_map(request.tools).items()
        } if request.tools else {}
        calls: list[LLMToolCall] = []
        try:
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, call_id, arguments = block.get("name"), block.get("id"), block.get("input")
                if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
                    raise LLMToolCallParseError("tool_use block requires a name and id")
                if not isinstance(arguments, dict):
                    raise LLMToolCallParseError("tool_use input must be an object")
                calls.append(LLMToolCall(
                    tool_call_id=call_id,
                    tool_name=provider_to_internal.get(name, name),
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                    provider_tool_call_id=call_id,
                    metadata={"provider_tool_name": name},
                ))
        except LLMToolCallParseError as exc:
            raise self._request_error(f"tool call parse failed: {exc}", "schema_error", attempts=attempts) from exc
        return calls

    def _raise_for_refusal(self, message: dict[str, Any], *, attempts: int) -> None:
        if message.get("stop_reason") != "refusal":
            return
        details = message.get("stop_details") if isinstance(message.get("stop_details"), dict) else {}
        category = details.get("category")
        raise LLMProviderError(
            f"{self.config.provider} declined the request",
            provider=self.config.provider,
            model=self.config.model,
            error_type="content_refused",
            retryable=False,
            attempts=attempts,
            diagnostics=({"stage": "refusal", "refusal_category": category if isinstance(category, str) else None},),
        )

    def _decode_object(self, raw: bytes, *, attempts: int, stage: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._shape_error("payload is not valid JSON", attempts=attempts, raw_body=raw, stage=stage) from exc
        if not isinstance(payload, dict):
            raise self._shape_error("payload is not an object", attempts=attempts, raw_body=raw, stage=stage)
        return payload

    def _error_from_http(self, exc: HTTPError, *, attempts: int) -> LLMProviderError:
        status = int(exc.code)
        raw, truncated = read_bounded_http_error_body(exc)
        payload = decode_provider_error_payload(raw, truncated=truncated)
        error = payload.get("error") if isinstance(payload, dict) else None
        error_kind = error.get("type") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if status == 413 or (
            status == 400 and error_kind == "invalid_request_error"
            and isinstance(message, str) and _PROMPT_TOO_LONG.search(message)
        ):
            limit, usage = provider_overflow_token_diagnostics(payload)
            return LLMProviderContextOverflow(
                f"{self.config.provider} request exceeded provider context capacity",
                provider=self.config.provider,
                model=self.config.model,
                status_code=status,
                attempts=attempts,
                provider_error_code=error_kind if isinstance(error_kind, str) else None,
                provider_reported_limit_tokens=limit,
                provider_reported_usage_tokens=usage,
            )
        return LLMProviderError(
            f"{self.config.provider} request failed: HTTP {status}",
            provider=self.config.provider,
            model=self.config.model,
            error_type="server_error" if status == 529 else error_type_from_http_status(status),
            retryable=status in self._retry_policy.retryable_status_codes,
            status_code=status,
            attempts=attempts,
            diagnostics=(provider_failure_diagnostic(
                payload=payload, raw_body=raw, raw_body_truncated=truncated, stage="http_error_response",
            ),),
        )

    def _error_from_network(self, exc: TimeoutError | URLError | OSError, *, attempts: int) -> LLMProviderError:
        reason = getattr(exc, "reason", None)
        is_timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        error_type = "timeout" if is_timeout else "transient_network"
        return LLMProviderError(
            f"{self.config.provider} request failed: {error_type}",
            provider=self.config.provider,
            model=self.config.model,
            error_type=error_type,
            retryable=True,
            attempts=attempts,
            diagnostics=(network_failure_diagnostic(exc, reason=reason),),
        )

    def _stream_error(self, event: dict[str, Any]) -> LLMProviderError:
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        kind = error.get("type")
        overloaded = kind in {"overloaded_error", "api_error"}
        return LLMProviderError(
            f"{self.config.provider} stream reported an error",
            provider=self.config.provider,
            model=self.config.model,
            error_type="rate_limit" if kind == "rate_limit_error" else "server_error" if overloaded else "invalid_request",
            retryable=overloaded or kind == "rate_limit_error",
            attempts=1,
            diagnostics=(provider_failure_diagnostic(payload=event, stage="stream_error_event"),),
        )

    def _request_error(self, detail: str, error_type: str, *, attempts: int = 1) -> LLMProviderError:
        return LLMProviderError(
            f"{self.config.provider} {detail}",
            provider=self.config.provider,
            model=self.config.model,
            error_type=error_type,
            retryable=False,
            attempts=attempts,
        )

    def _shape_error(
        self, detail: str, *, attempts: int, raw_body: bytes = b"", stage: str = "response_shape"
    ) -> LLMProviderError:
        return LLMProviderError(
            f"{self.config.provider} {detail}",
            provider=self.config.provider,
            model=self.config.model,
            error_type="schema_error",
            retryable=False,
            attempts=attempts,
            diagnostics=(provider_failure_diagnostic(raw_body=raw_body, stage=stage),),
        )


class _MessageAccumulator:
    """Rebuilds the final message from Messages streaming events."""

    def __init__(self) -> None:
        self._message: dict[str, Any] = {}
        self._blocks: dict[int, dict[str, Any]] = {}
        self._partial_json: dict[int, list[str]] = {}
        self.finished = False

    def apply(self, event: dict[str, Any]) -> str | None:
        """Fold one event into the message; returns any visible text it added."""
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            self._message = {key: value for key, value in event["message"].items() if key != "content"}
        elif kind == "content_block_start" and isinstance(event.get("content_block"), dict):
            index = _index(event)
            block = dict(event["content_block"])
            if block.get("type") == "tool_use":
                block["input"] = {}
                self._partial_json[index] = []
            self._blocks[index] = block
        elif kind == "content_block_delta" and isinstance(event.get("delta"), dict):
            return self._apply_delta(_index(event), event["delta"])
        elif kind == "content_block_stop":
            self._close_block(_index(event))
        elif kind == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            for key in ("stop_reason", "stop_sequence", "stop_details"):
                if key in delta:
                    self._message[key] = delta[key]
            if isinstance(event.get("usage"), dict):
                # message_delta usage counts are cumulative, so they replace earlier values.
                self._message["usage"] = {**(self._message.get("usage") or {}), **event["usage"]}
        elif kind == "message_stop":
            self.finished = True
        return None

    def _apply_delta(self, index: int, delta: dict[str, Any]) -> str | None:
        block = self._blocks.setdefault(index, {"type": "text", "text": ""})
        kind = delta.get("type")
        if kind == "text_delta" and isinstance(delta.get("text"), str):
            block["text"] = block.get("text", "") + delta["text"]
            return delta["text"]
        if kind == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            self._partial_json.setdefault(index, []).append(delta["partial_json"])
        elif kind == "thinking_delta" and isinstance(delta.get("thinking"), str):
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
        elif kind == "signature_delta" and isinstance(delta.get("signature"), str):
            block["signature"] = delta["signature"]
        return None

    def _close_block(self, index: int) -> None:
        if index not in self._partial_json:
            return
        raw = "".join(self._partial_json.pop(index))
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise LLMToolCallParseError("streamed tool input is not valid JSON") from exc
        self._blocks[index]["input"] = decoded

    def message(self) -> dict[str, Any]:
        return {**self._message, "content": [self._blocks[index] for index in sorted(self._blocks)]}


def _server_sent_events(lines) -> Iterator[tuple[str, str]]:
    event_name = "message"
    data: list[str] = []
    for raw in lines:
        line = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).rstrip("\r\n")
        if not line:
            if data:
                yield event_name, "\n".join(data)
            event_name, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event_name = value
        elif name == "data":
            data.append(value)
    if data:
        yield event_name, "\n".join(data)


def _index(event: dict[str, Any]) -> int:
    index = event.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else 0


def _text(message: dict[str, Any]) -> str:
    return "".join(
        block.get("text") or ""
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _structured_text(content: str, projection: ProviderSchemaProjection | None) -> str:
    """Without native enforcement Claude may fence its JSON; the local gate still validates it."""
    if projection is not None and projection.mode == "native_strict":
        return content
    match = _JSON_FENCE.match(content)
    return match.group(1) if match else content


def _usage(raw: Any) -> TokenUsage:
    usage = raw if isinstance(raw, dict) else {}

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    cached = count("cache_read_input_tokens")
    return TokenUsage(
        input_tokens=count("input_tokens") + cached + count("cache_creation_input_tokens"),
        output_tokens=count("output_tokens"),
        cached_input_tokens=cached,
    )


def _finish_reason(stop_reason: Any) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "pause_turn": "pause",
    }.get(stop_reason, str(stop_reason or "stop"))


def _uses_native_schema(request: LLMRequest) -> bool:
    projection = request.provider_schema_projection()
    return projection is not None and projection.mode == "native_strict"


__all__ = [
    "ANTHROPIC_API_VERSION",
    "ANTHROPIC_RETRYABLE_STATUS_CODES",
    "AnthropicMessagesClient",
    "AnthropicMessagesConfig",
]
