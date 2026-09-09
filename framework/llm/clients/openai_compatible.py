from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from itertools import chain
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from framework.llm.context.openai import build_openai_chat_payload, openai_response_format
from framework.llm.models import LLMRequest, LLMResponse, TokenUsage
from framework.llm.redaction import redact_sensitive_values
from framework.llm.models import LLMStreamEvent
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
from framework.llm.clients.tool_adapters import (
    LLMToolCallParseError,
    LLMToolSchemaError,
    parse_openai_tool_calls,
)


class LLMConfigurationError(RuntimeError):
    """Raised when LLM provider configuration is incomplete."""


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider request fails."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        deployment_id: str | None = None,
        error_type: str = "unknown_llm_error",
        retryable: bool = False,
        status_code: int | None = None,
        attempts: int = 1,
        diagnostics: Iterable[dict[str, Any]] | None = None,
        response_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.deployment_id = deployment_id
        self.error_type = error_type
        self.retryable = retryable
        self.status_code = status_code
        self.attempts = attempts
        self.diagnostics = tuple(dict(item) for item in (diagnostics or ()))
        self.response_fingerprint = response_fingerprint

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": str(self),
            "provider": self.provider,
            "model": self.model,
            "deployment_id": self.deployment_id,
            "error_type": self.error_type,
            "error_category": _canonical_error_category(self.error_type),
            "retryable": self.retryable,
            "status_code": self.status_code,
            "attempts": self.attempts,
        }
        if self.diagnostics:
            payload["diagnostics"] = [dict(item) for item in self.diagnostics]
        if self.response_fingerprint:
            payload["response_fingerprint"] = self.response_fingerprint
        if redact:
            return redact_sensitive_values(payload)
        return payload


