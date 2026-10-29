from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from itertools import chain
from typing import Any, get_args
from urllib.error import HTTPError, URLError
from urllib.request import Request

from framework.llm.clients.endpoint import (
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    llm_credential_mode,
    resolve_invocation_endpoint,
)
from framework.llm.context.openai import (
    OpenAIChatDialect,
    build_openai_chat_payload,
    openai_response_format,
)
from framework.llm.models import LLMRequest, LLMResponse, TokenUsage
from framework.llm.models.reasoning import ReasoningEffort
from framework.shared.secret_text import SecretText
from framework.llm.models import LLMStreamEvent
from framework.llm.structured_output import (
    LLMStructuredOutputProjectionError,
    LLMStructuredOutputSchemaError,
    ProviderSchemaProjection,
    ProviderStructuredOutputCapability,
    StructuredOutputContract,
)
from framework.llm.structured_output.projection import ProviderSchemaProjectionMode
from framework.llm.structured_output.request_adapters import (
    require_structured_output_request_adapter,
    structured_output_request_adapter,
)
from framework.llm.clients.provider_errors import (
    CONTEXT_OVERFLOW_CODES,
    LLMConfigurationError,
    LLMProviderContextOverflow,
    LLMProviderError,
    decode_provider_error_payload,
    error_type_from_http_status,
    network_failure_diagnostic,
    payload_error_code,
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
    parse_openai_tool_calls,
)
from framework.llm.clients.transport import (
    LLMRetryPolicy,
    Sleep,
    StreamTransport,
    Transport,
    urlopen_stream_transport,
    urlopen_transport,
)


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """Endpoint, model and credential source of one OpenAI Chat Completions deployment.

    The credential comes from exactly one place: ``api_key`` (a per-user key held
    in memory) or ``api_key_env`` (an operator environment variable, used by
    evaluation scripts and ``configs/models.yaml``).
    """

    provider: str
    base_url: str
    model: str
    api_key_env: str | None = None
    timeout_seconds: float = 90.0
    api_key: SecretText | None = field(default=None, repr=False)
    dialect: OpenAIChatDialect = field(default_factory=OpenAIChatDialect)
    reasoning_effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if self.api_key is not None and not isinstance(self.api_key, SecretText):
            object.__setattr__(self, "api_key", SecretText(self.api_key))
        if (self.api_key is None) == (not self.api_key_env):
            raise LLMConfigurationError("exactly one of api_key or api_key_env must be configured")
        if not isinstance(self.dialect, OpenAIChatDialect):
            raise LLMConfigurationError("dialect must be an OpenAIChatDialect")
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", ReasoningEffort.parse(self.reasoning_effort))

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
        if self.api_key is not None:
            return self.api_key.reveal()
        api_key = os.environ.get(self.api_key_env or "")
        if not api_key:
            raise LLMConfigurationError(f"missing required environment variable: {self.api_key_env}")
        return api_key


