from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace

from framework.execution_environment import (
    DeploymentCapabilityEvidence,
    DeploymentRollbackEvidence,
    DeploymentQualificationService,
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
)


class _Provider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(
            provider_id="docker",
            available=True,
            enforces_filesystem_roots=True,
            enforces_network_deny=True,
            enforces_argv_policy=True,
            isolates_environment=True,
            controls_process_tree=True,
            confirms_termination=True,
            supports_cancellation=True,
        )

    def execute(self, request, *, cancellation=None):
        raise AssertionError


def _probe(provider: ExecutionCapabilityProfile):
    now = datetime.now(UTC)
    capability = DeploymentCapabilityEvidence(
        provider_id="docker",
        deployment_ref="deployment-1",
        deployment_identity="runtime-1",
        image_ref="newsroom/runtime",
        image_digest="sha256:" + "a" * 64,
        provider_capability_checksum=provider.checksum,
        evidence_kind="deployment_owner_probe",
        evidence_source="owner-receipt-1",
        tested_capabilities=(
            "filesystem_roots",
            "network_deny",
            "environment_isolation",
            "argv_policy",
            "process_tree_control",
            "cancellation",
            "termination_confirmation",
        ),
        unsupported_capabilities=("network_allowlist",),
        qualified_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        rollback_evidence_ref="rollback-1",
    )
    rollback = DeploymentRollbackEvidence(
        evidence_ref="rollback-1",
        provider_id="docker",
        deployment_identity=capability.deployment_identity,
        image_digest=capability.image_digest,
        provider_capability_checksum=provider.checksum,
        rollback_target_identity="runtime-0",
        rollback_target_image_digest="sha256:" + "b" * 64,
        status="succeeded",
        termination_confirmed=True,
        verified_at=now,
    )

    class Probe:
        def produce_deployment_capability_evidence(self, **kwargs):
            return capability

        def produce_deployment_rollback_evidence(self, **kwargs):
            return rollback

    return Probe()


def test_qualification_requires_explicit_owner_probe_and_binds_receipts() -> None:
    registry = ExecutionEnvironmentRegistry()
    provider = _Provider()
    registry.register(provider)
    service = DeploymentQualificationService(
        execution_registry=registry,
        required_provider_ids=("docker",),
    )

    result = service.qualify(probes={"docker": _probe(provider.capabilities)})

    assert result.ready is True
    assert result.capability_evidence[0].provider_id == "docker"
    assert result.rollback_evidence[0].evidence_ref == "rollback-1"


def test_composition_qualification_does_not_mutate_manifest() -> None:
    registry = ExecutionEnvironmentRegistry()
    provider = _Provider()
    registry.register(provider)
    from framework.execution_environment import (
        ExecutionProfileRegistry,
        ExecutionProfile,
        RuntimeCompositionManifest,
        RuntimeExecutionComposition,
    )

    profiles = ExecutionProfileRegistry()
    profiles.register(
        "external",
        ExecutionProfile.external_process(
            provider_id="docker",
            allowed_argv_prefixes=(("worker",),),
        ),
    )
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="test",
        profile_registry=profiles,
        execution_registry=registry,
    )
    composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=registry,
        required_provider_ids=("docker",),
    )
    result = composition.qualify_deployment(probes={"docker": _probe(provider.capabilities)})
    assert result.ready is True
    assert composition.manifest.deployment_capability_evidence == ()


def test_qualification_missing_probe_is_fail_closed_with_stable_reason() -> None:
    registry = ExecutionEnvironmentRegistry()
    registry.register(_Provider())
    result = DeploymentQualificationService(
        execution_registry=registry,
        required_provider_ids=("docker",),
    ).qualify()

    assert result.ready is False
    assert result.issues[0].reason_code == "capability_probe_unsupported"
    assert result.issues[0].denial_code == "execution_deployment_capability_probe_unsupported"
    assert result.capability_evidence == ()


def test_qualification_rejects_unsupported_advertised_capability() -> None:
    registry = ExecutionEnvironmentRegistry()
    provider = _Provider()
    registry.register(provider)
    probe = _probe(provider.capabilities)
    original = probe.produce_deployment_capability_evidence

    def invalid(**kwargs):
        evidence = original(**kwargs)
        return replace(
            evidence,
            tested_capabilities=("network_allowlist",),
            unsupported_capabilities=(),
        )

    probe.produce_deployment_capability_evidence = invalid
    result = DeploymentQualificationService(
        execution_registry=registry,
        required_provider_ids=("docker",),
    ).qualify(probes={"docker": probe})

    assert result.ready is False
    assert result.issues[0].reason_code == "capability_evidence_unsupported"


def test_qualification_rejects_incomplete_advertised_capability_coverage() -> None:
    registry = ExecutionEnvironmentRegistry()
    provider = _Provider()
    registry.register(provider)
    probe = _probe(provider.capabilities)
    original = probe.produce_deployment_capability_evidence

    def incomplete(**kwargs):
        evidence = original(**kwargs)
        return replace(
            evidence,
            tested_capabilities=("filesystem_roots", "network_deny"),
        )

    probe.produce_deployment_capability_evidence = incomplete
    result = DeploymentQualificationService(
        execution_registry=registry,
        required_provider_ids=("docker",),
    ).qualify(probes={"docker": probe})

    assert result.ready is False
    assert result.issues[0].reason_code == "capability_evidence_unsupported"


def test_qualification_requires_successful_rollback_receipt() -> None:
    registry = ExecutionEnvironmentRegistry()
    provider = _Provider()
    registry.register(provider)
    probe = _probe(provider.capabilities)

    class FailedProbe:
        def produce_deployment_capability_evidence(self, **kwargs):
            return probe.produce_deployment_capability_evidence(**kwargs)

        def produce_deployment_rollback_evidence(self, **kwargs):
            rollback = probe.produce_deployment_rollback_evidence(**kwargs)
            return DeploymentRollbackEvidence(
                **{**rollback.to_dict(), "status": "indeterminate", "verified_at": rollback.verified_at}
            )

    result = DeploymentQualificationService(
        execution_registry=registry,
        required_provider_ids=("docker",),
    ).qualify(probes={"docker": FailedProbe()})

    assert result.ready is False
    assert result.issues[0].reason_code == "rollback_evidence_failed"
