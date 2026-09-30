from __future__ import annotations

"""Process-scoped execution composition contracts.

The composition owns execution provider/profile policy only.
``required_provider_ids`` is deliberately explicit: a profile may be present
in the shared catalog for another process role without making that provider a
startup dependency here.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import re
from types import MappingProxyType
from typing import Any

from framework.execution_environment.errors import (
    ExecutionEnvironmentUnavailableError,
    RuntimeCompositionDriftError,
    RuntimeCompositionProfileError,
)
from framework.execution_environment.models import (
    CAPABILITY_DENIAL_CODE_VERSION,
    DeploymentCapabilityEvidence,
    DeploymentRollbackEvidence,
    EXECUTION_CAPABILITY_FIELDS,
    ExecutionMode,
    ExecutionProfile,
    capability_denial_code,
)
from framework.execution_environment.registry import ExecutionEnvironmentRegistry
from framework.shared.json import stable_json_dumps
from framework.shared.time import utc_now


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}\Z")
_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DEPLOYMENT_EVIDENCE_DENIAL_CODES = MappingProxyType({
    "deployment_capability_evidence_missing": (
        "execution_deployment_capability_evidence_missing"
    ),
    "deployment_capability_evidence_mismatch": (
        "execution_deployment_capability_evidence_mismatch"
    ),
    "deployment_capability_evidence_expired": (
        "execution_deployment_capability_evidence_expired"
    ),
    "deployment_capability_evidence_not_yet_valid": (
        "execution_deployment_capability_evidence_not_yet_valid"
    ),
    "deployment_capability_evidence_incomplete": (
        "execution_deployment_capability_evidence_incomplete"
    ),
    "deployment_capability_evidence_unbound": (
        "execution_deployment_capability_evidence_unbound"
    ),
    "deployment_capability_evidence_unsupported": (
        "execution_deployment_capability_evidence_unsupported"
    ),
    "deployment_capability_evidence_inconsistent": (
        "execution_deployment_capability_evidence_inconsistent"
    ),
    "deployment_rollback_evidence_missing": (
        "execution_deployment_rollback_evidence_missing"
    ),
    "deployment_rollback_evidence_unbound": (
        "execution_deployment_rollback_evidence_unbound"
    ),
    "deployment_rollback_evidence_mismatch": (
        "execution_deployment_rollback_evidence_mismatch"
    ),
    "deployment_rollback_evidence_failed": (
        "execution_deployment_rollback_evidence_failed"
    ),
})


def _identifier(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return normalized


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        stable_json_dumps(value).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeCompositionManifest:
    """Immutable identity shared by all instances of one process role."""

    composition_id: str
    version: str = "1"
    policy_fingerprint: str = "sha256:" + "0" * 64
    provider_fingerprint: str = "sha256:" + "0" * 64
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deployment_capability_evidence: tuple[
        DeploymentCapabilityEvidence | Mapping[str, Any], ...
    ] = ()
    deployment_rollback_evidence: tuple[
        DeploymentRollbackEvidence | Mapping[str, Any], ...
    ] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "composition_id", _identifier(self.composition_id, "composition_id"))
        object.__setattr__(self, "version", _identifier(self.version, "version"))
        for name in ("policy_fingerprint", "provider_fingerprint"):
            value = str(getattr(self, name)).strip().lower()
            if _CHECKSUM.fullmatch(value) is None:
                raise ValueError(f"{name} must be a sha256 checksum")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "metadata", dict(self.metadata))
        evidence: list[DeploymentCapabilityEvidence] = []
        for item in self.deployment_capability_evidence:
            normalized = (
                item
                if isinstance(item, DeploymentCapabilityEvidence)
                else DeploymentCapabilityEvidence.from_dict(item)
            )
            evidence.append(normalized)
        provider_ids = [item.provider_id for item in evidence]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError(
                "deployment capability evidence must contain one receipt per provider"
            )
        object.__setattr__(self, "deployment_capability_evidence", tuple(sorted(
            evidence, key=lambda item: item.provider_id
        )))
        rollback_evidence: list[DeploymentRollbackEvidence] = []
        for item in self.deployment_rollback_evidence:
            normalized = (
                item
                if isinstance(item, DeploymentRollbackEvidence)
                else DeploymentRollbackEvidence.from_dict(item)
            )
            rollback_evidence.append(normalized)
        rollback_refs = [item.evidence_ref for item in rollback_evidence]
        if len(set(rollback_refs)) != len(rollback_refs):
            raise ValueError("deployment rollback evidence refs must be unique")
        object.__setattr__(self, "deployment_rollback_evidence", tuple(sorted(
            rollback_evidence, key=lambda item: item.evidence_ref
        )))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    @classmethod
    def from_registries(
        cls,
        *,
        composition_id: str,
        profile_registry: "ExecutionProfileRegistry",
        execution_registry: ExecutionEnvironmentRegistry,
        **kwargs: Any,
    ) -> "RuntimeCompositionManifest":
        return cls(
            composition_id=composition_id,
            policy_fingerprint=profile_registry.fingerprint,
            provider_fingerprint=execution_registry.fingerprint,
            **kwargs,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeCompositionManifest":
        if not isinstance(value, Mapping):
            raise TypeError("runtime composition manifest must be an object")
        expected = {
            "composition_id", "version", "policy_fingerprint", "provider_fingerprint",
            "metadata",
            "deployment_capability_evidence",
            "deployment_rollback_evidence",
        }
        unknown = sorted(set(value) - expected)
        if unknown:
            raise ValueError(f"runtime composition manifest contains unknown fields: {unknown}")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition_id": self.composition_id,
            "version": self.version,
            "policy_fingerprint": self.policy_fingerprint,
            "provider_fingerprint": self.provider_fingerprint,
            "metadata": dict(self.metadata),
            "deployment_capability_evidence": [
                item.to_dict() for item in self.deployment_capability_evidence
            ],
            "deployment_rollback_evidence": [
                item.to_dict() for item in self.deployment_rollback_evidence
            ],
        }


class ExecutionProfileRegistry:
    """Explicit, immutable-by-fingerprint registry of named execution profiles."""

    def __init__(self) -> None:
        self._profiles: dict[str, ExecutionProfile] = {}

    def register(self, profile_id: str, profile: ExecutionProfile) -> None:
        normalized = _identifier(profile_id, "profile_id")
        if not isinstance(profile, ExecutionProfile):
            raise TypeError("profile must be ExecutionProfile")
        if normalized in self._profiles:
            raise ValueError(f"execution profile is already registered: {normalized}")
        self._profiles[normalized] = profile

    def resolve(self, profile_id: str) -> ExecutionProfile:
        normalized = _identifier(profile_id, "profile_id")
        profile = self._profiles.get(normalized)
        if profile is None:
            raise RuntimeCompositionProfileError(
                "requested execution profile is not registered",
                details={"profile_id": normalized},
            )
        return profile

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            [
                {"profile_id": profile_id, "profile": self._profiles[profile_id].to_dict()}
                for profile_id in self.profile_ids
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            profile_id: self._profiles[profile_id].to_dict()
            for profile_id in self.profile_ids
        }


class RuntimeExecutionComposition:
    """Validated process-local binding for execution and tool construction."""

    def __init__(
        self,
        *,
        manifest: RuntimeCompositionManifest,
        profile_registry: ExecutionProfileRegistry,
        execution_registry: ExecutionEnvironmentRegistry,
        expected_manifest_fingerprint: str | None = None,
        require_explicit_execution_profile: bool = True,
        required_provider_ids: Sequence[str] = (),
    ) -> None:
        if not isinstance(manifest, RuntimeCompositionManifest):
            raise TypeError("manifest must be RuntimeCompositionManifest")
        if not isinstance(profile_registry, ExecutionProfileRegistry):
            raise TypeError("profile_registry must be ExecutionProfileRegistry")
        if not isinstance(execution_registry, ExecutionEnvironmentRegistry):
            raise TypeError("execution_registry must be ExecutionEnvironmentRegistry")
        if not isinstance(require_explicit_execution_profile, bool):
            raise TypeError("require_explicit_execution_profile must be boolean")
        required_providers = tuple(sorted(
            _identifier(value, "required_provider_id")
            for value in required_provider_ids
        ))
        if len(set(required_providers)) != len(required_providers):
            raise ValueError("required execution providers must be unique")
        if expected_manifest_fingerprint is not None:
            expected_manifest_fingerprint = str(expected_manifest_fingerprint).strip().lower()
            if _CHECKSUM.fullmatch(expected_manifest_fingerprint) is None:
                raise RuntimeCompositionDriftError(
                    "expected runtime composition fingerprint is invalid",
                    details={
                        "expected_manifest_fingerprint": expected_manifest_fingerprint,
                        "actual_manifest_fingerprint": manifest.fingerprint,
                    },
                )
        if expected_manifest_fingerprint is not None and expected_manifest_fingerprint != manifest.fingerprint:
            raise RuntimeCompositionDriftError(
                details={
                    "expected_manifest_fingerprint": expected_manifest_fingerprint,
                    "actual_manifest_fingerprint": manifest.fingerprint,
                }
            )
        if manifest.policy_fingerprint != profile_registry.fingerprint:
            raise RuntimeCompositionDriftError(
                "runtime policy fingerprint does not match profile registry",
                details={"expected": manifest.policy_fingerprint, "actual": profile_registry.fingerprint},
            )
        if manifest.provider_fingerprint != execution_registry.fingerprint:
            raise RuntimeCompositionDriftError(
                "runtime provider fingerprint does not match execution registry",
                details={"expected": manifest.provider_fingerprint, "actual": execution_registry.fingerprint},
            )
        self.manifest = manifest
        self.profile_registry = profile_registry
        self.execution_registry = execution_registry
        self.require_explicit_execution_profile = require_explicit_execution_profile
        self._required_provider_ids = required_providers

    @property
    def fingerprint(self) -> str:
        return self.manifest.fingerprint

    def resolve_profile(self, profile_id: str) -> ExecutionProfile:
        self.verify_integrity()
        profile = self.profile_registry.resolve(profile_id)
        if profile.mode in {
            ExecutionMode.SANDBOXED_PROCESS,
            ExecutionMode.EXTERNAL_PROCESS,
        } and profile.provider_id not in self.execution_registry.provider_ids():
            raise ExecutionEnvironmentUnavailableError(
                "execution profile provider is not registered",
                details={"profile_id": profile_id, "provider_id": profile.provider_id},
            )
        return profile

    def verify_integrity(self) -> None:
        """Fail closed if mutable registries drift after composition startup."""

        if self.manifest.policy_fingerprint != self.profile_registry.fingerprint:
            raise RuntimeCompositionDriftError(
                "runtime policy fingerprint drift detected",
                details={
                    "expected": self.manifest.policy_fingerprint,
                    "actual": self.profile_registry.fingerprint,
                },
            )
        if self.manifest.provider_fingerprint != self.execution_registry.fingerprint:
            raise RuntimeCompositionDriftError(
                "runtime provider fingerprint drift detected",
                details={
                    "expected": self.manifest.provider_fingerprint,
                    "actual": self.execution_registry.fingerprint,
                },
            )

    def diagnostics(self) -> dict[str, Any]:
        self.verify_integrity()
        provider_capabilities = {
            provider_id: self.execution_registry.resolve_capabilities(provider_id).to_dict()
            for provider_id in self.execution_registry.provider_ids()
        }
        profiles = {
            profile_id: self.profile_registry.resolve(profile_id).to_dict()
            for profile_id in self.profile_registry.profile_ids
        }
        unavailable_providers = self.unavailable_provider_ids
        evidence_issues = self.deployment_capability_evidence_issues()
        return {
            "status": "blocked" if unavailable_providers or evidence_issues else "ready",
            "composition_id": self.manifest.composition_id,
            "manifest_fingerprint": self.manifest.fingerprint,
            "policy_fingerprint": self.manifest.policy_fingerprint,
            "provider_fingerprint": self.manifest.provider_fingerprint,
            "profiles": list(self.profile_registry.profile_ids),
            "profile_catalog": profiles,
            "providers": list(self.execution_registry.provider_ids()),
            "required_providers": list(self.required_provider_ids),
            "unavailable_providers": list(unavailable_providers),
            "deployment_capability_evidence": [
                item.to_operator_projection()
                for item in self.manifest.deployment_capability_evidence
            ],
            "deployment_capability_evidence_issues": evidence_issues,
            "deployment_qualification_issues": self.deployment_qualification_issues(),
            "deployment_rollback_evidence": [
                item.to_operator_projection()
                for item in self.manifest.deployment_rollback_evidence
            ],
            "provider_capabilities": provider_capabilities,
        }

    @property
    def required_provider_ids(self) -> tuple[str, ...]:
        return self._required_provider_ids

    @property
    def unavailable_provider_ids(self) -> tuple[str, ...]:
        registered = set(self.execution_registry.provider_ids())
        return tuple(
            provider_id
            for provider_id in self.required_provider_ids
            if provider_id not in registered
            or not self.execution_registry.resolve_capabilities(provider_id).available
        )

    def deployment_capability_evidence_issues(self) -> list[dict[str, Any]]:
        """Return stable, redacted qualification denials for required providers."""

        evidence_by_provider = {
            item.provider_id: item
            for item in self.manifest.deployment_capability_evidence
        }
        registered = set(self.execution_registry.provider_ids())
        now = utc_now()
        issues: list[dict[str, Any]] = []
        for evidence in self.manifest.deployment_capability_evidence:
            if evidence.provider_id not in registered:
                issues.append(
                    {
                        "provider_id": evidence.provider_id,
                        "capability": "deployment_capability_evidence_unbound",
                        "denial_code": _DEPLOYMENT_EVIDENCE_DENIAL_CODES[
                            "deployment_capability_evidence_unbound"
                        ],
                    }
                )
        for provider_id in self.required_provider_ids:
            if provider_id not in registered:
                continue
            capabilities = self.execution_registry.resolve_capabilities(provider_id)
            if not capabilities.available:
                continue
            evidence = evidence_by_provider.get(provider_id)
            reason = None
            if evidence is None:
                reason = "deployment_capability_evidence_missing"
            elif not all(
                (
                    evidence.deployment_ref,
                    evidence.deployment_identity,
                    evidence.image_ref,
                    evidence.image_digest,
                    evidence.rollback_evidence_ref,
                )
            ):
                reason = "deployment_capability_evidence_incomplete"
            elif evidence.provider_capability_checksum != capabilities.checksum:
                reason = "deployment_capability_evidence_mismatch"
            else:
                advertised_capabilities = {
                    capability_name
                    for field_name, capability_name in EXECUTION_CAPABILITY_FIELDS
                    if getattr(capabilities, field_name)
                }
                if set(evidence.tested_capabilities) - advertised_capabilities:
                    reason = "deployment_capability_evidence_inconsistent"
                elif set(evidence.unsupported_capabilities) & advertised_capabilities:
                    reason = "deployment_capability_evidence_unsupported"
            if reason is None:
                if now < evidence.qualified_at:
                    reason = "deployment_capability_evidence_not_yet_valid"
                elif now >= evidence.expires_at:
                    reason = "deployment_capability_evidence_expired"
            if reason is not None:
                issues.append(
                    {
                        "provider_id": provider_id,
                        "capability": reason,
                        "denial_code": _DEPLOYMENT_EVIDENCE_DENIAL_CODES[reason],
                    }
                )
        return issues

    def require_ready(self) -> None:
        """Fail closed unless every role-required provider is available.

        Catalogued providers that are not listed in ``required_provider_ids``
        remain admission-gated when an activity requests them, but do not make
        an otherwise trusted-only process report itself unavailable.
        """

        self.verify_integrity()
        unavailable = self.unavailable_provider_ids
        evidence_issues = self.deployment_capability_evidence_issues()
        if unavailable or evidence_issues:
            denials = [
                {
                    "provider_id": provider_id,
                    "capability": "provider_unavailable",
                    "denial_code": capability_denial_code("provider_unavailable"),
                }
                for provider_id in unavailable
            ] + evidence_issues
            missing = [item["capability"] for item in denials]
            raise ExecutionEnvironmentUnavailableError(
                "required runtime execution providers are unavailable",
                details={
                    "provider_ids": sorted({item["provider_id"] for item in denials}),
                    "missing": missing,
                    "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
                    "denial_code": denials[0]["denial_code"],
                    "denials": denials,
                },
            )

    def deployment_qualification_issues(self) -> list[dict[str, Any]]:
        """Validate deployment and rollback receipts for every enabled provider.

        ``required_provider_ids`` is the explicit enabled-provider set for a
        process role.  This gate is intentionally separate from local
        provider admission: a Docker daemon probe or capability checksum is
        insufficient without a deployment-owned receipt and a successful,
        identity-bound rollback rehearsal.
        """

        self.verify_integrity()
        evidence_by_provider = {
            item.provider_id: item
            for item in self.manifest.deployment_capability_evidence
        }
        rollback_by_ref = {
            item.evidence_ref: item
            for item in self.manifest.deployment_rollback_evidence
        }
        issues: list[dict[str, Any]] = []

        def add(provider_id: str, capability: str) -> None:
            issues.append({
                "provider_id": provider_id,
                "capability": capability,
                "denial_code": _DEPLOYMENT_EVIDENCE_DENIAL_CODES[capability],
            })

        enabled = set(self.required_provider_ids)
        for rollback in self.manifest.deployment_rollback_evidence:
            if rollback.provider_id not in enabled:
                add(rollback.provider_id, "deployment_rollback_evidence_unbound")

        for provider_id in self.required_provider_ids:
            if provider_id not in self.execution_registry.provider_ids():
                add(provider_id, "deployment_capability_evidence_missing")
                continue
            capabilities = self.execution_registry.resolve_capabilities(provider_id)
            evidence = evidence_by_provider.get(provider_id)
            if evidence is None:
                add(provider_id, "deployment_capability_evidence_missing")
                continue
            static_issues = self._deployment_capability_evidence_issues_for(
                provider_id, capabilities, evidence
            )
            if static_issues:
                issues.extend(static_issues)
                continue
            rollback = rollback_by_ref.get(evidence.rollback_evidence_ref)
            if rollback is None:
                add(provider_id, "deployment_rollback_evidence_missing")
                continue
            if rollback.provider_id != provider_id:
                add(provider_id, "deployment_rollback_evidence_mismatch")
                continue
            if (
                rollback.deployment_identity != evidence.deployment_identity
                or rollback.image_digest != evidence.image_digest
                or rollback.provider_capability_checksum != capabilities.checksum
            ):
                add(provider_id, "deployment_rollback_evidence_mismatch")
                continue
            if rollback.status != "succeeded" or not rollback.termination_confirmed:
                add(provider_id, "deployment_rollback_evidence_failed")
        return issues

    def require_deployment_ready(self) -> None:
        """Fail closed unless enabled providers have deployment/rollback proof."""

        issues = self.deployment_qualification_issues()
        if issues:
            raise ExecutionEnvironmentUnavailableError(
                "runtime deployment qualification is unavailable",
                details={
                    "provider_ids": sorted({item["provider_id"] for item in issues}),
                    "missing": [item["capability"] for item in issues],
                    "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
                    "denial_code": issues[0]["denial_code"],
                    "denials": issues,
                },
            )

    @staticmethod
    def _deployment_capability_evidence_issues_for(
        provider_id: str,
        capabilities: Any,
        evidence: DeploymentCapabilityEvidence,
    ) -> list[dict[str, Any]]:
        reason: str | None = None
        if not all((evidence.deployment_ref, evidence.deployment_identity,
                    evidence.image_ref, evidence.image_digest,
                    evidence.rollback_evidence_ref)):
            reason = "deployment_capability_evidence_incomplete"
        elif evidence.provider_capability_checksum != capabilities.checksum:
            reason = "deployment_capability_evidence_mismatch"
        else:
            advertised_capabilities = {
                capability_name
                for field_name, capability_name in EXECUTION_CAPABILITY_FIELDS
                if getattr(capabilities, field_name)
            }
            if set(evidence.tested_capabilities) - advertised_capabilities:
                reason = "deployment_capability_evidence_inconsistent"
            elif set(evidence.unsupported_capabilities) & advertised_capabilities:
                reason = "deployment_capability_evidence_unsupported"
        if reason is None and not evidence.is_valid_at(utc_now()):
            reason = (
                "deployment_capability_evidence_not_yet_valid"
                if utc_now() < evidence.qualified_at
                else "deployment_capability_evidence_expired"
            )
        return [] if reason is None else [{
            "provider_id": provider_id,
            "capability": reason,
            "denial_code": _DEPLOYMENT_EVIDENCE_DENIAL_CODES[reason],
        }]

    def tool_executor_factory(self, registry: Any, **kwargs: Any) -> Any:
        self.verify_integrity()
        from framework.tool.runtime.executor import ToolExecutor

        kwargs["execution_environment"] = self.execution_registry
        kwargs["require_explicit_execution_profile"] = (
            self.require_explicit_execution_profile
        )
        return ToolExecutor(registry, **kwargs)


def build_runtime_execution_composition(
    *,
    manifest: RuntimeCompositionManifest,
    profile_registry: ExecutionProfileRegistry,
    execution_registry: ExecutionEnvironmentRegistry,
    **kwargs: Any,
) -> RuntimeExecutionComposition:
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profile_registry,
        execution_registry=execution_registry,
        **kwargs,
    )


__all__ = [
    "ExecutionProfileRegistry",
    "RuntimeCompositionManifest",
    "RuntimeExecutionComposition",
    "build_runtime_execution_composition",
]
