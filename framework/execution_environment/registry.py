from __future__ import annotations

import hashlib

from framework.shared.json import stable_json_dumps

from framework.execution_environment.errors import (
    ExecutionEnvironmentUnavailableError,
    ExecutionIdentityMismatchError,
)
from framework.execution_environment.models import (
    CAPABILITY_DENIAL_CODE_VERSION,
    ExecutionOutcome,
    ExecutionRequest,
    capability_denial_code,
)
from framework.execution_environment.ports import (
    ExecutionCancellationSignal,
    ExecutionEnvironmentPort,
)


class ExecutionEnvironmentRegistry:
    """Fail-closed provider registry selected by pinned profile provider id."""

    def __init__(self) -> None:
        self._providers: dict[str, ExecutionEnvironmentPort] = {}

    def register(self, provider: ExecutionEnvironmentPort) -> None:
        if not isinstance(provider, ExecutionEnvironmentPort):
            raise TypeError("provider must implement ExecutionEnvironmentPort")
        provider_id = provider.capabilities.provider_id
        if provider_id in self._providers:
            raise ValueError(f"execution environment provider is already registered: {provider_id}")
        self._providers[provider_id] = provider

    def resolve(
        self,
        request: ExecutionRequest,
        *,
        require_cancellation: bool = False,
    ) -> ExecutionEnvironmentPort:
        if not isinstance(require_cancellation, bool):
            raise TypeError("require_cancellation must be boolean")
        provider_id = request.profile.provider_id
        if provider_id is None:
            raise ExecutionEnvironmentUnavailableError(
                "sandboxed execution profile has no pinned provider",
                details={
                    "missing": ["provider_id"],
                    "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
                    "denial_code": "execution_provider_unavailable",
                    "denials": [
                        {
                            "capability": "provider_unavailable",
                            "denial_code": capability_denial_code("provider_unavailable"),
                        }
                    ],
                },
            )
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ExecutionEnvironmentUnavailableError(
                "requested execution environment provider is not registered",
                details={
                    "provider_id": provider_id,
                    "missing": ["provider"],
                    "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
                    "denial_code": "execution_provider_unavailable",
                    "denials": [
                        {
                            "capability": "provider_unavailable",
                            "denial_code": capability_denial_code("provider_unavailable"),
                        }
                    ],
                },
            )
        diagnostics = provider.capabilities.admission_diagnostics(
            request,
            require_cancellation=require_cancellation,
        )
        if diagnostics["missing"]:
            raise ExecutionEnvironmentUnavailableError(
                "requested execution environment capabilities are unavailable",
                details=diagnostics,
            )
        return provider

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: ExecutionCancellationSignal | None = None,
    ) -> ExecutionOutcome:
        if cancellation is not None and not isinstance(
            cancellation,
            ExecutionCancellationSignal,
        ):
            raise TypeError("cancellation must implement ExecutionCancellationSignal")
        provider = self.resolve(
            request,
            require_cancellation=cancellation is not None,
        )
        outcome = provider.execute(request, cancellation=cancellation)
        if not isinstance(outcome, ExecutionOutcome):
            raise TypeError("execution environment provider returned an invalid outcome")
        if not outcome.receipt.matches_request(request):
            raise ExecutionIdentityMismatchError(
                details={
                    "execution_id": request.execution_id,
                    "provider_id": provider.capabilities.provider_id,
                }
            )
        if outcome.receipt.provider_capability_checksum != provider.capabilities.checksum:
            raise ExecutionIdentityMismatchError(
                "execution receipt capability checksum does not match the admitted provider",
                details={
                    "provider_id": provider.capabilities.provider_id,
                    "expected_capability_checksum": provider.capabilities.checksum,
                    "received_capability_checksum": outcome.receipt.provider_capability_checksum,
                },
            )
        # A provider receipt is the integrity boundary for output material.
        # Empty output is still checked: a provider must not smuggle a stale
        # non-zero size/digest through the ``no output`` branch.
        if outcome.output in (None, b"", ""):
            if outcome.output is None and (
                outcome.receipt.output_checksum is not None
                or outcome.receipt.output_bytes is not None
            ):
                raise ExecutionIdentityMismatchError(
                    "execution receipt declares inline output that was not returned",
                    details={"execution_id": request.execution_id},
                )
            if outcome.receipt.output_bytes not in (None, 0):
                raise ExecutionIdentityMismatchError(
                    "execution receipt declares bytes for empty provider output",
                    details={
                        "execution_id": request.execution_id,
                        "received_output_bytes": outcome.receipt.output_bytes,
                    },
                )
            if outcome.output is not None and outcome.receipt.output_checksum not in (
                None,
                "sha256:" + hashlib.sha256(b"").hexdigest(),
            ):
                raise ExecutionIdentityMismatchError(
                    "execution receipt empty-output checksum does not match provider output",
                    details={
                        "execution_id": request.execution_id,
                        "received_output_checksum": outcome.receipt.output_checksum,
                    },
                )
        else:
            output_bytes = (
                outcome.output
                if isinstance(outcome.output, bytes)
                else str(outcome.output).encode("utf-8")
            )
            expected_checksum = "sha256:" + hashlib.sha256(output_bytes).hexdigest()
            if outcome.receipt.output_checksum != expected_checksum:
                raise ExecutionIdentityMismatchError(
                    "execution receipt output checksum does not match provider output",
                    details={
                        "execution_id": request.execution_id,
                        "expected_output_checksum": expected_checksum,
                        "received_output_checksum": outcome.receipt.output_checksum,
                    },
                )
            if outcome.receipt.output_bytes != len(output_bytes):
                raise ExecutionIdentityMismatchError(
                    "execution receipt output size does not match provider output",
                    details={
                        "execution_id": request.execution_id,
                        "expected_output_bytes": len(output_bytes),
                        "received_output_bytes": outcome.receipt.output_bytes,
                    },
                )
        return outcome

    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def resolve_capabilities(self, provider_id: str):
        """Return the advertised capabilities without admitting a request."""

        normalized = str(provider_id).strip()
        provider = self._providers.get(normalized)
        if provider is None:
            raise ExecutionEnvironmentUnavailableError(
                "requested execution environment provider is not registered",
                details={
                    "provider_id": normalized,
                    "missing": ["provider"],
                    "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
                    "denial_code": capability_denial_code("provider_unavailable"),
                    "denials": [
                        {
                            "capability": "provider_unavailable",
                            "denial_code": capability_denial_code("provider_unavailable"),
                        }
                    ],
                },
            )
        return provider.capabilities

    @property
    def fingerprint(self) -> str:
        """Stable fingerprint of provider identities and advertised capabilities."""

        payload = [
            {
                "provider_id": provider_id,
                "capability_checksum": self._providers[provider_id].capabilities.checksum,
            }
            for provider_id in self.provider_ids()
        ]
        return "sha256:" + hashlib.sha256(
            stable_json_dumps(payload).encode("utf-8")
        ).hexdigest()


__all__ = ["ExecutionEnvironmentRegistry"]
