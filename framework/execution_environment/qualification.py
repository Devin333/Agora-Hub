"""Deployment qualification probes for execution providers.

The execution registry can prove that a provider is usable *now*.  It cannot
prove that a deployed image was exercised or that a pinned rollback target was
restored.  This module keeps that distinction explicit: only a deployment
owner supplied probe may produce the two deployment receipts consumed by
``RuntimeExecutionComposition``.  Missing probes and malformed receipts are
typed denials; they never get converted into synthetic evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from framework.execution_environment.models import (
    DeploymentCapabilityEvidence,
    DeploymentRollbackEvidence,
    EXECUTION_CAPABILITY_FIELDS,
    ExecutionCapabilityProfile,
)
from framework.execution_environment.registry import ExecutionEnvironmentRegistry
from framework.shared.time import utc_now


DEPLOYMENT_QUALIFICATION_SCHEMA = "newsroom.execution-deployment-qualification/v1"


@runtime_checkable
class DeploymentQualificationProbe(Protocol):
    """Deployment-owner boundary for real qualification and rollback receipts."""

    def produce_deployment_capability_evidence(
        self,
        *,
        provider_id: str,
        capabilities: ExecutionCapabilityProfile,
    ) -> DeploymentCapabilityEvidence | Mapping[str, Any]: ...

    def produce_deployment_rollback_evidence(
        self,
        *,
        provider_id: str,
        capability_evidence: DeploymentCapabilityEvidence,
    ) -> DeploymentRollbackEvidence | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DeploymentQualificationIssue:
    provider_id: str
    reason_code: str
    denial_code: str

    def __post_init__(self) -> None:
        provider_id = str(self.provider_id).strip()
        reason_code = str(self.reason_code).strip()
        denial_code = str(self.denial_code).strip()
        if not provider_id or not reason_code or not denial_code:
            raise ValueError("deployment qualification issue fields are required")
        if len(provider_id) > 256 or len(reason_code) > 128 or len(denial_code) > 128:
            raise ValueError("deployment qualification issue fields are bounded")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "denial_code", denial_code)

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "reason_code": self.reason_code,
            "denial_code": self.denial_code,
        }


@dataclass(frozen=True, slots=True)
class DeploymentQualificationResult:
    """Deterministic output of a qualification run.

    Receipts are returned only when the provider probe returned valid typed
    evidence.  Callers may bind this result into a manifest; an issue means
    the composition must remain dependency-unavailable.
    """

    capability_evidence: tuple[DeploymentCapabilityEvidence, ...] = ()
    rollback_evidence: tuple[DeploymentRollbackEvidence, ...] = ()
    issues: tuple[DeploymentQualificationIssue, ...] = ()
    schema_version: str = DEPLOYMENT_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != DEPLOYMENT_QUALIFICATION_SCHEMA:
            raise ValueError("unsupported deployment qualification schema")
        capabilities = tuple(self.capability_evidence)
        rollback = tuple(self.rollback_evidence)
        issues = tuple(self.issues)
        if not all(isinstance(item, DeploymentCapabilityEvidence) for item in capabilities):
            raise TypeError("capability_evidence must contain typed receipts")
        if not all(isinstance(item, DeploymentRollbackEvidence) for item in rollback):
            raise TypeError("rollback_evidence must contain typed receipts")
        if not all(isinstance(item, DeploymentQualificationIssue) for item in issues):
            raise TypeError("issues must contain DeploymentQualificationIssue values")
        if len({item.provider_id for item in capabilities}) != len(capabilities):
            raise ValueError("capability evidence must contain one receipt per provider")
        if len({item.evidence_ref for item in rollback}) != len(rollback):
            raise ValueError("rollback evidence refs must be unique")
        object.__setattr__(self, "capability_evidence", tuple(sorted(capabilities, key=lambda item: item.provider_id)))
        object.__setattr__(self, "rollback_evidence", tuple(sorted(rollback, key=lambda item: item.evidence_ref)))
        object.__setattr__(self, "issues", tuple(sorted(issues, key=lambda item: (item.provider_id, item.reason_code))))

    @property
    def ready(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_evidence": [item.to_operator_projection() for item in self.capability_evidence],
            "rollback_evidence": [item.to_operator_projection() for item in self.rollback_evidence],
            "issues": [item.to_dict() for item in self.issues],
            "ready": self.ready,
        }


class DeploymentQualificationService:
    """Run explicit provider-owned deployment and rollback probes."""

    def __init__(
        self,
        *,
        execution_registry: ExecutionEnvironmentRegistry,
        required_provider_ids: Sequence[str],
    ) -> None:
        if not isinstance(execution_registry, ExecutionEnvironmentRegistry):
            raise TypeError("execution_registry must be ExecutionEnvironmentRegistry")
        provider_ids = tuple(sorted(str(item).strip() for item in required_provider_ids))
        if not provider_ids or any(not item for item in provider_ids):
            raise ValueError("required_provider_ids must contain non-empty ids")
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("required_provider_ids must be unique")
        self._execution_registry = execution_registry
        self._required_provider_ids = provider_ids

    @property
    def required_provider_ids(self) -> tuple[str, ...]:
        return self._required_provider_ids

    def qualify(
        self,
        *,
        probes: Mapping[str, DeploymentQualificationProbe] | None = None,
        reference_time: datetime | None = None,
    ) -> DeploymentQualificationResult:
        """Probe every enabled provider and return receipts plus typed denials.

        A probe must be supplied by the deployment owner.  A registry provider
        is never treated as a probe merely because it can execute workloads.
        """

        if probes is None:
            probes = {}
        if not isinstance(probes, Mapping):
            raise TypeError("probes must be a mapping")
        now = utc_now() if reference_time is None else _utc(reference_time)
        capability_receipts: list[DeploymentCapabilityEvidence] = []
        rollback_receipts: list[DeploymentRollbackEvidence] = []
        issues: list[DeploymentQualificationIssue] = []
        for provider_id in self._required_provider_ids:
            try:
                capabilities = self._execution_registry.resolve_capabilities(provider_id)
            except Exception:
                issues.append(_issue(provider_id, "provider_unavailable"))
                continue
            if not capabilities.available:
                issues.append(_issue(provider_id, "provider_unavailable"))
                continue
            probe = probes.get(provider_id)
            if probe is None or not callable(getattr(probe, "produce_deployment_capability_evidence", None)):
                issues.append(_issue(provider_id, "capability_probe_unsupported"))
                continue
            try:
                evidence = _as_capability_evidence(
                    probe.produce_deployment_capability_evidence(
                        provider_id=provider_id,
                        capabilities=capabilities,
                    )
                )
            except Exception:
                issues.append(_issue(provider_id, "capability_probe_invalid"))
                continue
            receipt_issue = _validate_capability_evidence(provider_id, capabilities, evidence, now)
            if receipt_issue is not None:
                issues.append(_issue(provider_id, receipt_issue))
                continue
            capability_receipts.append(evidence)
            rollback_fn = getattr(probe, "produce_deployment_rollback_evidence", None)
            if not callable(rollback_fn):
                issues.append(_issue(provider_id, "rollback_probe_unsupported"))
                continue
            try:
                rollback = _as_rollback_evidence(
                    rollback_fn(provider_id=provider_id, capability_evidence=evidence)
                )
            except Exception:
                issues.append(_issue(provider_id, "rollback_probe_invalid"))
                continue
            rollback_issue = _validate_rollback_evidence(provider_id, capabilities, evidence, rollback)
            if rollback_issue is not None:
                issues.append(_issue(provider_id, rollback_issue))
                continue
            rollback_receipts.append(rollback)
        return DeploymentQualificationResult(
            capability_evidence=tuple(capability_receipts),
            rollback_evidence=tuple(rollback_receipts),
            issues=tuple(issues),
        )


def _as_capability_evidence(value: Any) -> DeploymentCapabilityEvidence:
    if isinstance(value, DeploymentCapabilityEvidence):
        return value
    if isinstance(value, Mapping):
        return DeploymentCapabilityEvidence.from_dict(value)
    raise TypeError("probe did not return deployment capability evidence")


def _as_rollback_evidence(value: Any) -> DeploymentRollbackEvidence:
    if isinstance(value, DeploymentRollbackEvidence):
        return value
    if isinstance(value, Mapping):
        return DeploymentRollbackEvidence.from_dict(value)
    raise TypeError("probe did not return deployment rollback evidence")


def _validate_capability_evidence(
    provider_id: str,
    capabilities: ExecutionCapabilityProfile,
    evidence: DeploymentCapabilityEvidence,
    now: datetime,
) -> str | None:
    if evidence.provider_id != provider_id:
        return "capability_evidence_provider_mismatch"
    if evidence.provider_capability_checksum != capabilities.checksum:
        return "capability_evidence_checksum_mismatch"
    advertised = {
        name
        for field, name in EXECUTION_CAPABILITY_FIELDS
        if getattr(capabilities, field)
    }
    tested = set(evidence.tested_capabilities)
    if tested - advertised or advertised - tested:
        return "capability_evidence_unsupported"
    if set(evidence.unsupported_capabilities) & advertised:
        return "capability_evidence_unsupported"
    if not evidence.is_valid_at(now):
        return "capability_evidence_expired"
    return None


def _validate_rollback_evidence(
    provider_id: str,
    capabilities: ExecutionCapabilityProfile,
    capability: DeploymentCapabilityEvidence,
    rollback: DeploymentRollbackEvidence,
) -> str | None:
    if rollback.provider_id != provider_id:
        return "rollback_evidence_provider_mismatch"
    if rollback.provider_capability_checksum != capabilities.checksum:
        return "rollback_evidence_checksum_mismatch"
    if (
        rollback.deployment_identity != capability.deployment_identity
        or rollback.image_digest != capability.image_digest
    ):
        return "rollback_evidence_identity_mismatch"
    if rollback.evidence_ref != capability.rollback_evidence_ref:
        return "rollback_evidence_ref_mismatch"
    if rollback.status != "succeeded" or not rollback.termination_confirmed:
        return "rollback_evidence_failed"
    return None


def _issue(provider_id: str, reason: str) -> DeploymentQualificationIssue:
    denial = {
        "provider_unavailable": "execution_provider_unavailable",
        "capability_probe_unsupported": "execution_deployment_capability_probe_unsupported",
        "capability_probe_invalid": "execution_deployment_capability_probe_invalid",
        "capability_evidence_provider_mismatch": "execution_deployment_capability_evidence_mismatch",
        "capability_evidence_checksum_mismatch": "execution_deployment_capability_evidence_mismatch",
        "capability_evidence_unsupported": "execution_deployment_capability_unsupported",
        "capability_evidence_expired": "execution_deployment_capability_evidence_expired",
        "rollback_probe_unsupported": "execution_deployment_rollback_probe_unsupported",
        "rollback_probe_invalid": "execution_deployment_rollback_probe_invalid",
        "rollback_evidence_provider_mismatch": "execution_deployment_rollback_evidence_mismatch",
        "rollback_evidence_checksum_mismatch": "execution_deployment_rollback_evidence_mismatch",
        "rollback_evidence_identity_mismatch": "execution_deployment_rollback_evidence_mismatch",
        "rollback_evidence_ref_mismatch": "execution_deployment_rollback_evidence_mismatch",
        "rollback_evidence_failed": "execution_deployment_rollback_evidence_failed",
    }.get(reason, "execution_deployment_qualification_failed")
    return DeploymentQualificationIssue(provider_id, reason, denial)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("reference_time must be datetime")
    if value.tzinfo is None:
        raise ValueError("reference_time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "DEPLOYMENT_QUALIFICATION_SCHEMA",
    "DeploymentQualificationIssue",
    "DeploymentQualificationProbe",
    "DeploymentQualificationResult",
    "DeploymentQualificationService",
]
