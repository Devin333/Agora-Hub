"""Canonical planning attempts, shared by execution and offline replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum, checksum, exact_keys, identifier,
    non_negative_int, positive_int,
)


PLAN_BUILD_ATTEMPT_SCHEMA = "newsroom.harness-plan-build-attempt/v1"
PLAN_BUILD_INTENT = "PLAN_BUILD_INTENT"
PLAN_BUILD_RECEIPT = "PLAN_BUILD_RECEIPT"


@dataclass(frozen=True, slots=True)
class PlanBuildAttempt:
    """An immutable attempt intent or its terminal receipt.

    Attempt admission consumes one call even if no terminal receipt arrives.
    The request checksum binds approved inputs and the exact execution identity.
    """

    request_checksum: str
    policy_checksum: str
    attempt: int
    max_calls: int
    timeout_ms: int
    status: str = "STARTED"
    elapsed_ms: int = 0
    reason_code: str | None = None
    candidate_checksum: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        checksum(self.request_checksum, "request_checksum")
        checksum(self.policy_checksum, "policy_checksum")
        positive_int(self.attempt, "attempt")
        positive_int(self.max_calls, "max_calls")
        positive_int(self.timeout_ms, "timeout_ms")
        non_negative_int(self.elapsed_ms, "elapsed_ms")
        if not isinstance(self.retryable, bool) or (self.retryable and self.status != "FAILED"):
            raise _invalid("only failed planning attempts may be retryable")
        if self.attempt > self.max_calls:
            raise _invalid("planning attempt exceeds its call budget")
        if self.status not in {"STARTED", "SUCCEEDED", "FAILED", "TIMED_OUT"}:
            raise _invalid("planning attempt status is invalid")
        if self.status == "STARTED":
            if self.elapsed_ms or self.reason_code is not None or self.candidate_checksum is not None:
                raise _invalid("planning intent cannot carry result evidence")
        elif self.status == "SUCCEEDED":
            if self.reason_code is not None or self.candidate_checksum is None:
                raise _invalid("successful planning attempt requires candidate evidence")
            checksum(self.candidate_checksum, "candidate_checksum")
            if self.elapsed_ms > self.timeout_ms:
                raise _invalid("successful planning attempt exceeds deadline")
        else:
            if self.reason_code is None or self.candidate_checksum is not None:
                raise _invalid("failed planning attempt requires reason and no candidate")
            identifier(self.reason_code, "reason_code")

    @property
    def identity_checksum(self) -> str:
        return canonical_payload_checksum({
            "request_checksum": self.request_checksum,
            "policy_checksum": self.policy_checksum,
            "attempt": self.attempt,
            "max_calls": self.max_calls,
            "timeout_ms": self.timeout_ms,
        })

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": PLAN_BUILD_ATTEMPT_SCHEMA,
            "request_checksum": self.request_checksum,
            "policy_checksum": self.policy_checksum,
            "attempt": self.attempt,
            "max_calls": self.max_calls,
            "timeout_ms": self.timeout_ms,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "reason_code": self.reason_code,
            "candidate_checksum": self.candidate_checksum,
            "retryable": self.retryable,
        }
        return {**body, "receipt_checksum": canonical_payload_checksum(body)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlanBuildAttempt":
        value = exact_keys(payload, required=frozenset({
            "schema_version", "request_checksum", "policy_checksum", "attempt",
            "max_calls", "timeout_ms", "status", "elapsed_ms", "reason_code",
            "candidate_checksum", "receipt_checksum",
            "retryable",
        }), model="PlanBuildAttempt")
        if value.pop("schema_version") != PLAN_BUILD_ATTEMPT_SCHEMA:
            raise _invalid("planning attempt schema is unsupported")
        expected = value.pop("receipt_checksum")
        attempt = cls(**value)
        if attempt.to_dict()["receipt_checksum"] != expected:
            raise _invalid("planning attempt checksum mismatch")
        return attempt


def validate_build_history(events: tuple[Any, ...]) -> tuple[PlanBuildAttempt, ...]:
    """Reject missing intents, conflicting policies and post-success execution."""
    attempts: list[PlanBuildAttempt] = []
    pinned: tuple[str, str, int, int] | None = None
    built_candidates: set[str] = set()
    plan_accepted = False
    for event in events:
        if event.event_type == "PLAN_CANDIDATE_BUILT":
            built_candidates.add(event.payload.get("candidate_ref"))
        if event.event_type == "PLAN_ACCEPTED":
            plan_accepted = True
            if attempts and attempts[-1].status != "SUCCEEDED":
                raise _invalid("plan accepted without a successful planning receipt")
        if event.event_type not in {PLAN_BUILD_INTENT, PLAN_BUILD_RECEIPT}:
            continue
        if plan_accepted:
            raise _invalid("planning execution follows plan acceptance")
        if set(event.payload) != {"build_attempt"}:
            raise _invalid("planning event payload differs from its contract")
        current = PlanBuildAttempt.from_dict(event.payload["build_attempt"])
        binding = (current.request_checksum, current.policy_checksum, current.max_calls, current.timeout_ms)
        if pinned is not None and binding != pinned:
            raise _invalid("planning attempt changed its pinned request or budget")
        pinned = binding
        if event.plan_id is not None or event.task_id is not None or event.task_instance_id is not None:
            raise _invalid("planning attempt carries a not-yet-admitted plan or task identity")
        if event.event_type == PLAN_BUILD_INTENT:
            if current.status != "STARTED" or current.attempt != len(attempts) + 1:
                raise _invalid("planning attempt admission is not contiguous")
            if attempts and attempts[-1].status in {"STARTED", "SUCCEEDED", "TIMED_OUT"}:
                raise _invalid("planning cannot retry an active, successful or timed-out attempt")
            if attempts and not attempts[-1].retryable:
                raise _invalid("planning failure is not eligible for retry")
            attempts.append(current)
        else:
            if not attempts or attempts[-1].status != "STARTED" or current.status == "STARTED":
                raise _invalid("planning receipt has no unresolved intent")
            if current.identity_checksum != attempts[-1].identity_checksum:
                raise _invalid("planning receipt differs from admitted attempt")
            if current.status == "SUCCEEDED" and current.candidate_checksum not in built_candidates:
                raise _invalid("planning success has no previously persisted candidate")
            attempts[-1] = current
    return tuple(attempts)


def _invalid(message: str) -> HarnessValidationError:
    return HarnessValidationError(message, code="task_plan_build_history_invalid")