class LLMProviderContextOverflow(LLMProviderError):
    """Stable non-transient provider context-window overflow."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        deployment_id: str | None = None,
        status_code: int | None = None,
        attempts: int = 1,
        provider_error_code: str | None = None,
        provider_reported_limit_tokens: int | None = None,
        provider_reported_usage_tokens: int | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            deployment_id=deployment_id,
            error_type="context_length",
            retryable=False,
            status_code=status_code,
            attempts=attempts,
        )
        self.provider_error_code = provider_error_code
        self.provider_reported_limit_tokens = provider_reported_limit_tokens
        self.provider_reported_usage_tokens = provider_reported_usage_tokens

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload = super().to_dict(redact=False)
        payload.update(
            {
                "provider_error_code": self.provider_error_code,
                "provider_reported_limit_tokens": self.provider_reported_limit_tokens,
                "provider_reported_usage_tokens": self.provider_reported_usage_tokens,
            }
        )
        if redact:
            return redact_sensitive_values(payload)
        return payload


Transport = Callable[[Request, float], bytes]
StreamTransport = Callable[[Request, float], Iterable[bytes | str]]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class LLMRetryPolicy:
    max_attempts: int = 1
    retry_delay_seconds: tuple[float, ...] = (0.0,)
    retryable_status_codes: tuple[int, ...] = (408, 409, 429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        object.__setattr__(self, "retry_delay_seconds", tuple(self.retry_delay_seconds))
        object.__setattr__(self, "retryable_status_codes", tuple(self.retryable_status_codes))
        if any(delay < 0 for delay in self.retry_delay_seconds):
            raise ValueError("retry delays must be non-negative")

    def delay_after_attempt(self, attempt: int) -> float:
        if not self.retry_delay_seconds:
            return 0.0
        index = min(max(attempt - 1, 0), len(self.retry_delay_seconds) - 1)
        return self.retry_delay_seconds[index]


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: float = 90.0

    @classmethod
    def dashscope_defaults(cls) -> OpenAICompatibleConfig:
        return cls(
            provider="dashscope",
            base_url=(
                os.environ.get("NEWS_LLM_BASE_URL")
                or os.environ.get("DASHSCOPE_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            model=os.environ.get("NEWS_LLM_MODEL", "deepseek-v4-flash"),
            api_key_env="DASHSCOPE_API_KEY",
        )

    def resolve_api_key(self) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise LLMConfigurationError(f"missing required environment variable: {self.api_key_env}")
        return api_key


class OpenAICompatibleClient:
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        retry_policy: LLMRetryPolicy | None = None,
        structured_output_capability: ProviderStructuredOutputCapability | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _urlopen_transport
        self._stream_transport = stream_transport or _urlopen_stream_transport
        self._retry_policy = retry_policy or LLMRetryPolicy()
        self._structured_output_capability = structured_output_capability
        self._sleep = sleep or time.sleep

    @staticmethod
    def supports_structured_output_projection(
        projection: ProviderSchemaProjection,
    ) -> bool:
        return projection.mode in {"native_strict", "json_object_local_gate"}

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            execution_request, contract, projection = (
                self._resolve_structured_output_execution(
                    request,
                    streaming=False,
                )
            )
            payload = self._build_payload(execution_request)
        except LLMToolSchemaError as exc:
            raise LLMProviderError(
                f"{self.config.provider} request tool schema is invalid: {exc}",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
            ) from exc
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
            raise self._provider_projection_error(exc) from exc

        api_key = self.config.resolve_api_key()

        max_attempts = min(self._retry_policy.max_attempts, request.max_transport_attempts or self._retry_policy.max_attempts)
        deadline = time.monotonic() + request.timeout_seconds if request.timeout_seconds is not None else None
        for attempt in range(1, max_attempts + 1):
            http_request = self._build_http_request(api_key, payload)
            try:
                remaining = self.config.timeout_seconds if deadline is None else min(self.config.timeout_seconds, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("LLM request deadline exhausted")
                raw_body = self._transport(http_request, remaining)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("LLM response arrived after request deadline")
            except HTTPError as exc:
                error = self._error_from_http(exc, attempts=attempt)
            except (TimeoutError, URLError, OSError) as exc:
                error = self._error_from_network(exc, attempts=attempt)
            else:
                response_payload = self._parse_response_payload(raw_body, attempts=attempt)
                return self._normalize_response(
                    response_payload,
                    request=execution_request,
                    attempts=attempt,
                    contract=contract,
                    projection=projection,
                )

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
        try:
            execution_request, contract, projection = (
                self._resolve_structured_output_execution(
                    request,
                    streaming=True,
                )
            )
            payload = self._build_payload(execution_request)
        except LLMToolSchemaError as exc:
            raise LLMProviderError(
                f"{self.config.provider} request tool schema is invalid: {exc}",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
            ) from exc
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
            raise self._provider_projection_error(exc) from exc
        payload["stream"] = True
        api_key = self.config.resolve_api_key()

        http_request = self._build_http_request(api_key, payload)
        try:
            timeout = min(self.config.timeout_seconds, request.timeout_seconds) if request.timeout_seconds is not None else self.config.timeout_seconds
            lines = iter(self._stream_transport(http_request, timeout))
            first_line = next(lines)
        except StopIteration:
            first_line = None
        except HTTPError as exc:
            raise self._error_from_http(exc, attempts=1) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise self._error_from_network(exc, attempts=1) from exc

        expects_structured_output = _expects_structured_output(execution_request)
        yield LLMStreamEvent(
            event_type="message_start",
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "provisional": expects_structured_output,
            },
            execution_identity=execution_request.execution_identity,
        )
        assembler = _OpenAIStreamToolCallAssembler(execution_request.tools)
        completed = False
        last_finish_reason: str | None = None
        response_id: str | None = None
        text_parts: list[str] = []

        try:
            source_lines = lines if first_line is None else chain((first_line,), lines)
            for raw_line in source_lines:
                line = _stream_line_text(raw_line)
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    completed = True
                    break
                chunk = self._parse_stream_chunk(data)
                response_id = response_id or _optional_str(chunk.get("id"))
                usage = _usage_from_stream_chunk(chunk)
                if usage is not None:
                    yield LLMStreamEvent(
                        event_type="usage_delta",
                        usage_delta=usage,
                        metadata={"provisional": expects_structured_output},
                        execution_identity=execution_request.execution_identity,
                    )
                for event in _events_from_stream_chunk(
                    chunk,
                    assembler=assembler,
                    provider=self.config.provider,
                    attempts=1,
                    execution_identity=execution_request.execution_identity,
                ):
                    if event.event_type == "message_complete":
                        last_finish_reason = event.metadata.get("finish_reason")
                        completed = True
                        continue
                    if event.event_type == "text_delta" and event.text_delta:
                        text_parts.append(event.text_delta)
                    yield _with_provisional_metadata(
                        event,
                        provisional=expects_structured_output,
                    )
        except LLMToolCallParseError as exc:
            raise LLMProviderError(
                f"{self.config.provider} streaming tool call parse failed: {exc}",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=1,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"{self.config.provider} stream chunk is not valid JSON",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=1,
            ) from exc
        except HTTPError as exc:
            raise self._error_from_http(exc, attempts=1) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise self._error_from_network(exc, attempts=1) from exc

        if not completed:
            raise LLMProviderError(
                f"{self.config.provider} stream ended before a terminal marker",
                provider=self.config.provider,
                model=self.config.model,
                error_type="provider_stream_incomplete",
                retryable=False,
                attempts=1,
            )
        structured_output = None
        if expects_structured_output:
            structured_output = self._parse_structured_output(
                "".join(text_parts),
                request=execution_request,
                attempts=1,
                contract=contract,
            )
        validation_metadata = self._structured_output_validation_metadata(
            execution_request,
            contract=contract,
            projection=projection,
            structured_output=structured_output,
        )
        yield LLMStreamEvent(
            event_type="message_complete",
            structured_output=structured_output,
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "response_id": response_id,
                "finish_reason": last_finish_reason or "stop",
                "attempts": 1,
                "retry_count": 0,
                "structured_output_requested": expects_structured_output,
                "response_format_mode": (
                    projection.mode if projection is not None else None
                ),
                "tool_count": len(execution_request.tools),
                "provisional": False,
                **validation_metadata,
            },
            execution_identity=execution_request.execution_identity,
        )

    def _build_payload(
        self,
        request: LLMRequest,
    ) -> dict[str, Any]:
        return build_openai_chat_payload(
            request,
            model=request.model or self.config.model,
        )

    @staticmethod
    def _compile_structured_output_contract(
        request: LLMRequest,
    ) -> StructuredOutputContract | None:
        if request.output_schema is None:
            return None
        return compile_structured_output_contract(
            request.structured_output_schema_source(),
            schema_name=request.output_schema_name,
        )

    def _resolve_structured_output_execution(
        self,
        request: LLMRequest,
        *,
        streaming: bool,
    ) -> tuple[
        LLMRequest,
        StructuredOutputContract | None,
        ProviderSchemaProjection | None,
    ]:
        contract = request.structured_output_contract()
        if contract is None:
            contract = self._compile_structured_output_contract(request)
        if contract is None:
            return request, None, None

        projection = request.provider_schema_projection()
        if projection is None:
            capability = self._structured_output_capability
            if capability is None:
                raise self._projection_ineligible_error(
                    contract,
                    "structured-output capability is not configured",
                    reason="provider_capability_missing",
                )
            if capability.provider != self.config.provider:
                raise self._projection_ineligible_error(
                    contract,
                    "structured-output capability provider does not match client",
                    reason="provider_capability_identity_mismatch",
                )
            projection = project_structured_output_contract(
                contract,
                capability,
                policy=request.structured_output_policy,
                streaming=streaming,
            )
        if projection.contract_digest != contract.schema_digest:
            raise self._projection_ineligible_error(
                contract,
                "provider projection does not match compiled contract",
                reason="provider_projection_contract_mismatch",
            )
        if projection.provider != self.config.provider:
            raise self._projection_ineligible_error(
                contract,
                "provider projection does not match client provider",
                reason="provider_projection_identity_mismatch",
            )
        if not self.supports_structured_output_projection(projection):
            raise self._projection_ineligible_error(
                contract,
                "OpenAI-compatible adapter does not implement constrained decoding",
                reason="provider_adapter_mapping_unsupported",
            )
        return (
            request.with_structured_output_execution(
                contract=contract,
                projection=projection,
            ),
            contract,
            projection,
        )

    def _provider_projection_error(
        self,
        error: LLMStructuredOutputProjectionError,
    ) -> LLMProviderError:
        return LLMProviderError(
            f"{self.config.provider} structured output projection is ineligible",
            provider=self.config.provider,
            model=self.config.model,
            error_type="provider_schema_ineligible",
            retryable=False,
            diagnostics=(item.to_dict() for item in error.diagnostics),
        )

    @staticmethod
    def _projection_ineligible_error(
        contract: StructuredOutputContract,
        message: str,
        *,
        reason: str,
    ) -> LLMStructuredOutputProjectionError:
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

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        return self._normalize_response(
            payload,
            request=LLMRequest(messages=[]),
            attempts=1,
            contract=None,
            projection=None,
        )

    def _build_http_request(self, api_key: str, payload: dict[str, Any]) -> Request:
        return Request(
            url=f"{self.config.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

    def _parse_response_payload(self, raw_body: bytes, *, attempts: int) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"{self.config.provider} response payload is not valid JSON",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            ) from exc
        if not isinstance(payload, dict):
            raise LLMProviderError(
                f"{self.config.provider} response payload is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        return payload

    def _parse_stream_chunk(self, data: str) -> dict[str, Any]:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise LLMProviderError(
                f"{self.config.provider} stream chunk is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=1,
            )
        return payload

    def _normalize_response(
        self,
        payload: dict[str, Any],
        *,
        request: LLMRequest,
        attempts: int,
        contract: StructuredOutputContract | None,
        projection: ProviderSchemaProjection | None,
    ) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMProviderError(
                f"{self.config.provider} response missing choices",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMProviderError(
                f"{self.config.provider} response choice is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise LLMProviderError(
                f"{self.config.provider} response message is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        try:
            tool_calls = parse_openai_tool_calls(message.get("tool_calls"), request.tools)
        except LLMToolCallParseError as exc:
            raise LLMProviderError(
                f"{self.config.provider} tool call parse failed: {exc}",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            ) from exc

        content = message.get("content")
        if content is None and tool_calls:
            content = ""
        if not isinstance(content, str):
            raise LLMProviderError(
                f"{self.config.provider} response missing message content",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        structured_output = None
        if _expects_structured_output(request):
            structured_output = self._parse_structured_output(
                content,
                request=request,
                attempts=attempts,
                contract=contract,
            )

        usage_payload = payload.get("usage") or {}
        if not isinstance(usage_payload, dict):
            usage_payload = {}
        try:
            usage = TokenUsage(
                input_tokens=int(usage_payload.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage_payload.get("completion_tokens", 0) or 0),
                reasoning_tokens=int(
                    (usage_payload.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens",
                        0,
                    )
                    or 0
                )
                if isinstance(usage_payload.get("completion_tokens_details"), dict)
                else 0,
                cached_input_tokens=int(
                    (usage_payload.get("prompt_tokens_details") or {}).get(
                        "cached_tokens",
                        0,
                    )
                    or 0
                )
                if isinstance(usage_payload.get("prompt_tokens_details"), dict)
                else 0,
            )
        except (TypeError, ValueError) as exc:
            raise LLMProviderError(
                f"{self.config.provider} response usage is invalid",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            ) from exc
        validation_metadata = self._structured_output_validation_metadata(
            request,
            contract=contract,
            projection=projection,
            structured_output=structured_output,
        )
        return LLMResponse(
            content=content,
            usage=usage,
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "response_id": payload.get("id"),
                "attempts": attempts,
                "retry_count": attempts - 1,
                **validation_metadata,
            },
            structured_output=structured_output,
            tool_calls=tool_calls,
            model=self.config.model,
            raw=dict(payload),
            execution_identity=request.execution_identity,
        )

    def _parse_structured_output(
        self,
        content: str,
        *,
        request: LLMRequest,
        attempts: int,
        contract: StructuredOutputContract | None,
    ) -> dict[str, Any]:
        try:
            structured_output = decode_structured_output(
                content,
                limits=contract.limits if contract is not None else None,
            )
        except LLMStructuredOutputParseError as exc:
            raise LLMProviderError(
                f"{self.config.provider} structured output failed strict JSON decoding",
                provider=self.config.provider,
                model=self.config.model,
                error_type="structured_output_parse_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
                response_fingerprint=structured_output_text_fingerprint(content),
            ) from exc
        if contract is not None:
            try:
                structured_output = validate_compiled_structured_output(
                    structured_output,
                    contract,
                )
            except LLMStructuredOutputValidationError as exc:
                raise LLMProviderError(
                    f"{self.config.provider} structured output failed schema validation: {exc}",
                    provider=self.config.provider,
                    model=self.config.model,
                    error_type="structured_output_validation_error",
                    retryable=False,
                    attempts=attempts,
                    diagnostics=(item.to_dict() for item in exc.diagnostics),
                    response_fingerprint=structured_output_response_fingerprint(
                        structured_output
                    ),
                ) from exc
        return structured_output

    @staticmethod
    def _structured_output_validation_metadata(
        request: LLMRequest,
        *,
        contract: StructuredOutputContract | None,
        projection: ProviderSchemaProjection | None,
        structured_output: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not _expects_structured_output(request):
            return {}
        return {
            "structured_output_validation": {
                "validated": structured_output is not None,
                "schema_name": (
                    request.output_schema_name
                    if request.output_schema is not None
                    else None
                ),
                "schema_digest": (
                    contract.schema_digest if contract is not None else None
                ),
                "schema_revision": (
                    contract.schema_revision if contract is not None else None
                ),
                "schema_dialect": contract.dialect if contract is not None else None,
                "typed_adapter_revision": (
                    contract.typed_adapter_revision
                    if contract is not None
                    else None
                ),
                "projection_digest": (
                    projection.projection_digest
                    if projection is not None
                    else None
                ),
                "projection_mode": (
                    projection.mode if projection is not None else None
                ),
                "provider_capability_revision": (
                    projection.provider_capability_revision
                    if projection is not None
                    else None
                ),
                "provider_enforced_keywords": (
                    sorted(projection.enforced_keywords)
                    if projection is not None
                    else []
                ),
                "provider_omitted_keywords": (
                    sorted(projection.omitted_keywords)
                    if projection is not None
                    else []
                ),
                "provider_native_json_mode": _uses_provider_native_json_mode(
                    request
                ),
                "response_fingerprint": (
                    structured_output_response_fingerprint(structured_output)
                    if structured_output is not None
                    else None
                ),
            }
        }

    def _error_from_http(self, exc: HTTPError, *, attempts: int) -> LLMProviderError:
        status_code = int(exc.code)
        provider_payload = _read_bounded_http_error_payload(exc)
        provider_error_code = _provider_error_code(provider_payload)
        if status_code == 413 or provider_error_code in _CONTEXT_OVERFLOW_CODES:
            limit_tokens, usage_tokens = _provider_overflow_token_diagnostics(
                provider_payload
            )
            return LLMProviderContextOverflow(
                f"{self.config.provider} request exceeded provider context capacity",
                provider=self.config.provider,
                model=self.config.model,
                status_code=status_code,
                attempts=attempts,
                provider_error_code=provider_error_code,
                provider_reported_limit_tokens=limit_tokens,
                provider_reported_usage_tokens=usage_tokens,
            )
        retryable = status_code in self._retry_policy.retryable_status_codes
        error_type = _error_type_from_http_status(status_code)
        return LLMProviderError(
            f"{self.config.provider} request failed: HTTP {status_code}",
            provider=self.config.provider,
            model=self.config.model,
            error_type=error_type,
            retryable=retryable,
            status_code=status_code,
            attempts=attempts,
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
        )


def _error_type_from_http_status(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code in (401, 403):
        return "auth_error"
    if status_code == 404:
        return "unsupported_model"
    if status_code == 408:
        return "timeout"
    if status_code == 409:
        return "transient_network"
    if status_code == 413:
        return "context_length"
    if status_code == 429:
        return "rate_limit"
    if 500 <= status_code <= 599:
        return "server_error"
    if 400 <= status_code <= 499:
        return "invalid_request"
    return "unknown_llm_error"


def _canonical_error_category(error_type: str) -> str:
    aliases = {
        "rate_limited": "rate_limit",
        "provider_timeout": "timeout",
        "provider_connection_error": "transient_network",
        "temporary_provider_error": "transient_network",
        "provider_server_error": "server_error",
        "invalid_api_key": "auth_error",
        "invalid_request_schema": "invalid_request",
        "context_length_exceeded": "context_length",
        "invalid_model": "unsupported_model",
        "provider_response_shape_invalid": "schema_error",
        "provider_stream_chunk_invalid": "schema_error",
        "tool_call_parse_error": "schema_error",
        "stream_tool_call_parse_error": "schema_error",
        "structured_output_parse_error": "schema_error",
        "provider_schema_ineligible": "schema_error",
        "structured_output_schema_error": "schema_error",
        "structured_output_validation_error": "schema_error",
    }
    return aliases.get(error_type, error_type)


_MAX_PROVIDER_ERROR_BODY_BYTES = 64 * 1024
_MAX_PROVIDER_TOKEN_DIAGNOSTIC = 2_000_000_000
_CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "max_context_length_exceeded",
}
_PROVIDER_LIMIT_TOKEN_KEYS = (
    "context_limit_tokens",
    "context_window_tokens",
    "limit_tokens",
    "max_context_tokens",
    "max_context_length",
)
_PROVIDER_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "requested_tokens",
    "total_tokens",
)


def _read_bounded_http_error_payload(exc: HTTPError) -> dict[str, Any] | None:
    try:
        raw_body = exc.read(_MAX_PROVIDER_ERROR_BODY_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw_body, bytes) or len(raw_body) > _MAX_PROVIDER_ERROR_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _provider_error_code(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    error = payload.get("error")
    raw_code = error.get("code") if isinstance(error, dict) else payload.get("code")
    if not isinstance(raw_code, str):
        return None
    code = raw_code.strip().casefold()
    if not code or len(code) > 128:
        return None
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in code):
        return None
    return code


def _provider_overflow_token_diagnostics(
    payload: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    if payload is None:
        return None, None
    error = payload.get("error")
    sources: list[dict[str, Any]] = [payload]
    if isinstance(error, dict):
        sources.insert(0, error)
        details = error.get("details")
        if isinstance(details, dict):
            sources.insert(0, details)
    return (
        _first_bounded_token_value(sources, _PROVIDER_LIMIT_TOKEN_KEYS),
        _first_bounded_token_value(sources, _PROVIDER_USAGE_TOKEN_KEYS),
    )


def _first_bounded_token_value(
    sources: Iterable[dict[str, Any]],
    keys: tuple[str, ...],
) -> int | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if 0 <= value <= _MAX_PROVIDER_TOKEN_DIAGNOSTIC:
                return value
    return None


def _expects_structured_output(request: LLMRequest) -> bool:
    if request.output_schema is not None:
        return True
    response_format = request.response_format
    if isinstance(response_format, str):
        return response_format in {"json_object", "json_schema"}
    if isinstance(response_format, dict):
        return response_format.get("type") in {"json_object", "json_schema"}
    return False


def _uses_provider_native_json_mode(request: LLMRequest) -> bool:
    response_format = openai_response_format(request)
    if not isinstance(response_format, dict):
        return False
    return response_format.get("type") in {"json_object", "json_schema"}


class _OpenAIStreamToolCallAssembler:
    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self._tools = tools
        self._calls: dict[int, dict[str, Any]] = {}

    def add_deltas(self, raw_tool_calls: Any) -> None:
        if raw_tool_calls in (None, []):
            return
        if not isinstance(raw_tool_calls, list):
            raise LLMToolCallParseError("provider streaming tool_calls must be a list")
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                raise LLMToolCallParseError("provider streaming tool_call must be an object")
            index = int(raw_tool_call.get("index", len(self._calls)))
            current = self._calls.setdefault(
                index,
                {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if raw_tool_call.get("id"):
                current["id"] = str(raw_tool_call["id"])
            if raw_tool_call.get("type"):
                current["type"] = str(raw_tool_call["type"])
            function = raw_tool_call.get("function") or {}
            if function and not isinstance(function, dict):
                raise LLMToolCallParseError("provider streaming tool_call function must be an object")
            current_function = current["function"]
            name_delta = function.get("name")
            if name_delta:
                current_function["name"] = f"{current_function.get('name', '')}{name_delta}"
            args_delta = function.get("arguments")
            if args_delta:
                current_function["arguments"] = (
                    f"{current_function.get('arguments', '')}{args_delta}"
                )

    def complete(self):
        raw_calls = []
        for index in sorted(self._calls):
            raw_call = self._calls[index]
            function = raw_call.get("function") or {}
            raw_calls.append(
                {
                    "id": raw_call.get("id") or f"tool_call_{index + 1}",
                    "type": raw_call.get("type") or "function",
                    "function": {
                        "name": function.get("name") or "",
                        "arguments": function.get("arguments") or "{}",
                    },
                }
            )
        return parse_openai_tool_calls(raw_calls, self._tools)


def _events_from_stream_chunk(
    chunk: dict[str, Any],
    *,
    assembler: _OpenAIStreamToolCallAssembler,
    provider: str,
    attempts: int,
    execution_identity: Any = None,
) -> list[LLMStreamEvent]:
    choices = chunk.get("choices") or []
    if not isinstance(choices, list):
            raise LLMProviderError(
                f"{provider} stream chunk choices are invalid",
                provider=provider,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
    events: list[LLMStreamEvent] = []
    for choice in choices:
        if not isinstance(choice, dict):
            raise LLMProviderError(
                f"{provider} stream chunk choice is invalid",
                provider=provider,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise LLMProviderError(
                f"{provider} stream chunk delta is invalid",
                provider=provider,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
            )
        content = delta.get("content")
        if isinstance(content, str) and content:
            events.append(
                LLMStreamEvent(
                    event_type="text_delta",
                    text_delta=content,
                    execution_identity=execution_identity,
                )
            )
        assembler.add_deltas(delta.get("tool_calls"))
        finish_reason = choice.get("finish_reason")
        if finish_reason == "tool_calls":
            for tool_call in assembler.complete():
                events.append(
                    LLMStreamEvent(
                        event_type="tool_call_complete",
                        tool_call=tool_call,
                        execution_identity=execution_identity,
                    )
                )
        if finish_reason:
            events.append(
                LLMStreamEvent(
                    event_type="message_complete",
                    metadata={"finish_reason": str(finish_reason)},
                    execution_identity=execution_identity,
                )
            )
    return events


def _with_provisional_metadata(
    event: LLMStreamEvent,
    *,
    provisional: bool,
) -> LLMStreamEvent:
    metadata = dict(event.metadata)
    metadata["provisional"] = provisional
    return replace(event, metadata=metadata)


def _usage_from_stream_chunk(chunk: dict[str, Any]) -> TokenUsage | None:
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        reasoning_tokens=int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        )
        if isinstance(usage.get("completion_tokens_details"), dict)
        else 0,
        cached_input_tokens=int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        )
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else 0,
    )


def _stream_line_text(raw_line: bytes | str) -> str:
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8").strip()
    return str(raw_line).strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _urlopen_transport(request: Request, timeout_seconds: float) -> bytes:
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def _urlopen_stream_transport(request: Request, timeout_seconds: float) -> Iterable[bytes]:
    with urlopen(request, timeout=timeout_seconds) as response:
        yield from response
    project_structured_output_contract,
