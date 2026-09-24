from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from framework.execution_environment import (
    DeploymentCapabilityEvidence,
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionProfileRegistry,
    RuntimeCompositionDriftError,
    RuntimeCompositionManifest,
    RuntimeCompositionProfileError,
    RuntimeExecutionComposition,
)
from framework.execution_environment.ports import ExecutionEnvironmentPort
from framework.tool import ToolRegistry


def _composition() -> tuple[RuntimeExecutionComposition, ExecutionProfileRegistry, ExecutionEnvironmentRegistry]:
    profiles = ExecutionProfileRegistry()
    profiles.register("trusted", ExecutionProfile.trusted_in_process())
    providers = ExecutionEnvironmentRegistry()
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="test-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
    ), profiles, providers


def test_manifest_fingerprint_is_stable_and_round_trips() -> None:
    composition, _profiles, _providers = _composition()
    restored = RuntimeCompositionManifest.from_dict(composition.manifest.to_dict())

    assert restored == composition.manifest
    assert restored.fingerprint == composition.fingerprint
    assert composition.diagnostics()["status"] == "ready"


def test_profile_registry_missing_profile_is_typed_denial() -> None:
    composition, _profiles, _providers = _composition()

    with pytest.raises(RuntimeCompositionProfileError) as error:
        composition.resolve_profile("missing")

    assert error.value.reason_code == "runtime_profile_denied"
    assert error.value.details["profile_id"] == "missing"


def test_composition_detects_registry_drift_before_factory_use() -> None:
    composition, profiles, _providers = _composition()
    profiles.register("trusted-copy", ExecutionProfile.trusted_in_process())

    with pytest.raises(RuntimeCompositionDriftError) as error:
        composition.tool_executor_factory(ToolRegistry())

    assert error.value.reason_code == "runtime_composition_drift"


def test_tool_executor_factory_binds_execution_registry() -> None:
    composition, _profiles, providers = _composition()

    executor = composition.tool_executor_factory(
        ToolRegistry(),
        execution_environment=object(),
        require_explicit_execution_profile=False,
    )

    assert executor._execution_environment is providers
    assert executor._require_explicit_execution_profile is True


def test_execution_composition_does_not_own_control_plane_ports() -> None:
    composition, _profiles, _providers = _composition()

    diagnostics = composition.diagnostics()

    assert diagnostics["status"] == "ready"
    assert "control_plane_ports" not in diagnostics
    assert "missing_control_plane_ports" not in diagnostics
    assert not hasattr(composition, "bind_control_plane_ports")


class _UnavailableProvider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(
            provider_id="offline",
            available=False,
        )

    def execute(self, request):
        raise AssertionError("unavailable provider must not execute")


class _AvailableProvider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(
            provider_id="qualified",
            available=True,
            enforces_filesystem_roots=True,
            enforces_network_deny=True,
            enforces_argv_policy=True,
        )

    def execute(self, request):
        raise AssertionError("composition tests must not execute a provider")


def _deployment_evidence(
    provider: ExecutionCapabilityProfile,
    *,
    qualified_at: datetime | None = None,
    expires_at: datetime | None = None,
    capability_checksum: str | None = None,
    tested_capabilities: tuple[str, ...] = ("filesystem_roots", "network_deny"),
    rollback_evidence_ref: str = "rollback-qualification-1",
    unsupported_capabilities: tuple[str, ...] = ("network_allowlist",),
) -> DeploymentCapabilityEvidence:
    start = qualified_at or datetime.now(UTC) - timedelta(minutes=1)
    return DeploymentCapabilityEvidence(
        provider_id=provider.provider_id,
        deployment_ref="deployment-qualified-1",
        deployment_identity="engine-qualified-1",
        image_ref="newsroom-execution:qualified",
        image_digest="sha256:" + "a" * 64,
        provider_capability_checksum=capability_checksum or provider.checksum,
        evidence_kind="integration_qualification",
        evidence_source="qualification-record-1",
        tested_capabilities=tested_capabilities,
        unsupported_capabilities=unsupported_capabilities,
        qualified_at=start,
        expires_at=expires_at or start + timedelta(hours=1),
        rollback_evidence_ref=rollback_evidence_ref,
    )