# Every projection mode an OpenAI-style request adapter can put on the wire.
_SUPPORTED_PROJECTION_MODES = frozenset(
    mode
    for mode in get_args(ProviderSchemaProjectionMode)
    if structured_output_request_adapter(mode) is not None
)


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
        credential_mode: LLMCredentialMode | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or urlopen_transport
        self._stream_transport = stream_transport or urlopen_stream_transport
        self._retry_policy = retry_policy or LLMRetryPolicy()
        self._sleep = sleep or time.sleep
        self._credential_mode = (
            LLMCredentialMode(credential_mode)
            if credential_mode is not None
            else llm_credential_mode()
        )
        self._invocation_api_key: str | None = None
        self._structured = self._structured_executor(structured_output_capability)

    @property
    def credential_mode(self) -> LLMCredentialMode:
        return self._credential_mode

    def _structured_executor(
        self,
        capability: ProviderStructuredOutputCapability | None,
    ) -> StructuredOutputExecutor:
        return StructuredOutputExecutor(
            provider=self.config.provider,
            model=self.config.model,
            capability=capability,
            supported_projection_modes=_SUPPORTED_PROJECTION_MODES,
            native_json_mode=_uses_provider_native_json_mode,
            adapter_name="OpenAI-compatible",
            require_own_capability=self._credential_mode is LLMCredentialMode.INVOCATION,
        )

    @staticmethod
    def supports_structured_output_projection(
        projection: ProviderSchemaProjection,
    ) -> bool:
        return projection.mode in _SUPPORTED_PROJECTION_MODES

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self._for_invocation()._complete(request)

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        # Bind eagerly so the endpoint is taken from the caller's context, not
        # from whichever context first advances the returned iterator.
        return self._for_invocation()._stream(request)

    def list_models(self) -> tuple[str, ...]:
        """Return the model ids the provider lists for this credential.

        ``GET /models`` authenticates the key without generating tokens, so
        it costs nothing and its latency does not depend on how slowly the
        model itself answers. Exactly one request is sent; failures raise
        ``LLMProviderError`` like a completion would.
        """

        return self._for_invocation()._list_models()

    def _for_invocation(self) -> OpenAICompatibleClient:
        """Return the client that serves exactly one provider call.

        In invocation mode the shared client is never mutated: the endpoint
        bound to the current context is applied to a shallow copy, so every
        ``self.config`` reference inside the call (URL, model, diagnostics)
        names the caller's endpoint while transports and policies are shared.
        """

        if self._credential_mode is LLMCredentialMode.ENVIRONMENT:
            return self
        if self._invocation_api_key is not None:
            return self
        endpoint = resolve_invocation_endpoint()
        bound = copy.copy(self)
        bound.config = replace(
            self.config,
            provider=endpoint.provider or self.config.provider,
            base_url=endpoint.base_url,
            model=endpoint.model,
        )
        # The operator deployment's structured-output capability describes a
        # different provider and model; the account endpoint carries its own,
        # and an endpoint that declares none cannot serve structured output.
        bound._structured = bound._structured_executor(endpoint.structured_output_capability)
        bound._invocation_api_key = endpoint.api_key
        return bound

    def _resolve_api_key(self) -> str:
        if self._credential_mode is LLMCredentialMode.ENVIRONMENT:
            return self.config.resolve_api_key()
        if self._invocation_api_key is None:
            raise LLMCredentialUnavailableError(
                "model_credential_required",
                "No model credential is bound to this request",
            )
        return self._invocation_api_key

    def _complete(self, request: LLMRequest) -> LLMResponse:
        try:
            execution_request, contract, projection = (
                self._structured.resolve(
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
            raise self._structured.projection_error(exc) from exc

        api_key = self._resolve_api_key()

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
                response_payload, response_raw_body = self._parse_response_payload(
                    raw_body,
                    attempts=attempt,
                )
                try:
                    return self._normalize_response(
                        response_payload,
                        request=execution_request,
                        attempts=attempt,
                        contract=contract,
                        projection=projection,
                        response_raw_body=response_raw_body,
                    )
                except LLMProviderError as exc:
                    if exc.error_type != "structured_output_empty":
                        raise
                    error = exc

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

    def _stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        try:
            execution_request, contract, projection = (
                self._structured.resolve(
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
            raise self._structured.projection_error(exc) from exc
        payload["stream"] = True
        api_key = self._resolve_api_key()

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

        structured_requested = expects_structured_output(execution_request)
        yield LLMStreamEvent(
            event_type="message_start",
            metadata={
                "provider": self.config.provider,
                "model": self.config.model,
                "provisional": structured_requested,
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
                        metadata={"provisional": structured_requested},
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
                        provisional=structured_requested,
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
        if structured_requested:
            structured_output = self._parse_structured_output(
                "".join(text_parts),
                request=execution_request,
                attempts=1,
                contract=contract,
            )
        validation_metadata = self._structured.validation_metadata(
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
                "structured_output_requested": structured_requested,
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
        # An account-bound endpoint names the only model the account agreed
        # to pay for, so a route-level request model never overrides it.
        if self._credential_mode is LLMCredentialMode.INVOCATION:
            model = self.config.model
        else:
            model = request.model or self.config.model
        return build_openai_chat_payload(
            request,
            model=model,
            dialect=self.config.dialect,
            default_reasoning_effort=self.config.reasoning_effort,
        )

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        return self._normalize_response(
            payload,
            request=LLMRequest(messages=[]),
            attempts=1,
            contract=None,
            projection=None,
        )

    def _parse_structured_output(
        self,
        content: str,
        *,
        request: LLMRequest,
        attempts: int,
        contract: StructuredOutputContract | None,
    ) -> dict[str, Any]:
        projection = request.provider_schema_projection()
        if (
            not content.strip()
            and projection is not None
            and require_structured_output_request_adapter(projection).empty_content_retryable
        ):
            # JSON-object mode can come back empty; that is a transient
            # provider outcome, not a contract violation.
            raise LLMProviderError(
                f"{self.config.provider} returned empty structured output content",
                provider=self.config.provider,
                model=self.config.model,
                error_type="structured_output_empty",
                retryable=True,
                attempts=attempts,
            )
        return self._structured.parse(content, attempts=attempts, contract=contract)

    def _list_models(self) -> tuple[str, ...]:
        api_key = self._resolve_api_key()
        http_request = Request(
            url=f"{self.config.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        try:
            raw_body = self._transport(http_request, self.config.timeout_seconds)
        except HTTPError as exc:
            raise self._error_from_http(exc, attempts=1) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise self._error_from_network(exc, attempts=1) from exc
        payload, _ = self._parse_response_payload(raw_body, attempts=1)
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise LLMProviderError(
                f"{self.config.provider} model list has no data array",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
            )
        return tuple(
            str(entry["id"])
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
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

    def _parse_response_payload(
        self,
        raw_body: bytes,
        *,
        attempts: int,
    ) -> tuple[dict[str, Any], bytes]:
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
                diagnostics=(
                    provider_failure_diagnostic(
                        raw_body=raw_body,
                        stage="response_json_decode",
                    ),
                ),
            ) from exc
        if not isinstance(payload, dict):
            raise LLMProviderError(
                f"{self.config.provider} response payload is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(
                    provider_failure_diagnostic(
                        payload=payload,
                        raw_body=raw_body,
                        stage="response_shape",
                    ),
                ),
            )
        return payload, raw_body

    def _parse_stream_chunk(self, data: str) -> dict[str, Any]:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(
                f"{self.config.provider} stream chunk is not valid JSON",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=1,
                diagnostics=(
                    provider_failure_diagnostic(
                        raw_body=data.encode("utf-8"),
                        stage="stream_json_decode",
                    ),
                ),
            ) from exc
        if not isinstance(payload, dict):
            raise LLMProviderError(
                f"{self.config.provider} stream chunk is not an object",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=1,
                diagnostics=(
                    provider_failure_diagnostic(
                        payload=payload,
                        raw_body=data.encode("utf-8"),
                        stage="stream_shape",
                    ),
                ),
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
        response_raw_body: bytes | None = None,
    ) -> LLMResponse:
        diagnostics: tuple[dict[str, Any], ...] | None = None

        def failure_diagnostics() -> tuple[dict[str, Any], ...]:
            nonlocal diagnostics
            if diagnostics is None:
                diagnostics = (
                    provider_failure_diagnostic(
                        payload=payload,
                        raw_body=response_raw_body or b"",
                        stage="response_shape",
                    ),
                )
            return diagnostics

        choices = payload.get("choices") or []
        if not choices:
            raise LLMProviderError(
                f"{self.config.provider} response missing choices",
                provider=self.config.provider,
                model=self.config.model,
                error_type="schema_error",
                retryable=False,
                attempts=attempts,
                diagnostics=failure_diagnostics(),
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
                diagnostics=failure_diagnostics(),
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
                diagnostics=failure_diagnostics(),
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
                diagnostics=failure_diagnostics(),
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
                diagnostics=failure_diagnostics(),
            )
        structured_output = None
        if expects_structured_output(request):
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
                diagnostics=failure_diagnostics(),
            ) from exc
        validation_metadata = self._structured.validation_metadata(
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

    def _error_from_http(self, exc: HTTPError, *, attempts: int) -> LLMProviderError:
        status_code = int(exc.code)
        raw_body, raw_body_truncated = read_bounded_http_error_body(exc)
        provider_payload = decode_provider_error_payload(raw_body, truncated=raw_body_truncated)
        provider_error_code = payload_error_code(provider_payload)
        if status_code == 413 or provider_error_code in CONTEXT_OVERFLOW_CODES:
            limit_tokens, usage_tokens = provider_overflow_token_diagnostics(
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
        error_type = error_type_from_http_status(status_code)
        diagnostic = provider_failure_diagnostic(
            payload=provider_payload,
            raw_body=raw_body,
            raw_body_truncated=raw_body_truncated,
            stage="http_error_response",
        )
        return LLMProviderError(
            f"{self.config.provider} request failed: HTTP {status_code}",
            provider=self.config.provider,
            model=self.config.model,
            error_type=error_type,
            retryable=retryable,
            status_code=status_code,
            attempts=attempts,
            diagnostics=(diagnostic,),
        )

    def _error_from_network(self, exc: TimeoutError | URLError | OSError, *, attempts: int) -> LLMProviderError:
        reason = getattr(exc, "reason", None)
        is_timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        error_type = "timeout" if is_timeout else "transient_network"
        diagnostic = network_failure_diagnostic(exc, reason=reason)
        return LLMProviderError(
            f"{self.config.provider} request failed: {error_type}",
            provider=self.config.provider,
            model=self.config.model,
            error_type=error_type,
            retryable=True,
            attempts=attempts,
            diagnostics=(diagnostic,),
        )


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

