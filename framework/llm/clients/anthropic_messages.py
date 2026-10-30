"""Native Anthropic Messages client implementing ``LLMClient`` on the official SDK.

The SDK owns the wire protocol (headers, API version, SSE framing, response
models). This client owns what the rest of the codebase relies on: the
OpenAI-shaped request translation, the structured-output local gate, the
project retry policy (the SDK's own retries are off), redacted provider
errors and token accounting.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import anthropic
import httpx2

from framework.llm.clients.endpoint import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.openai_compatible import (
    LLMProviderContextOverflow,
    LLMProviderError,
    LLMRetryPolicy,
    Sleep,
)
from framework.llm.clients.tool_adapters import (
    LLMToolSchemaError,
    decode_tool_call_arguments,
    openai_tool_name_map,
)
from framework.llm.context.anthropic import AnthropicRequestError, build_anthropic_messages_payload
from framework.llm.models import LLMRequest, LLMResponse, LLMStreamEvent, LLMToolCall, TokenUsage
from framework.llm.models.reasoning import ReasoningSetting
from framework.llm.structured_output import (
    LLMStructuredOutputParseError,
    LLMStructuredOutputProjectionError,
    LLMStructuredOutputSchemaError,
    LLMStructuredOutputValidationError,
    ProviderSchemaProjection,
    ProviderStructuredOutputCapability,
    StructuredOutputContract,
    StructuredOutputDiagnostic,
    compile_structured_output_contract,
    decode_structured_output,
    project_structured_output_contract,
    structured_output_response_fingerprint,
    structured_output_text_fingerprint,
    validate_compiled_structured_output,
)

ANTHROPIC_PROVIDER = "anthropic"
# 529 is Anthropic's "overloaded".
ANTHROPIC_RETRYABLE_STATUS_CODES = (408, 409, 429, 500, 502, 503, 504, 529)
# Claude has no JSON mode; the schema travels as an instruction and the reply is
# validated locally against the contract.
_SUPPORTED_PROJECTION_MODES = frozenset({"json_object_local_gate"})
_CONTEXT_OVERFLOW_STOP = "model_context_window_exceeded"


class AnthropicMessagesClient:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float,
        http_client: httpx2.Client | None = None,
        retry_policy: LLMRetryPolicy | None = None,
        structured_output_capability: ProviderStructuredOutputCapability | None = None,
        reasoning: ReasoningSetting | None = None,
        max_output_tokens: int | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._retry_policy = retry_policy or LLMRetryPolicy(
            retryable_status_codes=ANTHROPIC_RETRYABLE_STATUS_CODES
        )
        self._capability = structured_output_capability
        self._reasoning = reasoning
        self._max_output_tokens = max_output_tokens
        self._sleep = sleep or time.sleep
        self._sdk = anthropic.Anthropic(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout_seconds,
            # Retries follow the project policy so attempts, deadlines and
            # diagnostics match every other provider.
            max_retries=0,
            http_client=http_client,
        )

    @classmethod
    def for_endpoint(
        cls,
        endpoint: LLMEndpoint,
        *,
        timeout_seconds: float,
        http_client: httpx2.Client | None = None,
        retry_policy: LLMRetryPolicy | None = None,
        sleep: Sleep | None = None,
    ) -> AnthropicMessagesClient:
        if endpoint.protocol is not LLMWireProtocol.ANTHROPIC_MESSAGES:
            raise ValueError("endpoint does not speak the Anthropic Messages protocol")
        return cls(
            provider=endpoint.provider,
            base_url=endpoint.base_url,
            model=endpoint.model,
            api_key=endpoint.api_key,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
            retry_policy=retry_policy,
            structured_output_capability=endpoint.structured_output_capability,
            reasoning=endpoint.reasoning,
            max_output_tokens=endpoint.max_output_tokens,
            sleep=sleep,
        )

    @staticmethod
    def supports_structured_output_projection(projection: ProviderSchemaProjection) -> bool:
        return projection.mode in _SUPPORTED_PROJECTION_MODES

    def close(self) -> None:
        self._sdk.close()

    # -- public calls -------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        execution_request, contract, projection, payload = self._prepare(request)
        max_attempts = min(
            self._retry_policy.max_attempts,
            request.max_transport_attempts or self._retry_policy.max_attempts,
        )
        deadline = time.monotonic() + request.timeout_seconds if request.timeout_seconds is not None else None
        for attempt in range(1, max_attempts + 1):
            try:
                timeout = self._remaining(deadline)
                message = self._sdk.messages.create(**payload, timeout=timeout)
                return self._response(
                    message.to_dict(),
                    request=execution_request,
                    attempts=attempt,
                    contract=contract,
                    projection=projection,
                )
            except LLMProviderError as error:
                failure = error
            except anthropic.AnthropicError as exc:
                failure = self._sdk_error(exc, attempts=attempt)
            except TimeoutError as exc:
                failure = self._timeout_error(exc, attempts=attempt)
            if not failure.retryable or attempt >= max_attempts:
                raise failure
            delay = self._retry_policy.delay_after_attempt(attempt)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise failure
            self._sleep(delay)
        raise LLMProviderError(
            f"{self.provider} request failed before sending",
            provider=self.provider,
            model=self.model,
            attempts=max_attempts,
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        execution_request, contract, projection, payload = self._prepare(request)
        timeout = (
            min(self.timeout_seconds, request.timeout_seconds)
            if request.timeout_seconds is not None
            else self.timeout_seconds
        )
        # Opened before the generator is returned so connection and HTTP
        # failures surface at the call site, like every other client.
        try:
            events = self._sdk.messages.create(**payload, stream=True, timeout=timeout)
        except anthropic.AnthropicError as exc:
            raise self._sdk_error(exc, attempts=1) from exc
        return self._stream_events(
            events,
            request=execution_request,
            contract=contract,
            projection=projection,
        )

    def list_models(self) -> tuple[str, ...]:
        """Model ids this key may call; one listing, no tokens generated."""
        try:
            return tuple(item.id for item in self._sdk.models.list(limit=1000))
        except anthropic.AnthropicError as exc:
            raise self._sdk_error(exc, attempts=1) from exc

    # -- request preparation -----------------------------------------

    def _prepare(
        self, request: LLMRequest
    ) -> tuple[LLMRequest, StructuredOutputContract | None, ProviderSchemaProjection | None, dict[str, Any]]:
        try:
            execution_request, contract, projection = self._structured_execution(request)
            payload = build_anthropic_messages_payload(
                execution_request,
                # The endpoint's model is the only one the account pays for.
                model=self.model,
                reasoning=self._reasoning,
                max_output_tokens=self._max_output_tokens,
            )
        except (LLMToolSchemaError, AnthropicRequestError) as exc:
            raise LLMProviderError(
                f"{self.provider} request cannot be expressed: {exc}",
                provider=self.provider,
                model=self.model,
                error_type="schema_error",
                retryable=False,
            ) from exc
        except LLMStructuredOutputSchemaError as exc:
            raise LLMProviderError(
                f"{self.provider} structured output schema failed preflight",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_schema_error",
                retryable=False,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
            ) from exc
        except LLMStructuredOutputProjectionError as exc:
            raise LLMProviderError(
                f"{self.provider} structured output projection is ineligible",
                provider=self.provider,
                model=self.model,
                error_type="provider_schema_ineligible",
                retryable=False,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
            ) from exc
        return execution_request, contract, projection, payload

    def _structured_execution(
        self, request: LLMRequest
    ) -> tuple[LLMRequest, StructuredOutputContract | None, ProviderSchemaProjection | None]:
        contract = request.structured_output_contract()
        if contract is None and request.output_schema is not None:
            contract = compile_structured_output_contract(
                request.structured_output_schema_source(),
                schema_name=request.output_schema_name,
            )
        if contract is None:
            return request, None, None
        projection = request.provider_schema_projection()
        if projection is None:
            if self._capability is None:
                raise _ineligible(contract, "structured-output capability is not configured", "provider_capability_missing")
            projection = project_structured_output_contract(
                contract,
                self._capability,
                policy=request.structured_output_policy,
                streaming=False,
            )
        elif self._capability is None or (
            projection.deployment != self._capability.deployment
            or projection.provider_capability_revision != self._capability.revision
        ):
            raise _ineligible(
                contract,
                "provider projection was not made for the bound endpoint",
                "provider_projection_endpoint_mismatch",
            )
        if projection.contract_digest != contract.schema_digest:
            raise _ineligible(contract, "provider projection does not match compiled contract", "provider_projection_contract_mismatch")
        if not self.supports_structured_output_projection(projection):
            raise _ineligible(
                contract,
                "Anthropic Messages adapter enforces structured output only through the local gate",
                "provider_adapter_mapping_unsupported",
            )
        return (
            request.with_structured_output_execution(contract=contract, projection=projection),
            contract,
            projection,
        )

    # -- response handling -------------------------------------------

    def _response(
        self,
        message: dict[str, Any],
        *,
        request: LLMRequest,
        attempts: int,
        contract: StructuredOutputContract | None,
        projection: ProviderSchemaProjection | None,
    ) -> LLMResponse:
        stop_reason = message.get("stop_reason")
        self._raise_for_stop_reason(stop_reason, attempts=attempts)
        text, tool_calls = _content(message.get("content") or [], request)
        structured_output = None
        # A tool-call turn is not terminal: its structured answer comes later.
        if contract is not None and not tool_calls:
            structured_output = self._structured_output(text, contract=contract, attempts=attempts, projection=projection)
        return LLMResponse(
            content=text,
            usage=_usage(message.get("usage")),
            metadata={
                "provider": self.provider,
                "model": self.model,
                "response_id": message.get("id"),
                "finish_reason": stop_reason,
                "attempts": attempts,
                "retry_count": attempts - 1,
                **_validation_metadata(request, contract, projection, structured_output),
            },
            structured_output=structured_output,
            tool_calls=tool_calls,
            model=self.model,
            raw=dict(message),
            execution_identity=request.execution_identity,
        )

    def _stream_events(
        self,
        events: Any,
        *,
        request: LLMRequest,
        contract: StructuredOutputContract | None,
        projection: ProviderSchemaProjection | None,
    ) -> Iterator[LLMStreamEvent]:
        identity = request.execution_identity
        provisional = contract is not None
        yield LLMStreamEvent(
            event_type="message_start",
            metadata={"provider": self.provider, "model": self.model, "provisional": provisional},
            execution_identity=identity,
        )
        blocks: dict[int, dict[str, Any]] = {}
        response_id: str | None = None
        stop_reason: str | None = None
        input_usage: dict[str, Any] = {}
        output_tokens = 0
        finished = False
        try:
            with events:
                for event in events:
                    kind = event.type
                    if kind == "message_start":
                        response_id = event.message.id
                        input_usage = event.message.usage.to_dict()
                    elif kind == "content_block_start":
                        blocks[event.index] = event.content_block.to_dict()
                        if blocks[event.index].get("type") == "tool_use":
                            blocks[event.index]["_partial_json"] = ""
                    elif kind == "content_block_delta":
                        delta = event.delta
                        block = blocks.setdefault(event.index, {"type": "text", "text": ""})
                        if delta.type == "text_delta":
                            block["text"] = block.get("text", "") + delta.text
                            yield LLMStreamEvent(
                                event_type="text_delta",
                                text_delta=delta.text,
                                metadata={"provisional": provisional},
                                execution_identity=identity,
                            )
                        elif delta.type == "input_json_delta":
                            block["_partial_json"] = block.get("_partial_json", "") + delta.partial_json
                    elif kind == "message_delta":
                        stop_reason = event.delta.stop_reason or stop_reason
                        output_tokens = event.usage.output_tokens or output_tokens
                    elif kind == "message_stop":
                        finished = True
        except anthropic.AnthropicError as exc:
            raise self._sdk_error(exc, attempts=1) from exc
        if not finished:
            raise LLMProviderError(
                f"{self.provider} stream ended before a terminal marker",
                provider=self.provider,
                model=self.model,
                error_type="provider_stream_incomplete",
                retryable=False,
            )
        self._raise_for_stop_reason(stop_reason, attempts=1)
        content = []
        for index in sorted(blocks):
            block = dict(blocks[index])
            partial = block.pop("_partial_json", None)
            if block.get("type") == "tool_use" and partial:
                arguments, argument_error = decode_tool_call_arguments(partial)
                block["input"] = arguments
                if argument_error is not None:
                    block[_RAW_ARGUMENTS_KEY] = partial
                    block[_ARGUMENT_ERROR_KEY] = argument_error
            content.append(block)
        text, tool_calls = _content(content, request)
        structured_output = None
        # A tool-call turn is not terminal: its structured answer comes later.
        if contract is not None and not tool_calls:
            structured_output = self._structured_output(text, contract=contract, attempts=1, projection=projection)
        for tool_call in tool_calls:
            yield LLMStreamEvent(
                event_type="tool_call_complete",
                tool_call=tool_call,
                execution_identity=identity,
            )
        usage = _usage({**input_usage, "output_tokens": output_tokens})
        yield LLMStreamEvent(
            event_type="usage_delta",
            usage_delta=usage,
            metadata={"provisional": False},
            execution_identity=identity,
        )
        yield LLMStreamEvent(
            event_type="message_complete",
            structured_output=structured_output,
            metadata={
                "provider": self.provider,
                "model": self.model,
                "response_id": response_id,
                "finish_reason": stop_reason or "end_turn",
                "attempts": 1,
                "retry_count": 0,
                "structured_output_requested": provisional,
                "response_format_mode": projection.mode if projection is not None else None,
                "tool_count": len(request.tools),
                "provisional": False,
                **_validation_metadata(request, contract, projection, structured_output),
            },
            execution_identity=identity,
        )

    def _raise_for_stop_reason(self, stop_reason: Any, *, attempts: int) -> None:
        if stop_reason == _CONTEXT_OVERFLOW_STOP:
            raise LLMProviderContextOverflow(
                f"{self.provider} request exceeded the model context window",
                provider=self.provider,
                model=self.model,
                attempts=attempts,
                provider_error_code=_CONTEXT_OVERFLOW_STOP,
            )
        if stop_reason == "refusal":
            raise LLMProviderError(
                f"{self.provider} declined to answer this request",
                provider=self.provider,
                model=self.model,
                error_type="provider_refusal",
                retryable=False,
                attempts=attempts,
            )

    def _structured_output(
        self,
        text: str,
        *,
        contract: StructuredOutputContract,
        attempts: int,
        projection: ProviderSchemaProjection | None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise LLMProviderError(
                f"{self.provider} returned empty structured output content",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_empty",
                retryable=True,
                attempts=attempts,
            )
        try:
            decoded = decode_structured_output(_strip_json_fence(text), limits=contract.limits)
        except LLMStructuredOutputParseError as exc:
            raise LLMProviderError(
                f"{self.provider} structured output failed strict JSON decoding",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_parse_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
                response_fingerprint=structured_output_text_fingerprint(text),
            ) from exc
        try:
            return validate_compiled_structured_output(decoded, contract)
        except LLMStructuredOutputValidationError as exc:
            raise LLMProviderError(
                f"{self.provider} structured output failed schema validation: {exc}",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_validation_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
                response_fingerprint=structured_output_response_fingerprint(decoded),
            ) from exc

    # -- errors --------------------------------------------------------

    def _remaining(self, deadline: float | None) -> float:
        if deadline is None:
            return self.timeout_seconds
        remaining = min(self.timeout_seconds, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("LLM request deadline exhausted")
        return remaining

    def _timeout_error(self, exc: BaseException, *, attempts: int) -> LLMProviderError:
        return LLMProviderError(
            f"{self.provider} request failed: timeout",
            provider=self.provider,
            model=self.model,
            error_type="timeout",
            retryable=True,
            attempts=attempts,
            diagnostics=({"stage": "network", "error_class": type(exc).__name__},),
        )

    def _sdk_error(self, exc: anthropic.AnthropicError, *, attempts: int) -> LLMProviderError:
        # Only the error class, status and Anthropic's error type travel on:
        # provider messages can echo request content.
        if isinstance(exc, anthropic.APITimeoutError):
            return self._timeout_error(exc, attempts=attempts)
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMProviderError(
                f"{self.provider} request failed: transient_network",
                provider=self.provider,
                model=self.model,
                error_type="transient_network",
                retryable=True,
                attempts=attempts,
                diagnostics=({"stage": "network", "error_class": type(exc).__name__},),
            )
        if isinstance(exc, anthropic.APIStatusError):
            status = int(exc.status_code)
            provider_type = _anthropic_error_type(exc.body)
            if status == 413 or (status == 400 and provider_type == "request_too_large"):
                return LLMProviderContextOverflow(
                    f"{self.provider} request exceeded provider context capacity",
                    provider=self.provider,
                    model=self.model,
                    status_code=status,
                    attempts=attempts,
                    provider_error_code=provider_type,
                )
            return LLMProviderError(
                f"{self.provider} request failed: HTTP {status}",
                provider=self.provider,
                model=self.model,
                error_type=_error_type_for_status(status),
                retryable=status in self._retry_policy.retryable_status_codes,
                status_code=status,
                attempts=attempts,
                diagnostics=(
                    {
                        "stage": "http_error_response",
                        "error_class": type(exc).__name__,
                        "provider_error_type": provider_type,
                    },
                ),
            )
        return LLMProviderError(
            f"{self.provider} request failed",
            provider=self.provider,
            model=self.model,
            error_type="unknown_llm_error",
            retryable=False,
            attempts=attempts,
            diagnostics=({"stage": "sdk", "error_class": type(exc).__name__},),
        )


def _ineligible(contract: StructuredOutputContract, message: str, reason: str) -> LLMStructuredOutputProjectionError:
    return LLMStructuredOutputProjectionError(
        message,
        diagnostics=(
            StructuredOutputDiagnostic(
                code="provider_schema_ineligible",
                message=message,
                validator=reason,
                contract_digest=contract.schema_digest,
            ),
        ),
    )


_RAW_ARGUMENTS_KEY = "_raw_arguments"
_ARGUMENT_ERROR_KEY = "_argument_error"


def _content(blocks: list[Any], request: LLMRequest) -> tuple[str, list[LLMToolCall]]:
    reverse_names = {safe: original for original, safe in openai_tool_name_map(request.tools).items()} if request.tools else {}
    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            name = str(block.get("name") or "")
            arguments = block.get("input")
            tool_call_id = str(block.get("id") or "")
            argument_error = block.get(_ARGUMENT_ERROR_KEY)
            if argument_error is None and not isinstance(arguments, dict):
                argument_error = "tool call arguments must be a JSON object"
            if argument_error is not None:
                # Malformed arguments reject only this call; its siblings stay usable.
                raw_arguments = block.get(_RAW_ARGUMENTS_KEY)
                tool_calls.append(
                    LLMToolCall.with_invalid_arguments(
                        tool_call_id=tool_call_id,
                        tool_name=reverse_names.get(name, name),
                        raw_arguments=(
                            str(raw_arguments)
                            if raw_arguments is not None
                            else json.dumps(arguments, ensure_ascii=False, default=str)
                        ),
                        error=str(argument_error),
                        provider_tool_call_id=tool_call_id or None,
                    )
                )
                continue
            tool_calls.append(
                LLMToolCall(
                    tool_call_id=tool_call_id,
                    tool_name=reverse_names.get(name, name),
                    arguments=dict(arguments),
                    provider_tool_call_id=tool_call_id or None,
                )
            )
    return "".join(text_parts), tool_calls


def _usage(raw: Any) -> TokenUsage:
    usage = raw if isinstance(raw, dict) else {}
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    return TokenUsage(
        # Anthropic reports cached prompt tokens separately from input_tokens.
        input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(details.get("thinking_tokens") or details.get("reasoning_tokens") or 0),
        cached_input_tokens=cache_read,
    )


def _validation_metadata(
    request: LLMRequest,
    contract: StructuredOutputContract | None,
    projection: ProviderSchemaProjection | None,
    structured_output: dict[str, Any] | None,
) -> dict[str, Any]:
    if contract is None:
        return {}
    return {
        "structured_output_validation": {
            "validated": structured_output is not None,
            "schema_name": request.output_schema_name if request.output_schema is not None else None,
            "schema_digest": contract.schema_digest,
            "schema_revision": contract.schema_revision,
            "schema_dialect": contract.dialect,
            "typed_adapter_revision": contract.typed_adapter_revision,
            "projection_digest": projection.projection_digest if projection is not None else None,
            "projection_mode": projection.mode if projection is not None else None,
            "provider_capability_revision": projection.provider_capability_revision if projection is not None else None,
            "provider_enforced_keywords": sorted(projection.enforced_keywords) if projection is not None else [],
            "provider_omitted_keywords": sorted(projection.omitted_keywords) if projection is not None else [],
            "provider_native_json_mode": False,
            "response_fingerprint": (
                structured_output_response_fingerprint(structured_output)
                if structured_output is not None
                else None
            ),
        }
    }


def _strip_json_fence(text: str) -> str:
    # Claude sometimes wraps an object in a Markdown fence despite the instruction.
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1 and stripped.endswith("```"):
            return stripped[first_newline + 1 : -3].strip()
    return stripped


def _anthropic_error_type(body: Any) -> str | None:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            return error["type"]
    return None


def _error_type_for_status(status: int) -> str:
    if status == 400:
        return "invalid_request"
    if status in (401, 403):
        return "auth_error"
    if status == 404:
        return "unsupported_model"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status == 529 or 500 <= status <= 599:
        return "server_error"
    return "invalid_request" if 400 <= status <= 499 else "unknown_llm_error"


__all__ = ["ANTHROPIC_PROVIDER", "ANTHROPIC_RETRYABLE_STATUS_CODES", "AnthropicMessagesClient"]