def _qualified_composition(
    evidence: tuple[DeploymentCapabilityEvidence, ...] = (),
) -> RuntimeExecutionComposition:
    profiles = ExecutionProfileRegistry()
    profiles.register(
        "external",
        ExecutionProfile.external_process(
            provider_id="qualified",
            allowed_argv_prefixes=(("worker",),),
        ),
    )
    providers = ExecutionEnvironmentRegistry()
    provider = _AvailableProvider()
    providers.register(provider)
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="qualified-process",
        profile_registry=profiles,
        execution_registry=providers,
        deployment_capability_evidence=evidence,
    )
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("qualified",),
    )


def test_deployment_capability_evidence_round_trips_with_stable_checksum() -> None:
    provider = _AvailableProvider().capabilities
    evidence = _deployment_evidence(provider)
    restored = DeploymentCapabilityEvidence.from_dict(evidence.to_dict())

    assert restored == evidence
    assert restored.checksum == evidence.checksum


def test_required_provider_without_deployment_evidence_is_blocked() -> None:
    composition = _qualified_composition()

    diagnostics = composition.diagnostics()
    assert diagnostics["status"] == "blocked"
    assert diagnostics["deployment_capability_evidence_issues"] == [
        {
            "provider_id": "qualified",
            "capability": "deployment_capability_evidence_missing",
            "denial_code": "execution_deployment_capability_evidence_missing",
        }
    ]
    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        composition.require_ready()
    assert raised.value.reason_code == "execution_environment_unavailable"
    assert raised.value.details["missing"] == [
        "deployment_capability_evidence_missing"
    ]


def test_required_provider_with_matching_deployment_evidence_is_ready() -> None:
    provider = _AvailableProvider().capabilities
    composition = _qualified_composition((_deployment_evidence(provider),))

    assert composition.diagnostics()["status"] == "ready"
    composition.require_ready()


def test_deployment_capability_evidence_rejects_non_string_checksums() -> None:
    provider = _AvailableProvider().capabilities
    values = _deployment_evidence(provider).to_dict()

    with pytest.raises(TypeError, match="image_digest must be a string"):
        DeploymentCapabilityEvidence.from_dict({**values, "image_digest": 123})
    with pytest.raises(TypeError, match="provider_capability_checksum must be a string"):
        DeploymentCapabilityEvidence.from_dict(
            {**values, "provider_capability_checksum": 123}
        )


def test_deployment_capability_evidence_rejects_unknown_capability_names() -> None:
    provider = _AvailableProvider().capabilities

    with pytest.raises(ValueError, match="unknown capabilities"):
        _deployment_evidence(provider, unsupported_capabilities=("made_up_capability",))


def test_diagnostics_redact_deployment_capability_references() -> None:
    provider = _AvailableProvider().capabilities
    evidence = _deployment_evidence(provider)
    composition = _qualified_composition((evidence,))

    projection = composition.diagnostics()["deployment_capability_evidence"][0]
    assert projection["provider_id"] == provider.provider_id
    assert projection["evidence_checksum"] == evidence.checksum
    assert "deployment_ref" not in projection
    assert "deployment_identity" not in projection
    assert "image_ref" not in projection
    assert "evidence_source" not in projection
    assert "rollback_evidence_ref" not in projection
    assert "deployment-qualified-1" not in str(projection)
    assert "qualification-record-1" not in str(projection)



def test_tested_unadvertised_capability_blocks_readiness() -> None:
    provider = _AvailableProvider().capabilities
    evidence = _deployment_evidence(
        provider,
        tested_capabilities=("network_allowlist",),
        unsupported_capabilities=(),
    )
    composition = _qualified_composition((evidence,))

    issue = composition.diagnostics()["deployment_capability_evidence_issues"][0]
    assert issue["capability"] == "deployment_capability_evidence_inconsistent"
    assert issue["denial_code"] == "execution_deployment_capability_evidence_inconsistent"



def test_advertised_unsupported_capability_blocks_readiness() -> None:
    provider = _AvailableProvider().capabilities
    evidence = _deployment_evidence(provider, unsupported_capabilities=("argv_policy",))
    composition = _qualified_composition((evidence,))

    issue = composition.diagnostics()["deployment_capability_evidence_issues"][0]
    assert issue["capability"] == "deployment_capability_evidence_unsupported"
    assert issue["denial_code"] == "execution_deployment_capability_evidence_unsupported"


