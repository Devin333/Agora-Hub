from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionProfile,
    ExecutionProfileRegistry,
    RuntimeCompositionDriftError,
    RuntimeCompositionManifest,
    RuntimeExecutionComposition,
)
from framework.execution_environment.ports import ExecutionEnvironmentPort
from interfaces.api import create_app
from interfaces.composition.runtime_execution import build_process_execution_composition
from interfaces.services.worker_service import WorkerApplicationService


def _composition() -> tuple[RuntimeExecutionComposition, ExecutionProfileRegistry]:
    profiles = ExecutionProfileRegistry()
    profiles.register("trusted", ExecutionProfile.trusted_in_process())
    providers = ExecutionEnvironmentRegistry()
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="api-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    return (
        RuntimeExecutionComposition(
            manifest=manifest,
            profile_registry=profiles,
            execution_registry=providers,
        ),
        profiles,
    )


class _UnavailableProvider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(provider_id="offline", available=False)

    def execute(self, request):
        raise AssertionError("unavailable provider must not execute")


def _unavailable_composition() -> RuntimeExecutionComposition:
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
        composition_id="unavailable-api-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("offline",),
    )


def test_api_readiness_exposes_composition_fingerprint() -> None:
    composition, _profiles = _composition()
    client = TestClient(create_app(runtime_execution_composition=composition, audit_emitter_factory=None))

    response = client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()["data"]["runtime_composition"]
    assert payload["manifest_fingerprint"] == composition.fingerprint
    assert payload["composition_id"] == "api-process"


def test_api_readiness_fails_closed_after_composition_drift() -> None:
    composition, profiles = _composition()
    client = TestClient(create_app(runtime_execution_composition=composition, audit_emitter_factory=None))
    profiles.register("another-trusted", ExecutionProfile.trusted_in_process())

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_composition_drift"


def test_api_readiness_fails_closed_when_required_provider_is_unavailable() -> None:
    client = TestClient(
        create_app(
            runtime_execution_composition=_unavailable_composition(),
            audit_emitter_factory=None,
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "execution_environment_unavailable"
    assert error["message"] == "runtime execution composition is not ready"
    assert error["retryable"] is True
    assert error["details"] == {
        "provider_ids": ["offline"],
        "missing": ["provider_unavailable"],
        "denial_code_version": "newsroom.execution-capability-denials/v1",
        "denial_code": "execution_provider_unavailable",
        "denials": [
            {
                "provider_id": "offline",
                "capability": "provider_unavailable",
                "denial_code": "execution_provider_unavailable",
            }
        ],
    }


def test_default_process_roots_share_manifest_fingerprint() -> None:
    app = create_app(audit_emitter_factory=None)
    worker = WorkerApplicationService(queue=object(), handlers={})
    composition = build_process_execution_composition()

    assert app.state.runtime_execution_composition.fingerprint == composition.fingerprint
    assert worker.runtime_execution_composition.fingerprint == composition.fingerprint


def test_process_roots_reject_configured_manifest_fingerprint_drift(monkeypatch) -> None:
    baseline = build_process_execution_composition()
    monkeypatch.setenv(
        "NEWSROOM_RUNTIME_COMPOSITION_FINGERPRINT",
        "sha256:" + "0" * 64,
    )

    with pytest.raises(RuntimeCompositionDriftError) as raised:
        build_process_execution_composition()

    assert raised.value.details["expected_manifest_fingerprint"] == (
        "sha256:" + "0" * 64
    )
    assert raised.value.details["actual_manifest_fingerprint"] == baseline.fingerprint


def test_process_composition_publishes_profile_catalog_and_docker_denial_contract() -> None:
    composition = build_process_execution_composition()

    diagnostics = composition.diagnostics()
    assert diagnostics["profiles"] == [
        "research-parser-marker",
        "research-parser-mineru",
        "runtime-trusted-in-process",
    ]
    assert composition.manifest.metadata["profile_catalog"] == {
        "trusted_in_process": ["runtime-trusted-in-process"],
        "sandboxed": [],
        "external_process": [
            "research-parser-marker",
            "research-parser-mineru",
        ],
    }
    assert diagnostics["profile_catalog"] == {
        "research-parser-marker": composition.profile_registry.resolve(
            "research-parser-marker"
        ).to_dict(),
        "research-parser-mineru": composition.profile_registry.resolve(
            "research-parser-mineru"
        ).to_dict(),
        "runtime-trusted-in-process": composition.profile_registry.resolve(
            "runtime-trusted-in-process"
        ).to_dict(),
    }

    docker = diagnostics["provider_capabilities"]["docker"]
    assert docker["enforces_network_deny"] is True
    assert docker["enforces_network_allowlist"] is False
    assert docker["supports_secret_handles"] is False
    assert docker["enforces_cpu_limits"] is False
    assert docker["enforces_child_process_allowlist"] is False
    assert docker["enforces_timeout"] is True
    assert docker["supports_cancellation"] is True
