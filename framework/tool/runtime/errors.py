from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class ToolRuntimeError(RuntimeError):
    """Base exception for ToolRuntime failures."""


class ToolDefinitionError(ToolRuntimeError):
    """Raised when a tool definition is invalid."""


class ToolPermissionError(ToolRuntimeError):
    """Raised when an agent is not allowed to call a tool."""


class ToolTimeoutError(ToolRuntimeError):
    """Raised when a tool exceeds its execution timeout."""

    def __init__(
        self,
        message: str,
        *,
        attempt_id: str | None = None,
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        operation_kind: str | None = None,
        local_attempt_no: int | None = None,
        retry_credit_id: str | None = None,
        termination_confirmed: bool = False,
        indeterminate: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.idempotency_key = idempotency_key
        self.operation_id = operation_id
        self.operation_kind = operation_kind
        self.local_attempt_no = local_attempt_no
        self.retry_credit_id = retry_credit_id
        self.termination_confirmed = bool(termination_confirmed)
        self.indeterminate = (
            not self.termination_confirmed
            if indeterminate is None
            else bool(indeterminate)
        )


class ToolIndeterminateError(ToolRuntimeError):
    """Raised when an external Tool effect cannot be reconciled after failure."""

    def __init__(
        self,
        message: str,
        *,
        attempt_id: str,
        idempotency_key: str,
        operation_id: str,
        operation_kind: str,
        local_attempt_no: int,
        retry_credit_id: str | None,
        cause_type: str,
    ) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.idempotency_key = idempotency_key
        self.operation_id = operation_id
        self.operation_kind = operation_kind
        self.local_attempt_no = local_attempt_no
        self.retry_credit_id = retry_credit_id
        self.cause_type = cause_type
        self.termination_confirmed = True
        self.indeterminate = True


class ToolEffectNotAppliedError(ToolRuntimeError):
    """A side-effecting tool failed and its effect is known not to have happened.

    Raise this when the request was refused before anything was sent, or the
    receiver answered with a definite rejection. The executor reports it as a
    plain failure instead of an indeterminate effect, so callers know a new,
    corrected attempt cannot duplicate anything.
    """


@contextmanager
def effect_not_applied_on(
    *error_types: type[BaseException],
) -> Iterator[None]:
    """Report ``error_types`` raised inside the block as ``ToolEffectNotAppliedError``.

    Wrap the validation a write tool does before it touches anything external,
    so a rejected argument is not reported as a possibly-applied effect.
    """

    caught = error_types or (ValueError, TypeError, KeyError)
    try:
        yield
    except ToolEffectNotAppliedError:
        raise
    except caught as exc:
        raise ToolEffectNotAppliedError(str(exc)) from exc


class ToolSecretError(ToolRuntimeError):
    """Raised when a tool secret cannot be safely resolved."""