def test_deployment_capability_evidence_checksum_mismatch_is_blocked() -> None:
    provider = _AvailableProvider().capabilities
    evidence = _deployment_evidence(provider, capability_checksum="sha256:" + "b" * 64)
    composition = _qualified_composition((evidence,))

    issue = composition.diagnostics()["deployment_capability_evidence_issues"][0]
    assert issue["capability"] == "deployment_capability_evidence_mismatch"
    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        composition.require_ready()
    assert raised.value.details["denial_code"] == (
        "execution_deployment_capability_evidence_mismatch"
    )


def test_not_yet_valid_deployment_capability_evidence_is_blocked() -> None:
    provider = _AvailableProvider().capabilities
    start = datetime.now(UTC) + timedelta(hours=1)
    evidence = _deployment_evidence(
        provider,
        qualified_at=start,
        expires_at=start + timedelta(hours=1),
    )
    composition = _qualified_composition((evidence,))

    assert composition.diagnostics()["deployment_capability_evidence_issues"][0][
        "capability"
    ] == "deployment_capability_evidence_not_yet_valid"


def test_unbound_deployment_capability_evidence_is_blocked() -> None:
    provider = _AvailableProvider().capabilities
    profiles = ExecutionProfileRegistry()
    profiles.register(
        "trusted",
        ExecutionProfile.trusted_in_process(),
    )
    providers = ExecutionEnvironmentRegistry()
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="unbound-evidence",
        profile_registry=profiles,
        execution_registry=providers,
        deployment_capability_evidence=(_deployment_evidence(provider),),
    )
    composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
    )

    issue = composition.diagnostics()["deployment_capability_evidence_issues"][0]
    assert issue["capability"] == "deployment_capability_evidence_unbound"



def test_expired_deployment_capability_evidence_is_blocked() -> None:
    provider = _AvailableProvider().capabilities
    start = datetime.now(UTC) - timedelta(hours=2)
    evidence = _deployment_evidence(
        provider,
        qualified_at=start,
        expires_at=start + timedelta(minutes=1),
    )
    composition = _qualified_composition((evidence,))

    assert composition.diagnostics()["deployment_capability_evidence_issues"][0][
        "capability"
    ] == "deployment_capability_evidence_expired"


def test_deployment_evidence_requires_rollback_reference() -> None:
    provider = _AvailableProvider().capabilities
    with pytest.raises(ValueError, match="rollback_evidence_ref"):
        _deployment_evidence(provider, rollback_evidence_ref="")
    payload = _deployment_evidence(provider).to_dict()
    payload.pop("rollback_evidence_ref")
    with pytest.raises(TypeError, match="rollback_evidence_ref"):
        DeploymentCapabilityEvidence.from_dict(payload)


def test_unrequired_provider_keeps_legacy_composition_ready_without_evidence() -> None:
    _legacy_composition, _profiles, providers = _composition()
    provider = _AvailableProvider()
    providers.register(provider)

    # The registry drift is intentionally outside the composition's required
    # provider set; startup readiness remains backward compatible only when no
    # physical provider is required by this role.
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="trusted-with-catalogued-provider",
        profile_registry=_profiles,
        execution_registry=providers,
    )
    composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=_profiles,
        execution_registry=providers,
    )
    assert composition.diagnostics()["status"] == "ready"


def test_required_execution_provider_blocks_readiness_with_stable_denial() -> None:
    profiles = ExecutionProfileRegistry()
    profiles.register(
        "external",
        ExecutionProfile.external_process(
            provider_id="offline",
            allowed_argv_prefixes=(("worker",),),
        ),
    )
    providers = ExecutionEnvironmentRegistry()
    provider: ExecutionEnvironmentPort = _UnavailableProvider()
    providers.register(provider)
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="offline-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("offline",),
    )

    diagnostics = composition.diagnostics()
    assert diagnostics["status"] == "blocked"
    assert diagnostics["required_providers"] == ["offline"]
    assert diagnostics["unavailable_providers"] == ["offline"]

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        composition.require_ready()

    assert raised.value.details["denial_code_version"] == (
        "newsroom.execution-capability-denials/v1"
    )
    assert raised.value.details["denial_code"] == "execution_provider_unavailable"


def test_unregistered_required_provider_is_reported_without_provider_fallback() -> None:
    profiles = ExecutionProfileRegistry()
    profiles.register("trusted", ExecutionProfile.trusted_in_process())
    providers = ExecutionEnvironmentRegistry()
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="missing-provider-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("docker",),
    )

    assert composition.diagnostics()["unavailable_providers"] == ["docker"]
    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        composition.require_ready()

    assert raised.value.details["provider_ids"] == ["docker"]
