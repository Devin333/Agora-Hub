"""One small completion that proves a connection's key, model and reasoning tier work."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from framework.llm.clients.provider_errors import LLMConfigurationError, LLMProviderError
from framework.llm.models.client import LLMClient
from framework.llm.models.reasoning import ReasoningEffort
from framework.llm.models.request import LLMRequest

PROBE_PROMPT = "Reply with the single word OK."
# Room for a short reasoning pass before the one-word answer.
PROBE_MAX_OUTPUT_TOKENS = 1024
PROBE_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ProbeOutcome:
    succeeded: bool
    latency_ms: int
    error_type: str | None = None
    retryable: bool = False
    status_code: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "succeeded": self.succeeded,
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


def probe_model(
    client: LLMClient,
    *,
    reasoning_effort: ReasoningEffort | None,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeOutcome:
    """Send the probe; a provider refusal becomes a failed outcome, never an exception.

    The outcome carries only the error category, so nothing the vendor echoed back
    (which can include the key) reaches the caller.
    """
    request = LLMRequest(
        messages=[{"role": "user", "content": PROBE_PROMPT}],
        max_tokens=PROBE_MAX_OUTPUT_TOKENS,
        reasoning_effort=reasoning_effort,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_transport_attempts=1,
        metadata={"purpose": "connection_probe"},
    )
    started = clock()
    try:
        client.complete(request)
    except LLMProviderError as exc:
        return ProbeOutcome(
            succeeded=False,
            latency_ms=_elapsed_ms(started, clock),
            error_type=exc.error_type,
            retryable=exc.retryable,
            status_code=exc.status_code,
        )
    except LLMConfigurationError:
        return ProbeOutcome(
            succeeded=False,
            latency_ms=_elapsed_ms(started, clock),
            error_type="configuration_error",
        )
    return ProbeOutcome(succeeded=True, latency_ms=_elapsed_ms(started, clock))


def _elapsed_ms(started: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - started) * 1000))


__all__ = ["PROBE_MAX_OUTPUT_TOKENS", "PROBE_PROMPT", "ProbeOutcome", "probe_model"]
