"""Structured-output contract handling shared by every provider client.

A client asks this module to resolve the request's contract into a provider
projection before sending, and to decode and validate the returned text after.
The provider-specific part (how the projection is put on the wire, whether the
vendor enforced the schema natively) stays in the client.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from framework.llm.clients.provider_errors import LLMProviderError
from framework.llm.models import LLMRequest
from framework.llm.structured_output import (
    LLMStructuredOutputParseError,
    LLMStructuredOutputProjectionError,
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

StructuredExecution = tuple[
    LLMRequest,
    StructuredOutputContract | None,
    ProviderSchemaProjection | None,
]


@dataclass(frozen=True)
class StructuredOutputExecutor:
    provider: str
    model: str
    capability: ProviderStructuredOutputCapability | None
    supported_projection_modes: frozenset[str]
    # Whether the request as sent asks the vendor itself for JSON.
    native_json_mode: Callable[[LLMRequest], bool]
    adapter_name: str

    def resolve(self, request: LLMRequest, *, streaming: bool) -> StructuredExecution:
        contract = request.structured_output_contract()
        if contract is None:
            contract = _compile_contract(request)
        if contract is None:
            return request, None, None

        projection = request.provider_schema_projection()
        if projection is None:
            capability = self.capability
            if capability is None:
                raise _projection_ineligible(
                    contract,
                    "structured-output capability is not configured",
                    reason="provider_capability_missing",
                )
            if capability.provider != self.provider:
                raise _projection_ineligible(
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
            raise _projection_ineligible(
                contract,
                "provider projection does not match compiled contract",
                reason="provider_projection_contract_mismatch",
            )
        if projection.provider != self.provider:
            raise _projection_ineligible(
                contract,
                "provider projection does not match client provider",
                reason="provider_projection_identity_mismatch",
            )
        if projection.mode not in self.supported_projection_modes:
            raise _projection_ineligible(
                contract,
                f"{self.adapter_name} adapter does not implement constrained decoding",
                reason="provider_adapter_mapping_unsupported",
            )
        return (
            request.with_structured_output_execution(contract=contract, projection=projection),
            contract,
            projection,
        )

    def projection_error(self, error: LLMStructuredOutputProjectionError) -> LLMProviderError:
        return LLMProviderError(
            f"{self.provider} structured output projection is ineligible",
            provider=self.provider,
            model=self.model,
            error_type="provider_schema_ineligible",
            retryable=False,
            diagnostics=(item.to_dict() for item in error.diagnostics),
        )

    def parse(
        self,
        content: str,
        *,
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
                f"{self.provider} structured output failed strict JSON decoding",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_parse_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
                response_fingerprint=structured_output_text_fingerprint(content),
            ) from exc
        if contract is None:
            return structured_output
        try:
            return validate_compiled_structured_output(structured_output, contract)
        except LLMStructuredOutputValidationError as exc:
            raise LLMProviderError(
                f"{self.provider} structured output failed schema validation: {exc}",
                provider=self.provider,
                model=self.model,
                error_type="structured_output_validation_error",
                retryable=False,
                attempts=attempts,
                diagnostics=(item.to_dict() for item in exc.diagnostics),
                response_fingerprint=structured_output_response_fingerprint(structured_output),
            ) from exc

    def validation_metadata(
        self,
        request: LLMRequest,
        *,
        contract: StructuredOutputContract | None,
        projection: ProviderSchemaProjection | None,
        structured_output: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not expects_structured_output(request):
            return {}
        return {
            "structured_output_validation": {
                "validated": structured_output is not None,
                "schema_name": request.output_schema_name if request.output_schema is not None else None,
                "schema_digest": contract.schema_digest if contract is not None else None,
                "schema_revision": contract.schema_revision if contract is not None else None,
                "schema_dialect": contract.dialect if contract is not None else None,
                "typed_adapter_revision": (
                    contract.typed_adapter_revision if contract is not None else None
                ),
                "projection_digest": projection.projection_digest if projection is not None else None,
                "projection_mode": projection.mode if projection is not None else None,
                "provider_capability_revision": (
                    projection.provider_capability_revision if projection is not None else None
                ),
                "provider_enforced_keywords": (
                    sorted(projection.enforced_keywords) if projection is not None else []
                ),
                "provider_omitted_keywords": (
                    sorted(projection.omitted_keywords) if projection is not None else []
                ),
                "provider_native_json_mode": self.native_json_mode(request),
                "response_fingerprint": (
                    structured_output_response_fingerprint(structured_output)
                    if structured_output is not None
                    else None
                ),
            }
        }


def expects_structured_output(request: LLMRequest) -> bool:
    if request.output_schema is not None:
        return True
    response_format = request.response_format
    if isinstance(response_format, str):
        return response_format in {"json_object", "json_schema"}
    if isinstance(response_format, dict):
        return response_format.get("type") in {"json_object", "json_schema"}
    return False


def _compile_contract(request: LLMRequest) -> StructuredOutputContract | None:
    if request.output_schema is None:
        return None
    return compile_structured_output_contract(
        request.structured_output_schema_source(),
        schema_name=request.output_schema_name,
    )


def _projection_ineligible(
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


__all__ = ["StructuredExecution", "StructuredOutputExecutor", "expects_structured_output"]
