from __future__ import annotations

import json
from pathlib import Path

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionProfile,
    ExecutionProfileRegistry,
    RuntimeCompositionManifest,
    RuntimeExecutionComposition,
)
from framework.execution_environment.ports import ExecutionEnvironmentPort
from interfaces.services.diagnose_service import (
    DiagnoseCheck,
    DiagnoseResult,
    DiagnosticApplicationService,
)


class _UnavailableProvider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(provider_id="offline", available=False)

    def execute(self, request):
        raise AssertionError("unavailable provider must not execute")


class _AvailableProvider:
    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(provider_id="docker", available=True)

    def execute(self, request):
        raise AssertionError("diagnostic tests must not execute a provider")


def _missing_evidence_composition() -> RuntimeExecutionComposition:
    profiles = ExecutionProfileRegistry()
    profiles.register("trusted", ExecutionProfile.trusted_in_process())
    providers = ExecutionEnvironmentRegistry()
    providers.register(_AvailableProvider())
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="diagnostic-missing-evidence",
        profile_registry=profiles,
        execution_registry=providers,
    )
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("docker",),
    )


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
        composition_id="diagnostic-offline-process",
        profile_registry=profiles,
        execution_registry=providers,
    )
    return RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=providers,
        required_provider_ids=("offline",),
    )


def test_diagnose_result_aggregates_warning() -> None:
    result = DiagnoseResult.from_checks(
        [
            DiagnoseCheck("redis", "Redis", "ok", "ok"),
            DiagnoseCheck("dashscope", "DashScope", "warning", "missing"),
            DiagnoseCheck("postgres", "PostgreSQL", "skipped", "not configured"),
        ]
    )

    assert result.status == "warning"
    assert result.summary == "1 ok, 1 warning, 0 error, 1 skipped"


def test_diagnostic_service_runs_injected_checks() -> None:
    service = DiagnosticApplicationService(
        checks=[
            lambda: DiagnoseCheck("redis", "Redis", "ok", "ok"),
            lambda: DiagnoseCheck("qdrant", "Qdrant", "error", "down"),
        ]
    )

    result = service.run()

    assert result.status == "error"
    assert [check.check_id for check in result.checks] == ["redis", "qdrant"]


def test_diagnostic_service_reports_missing_deployment_evidence_as_error() -> None:
    service = DiagnosticApplicationService(
        runtime_execution_composition=_missing_evidence_composition(),
        checks=[],
    )

    check = service._check_runtime_composition()

    assert check.status == "error"
    assert check.details["unavailable_providers"] == []
    assert check.details["deployment_capability_evidence_issues"] == [
        {
            "provider_id": "docker",
            "capability": "deployment_capability_evidence_missing",
            "denial_code": "execution_deployment_capability_evidence_missing",
        }
    ]
    assert "deployment qualification evidence" in (check.remediation or "")


def test_diagnostic_service_reports_required_provider_unavailable_as_error() -> None:
    service = DiagnosticApplicationService(
        runtime_execution_composition=_unavailable_composition(),
        checks=[],
    )

    check = service._check_runtime_composition()

    assert check.status == "error"
    assert check.details["reason_code"] == "execution_environment_unavailable"
    assert check.details["details"]["denial_code"] == "execution_provider_unavailable"
    assert check.details["unavailable_providers"] == ["offline"]


def test_dashscope_key_check_does_not_expose_secret() -> None:
    service = DiagnosticApplicationService(env={"DASHSCOPE_API_KEY": "secret-value"}, checks=[])

    check = service._check_dashscope_key()

    assert check.status == "ok"
    assert "secret-value" not in str(check.to_dict())


def test_diagnostic_service_validates_tracked_source_config() -> None:
    service = DiagnosticApplicationService(env={}, checks=[])

    check = service._check_source_config()

    assert check.status == "ok"
    assert check.details["source_count"] >= 3


def test_diagnostic_service_reports_source_config_parse_error_without_exposing_file_content(
    tmp_path: Path,
) -> None:
    secret = "sk-test-secret-value"
    path = tmp_path / "sources.yaml"
    path.write_text(f"api_key: [{secret}\n", encoding="utf-8")
    service = DiagnosticApplicationService(env={"NEWS_SOURCES_CONFIG": str(path)}, checks=[])

    check = service._check_source_config()

    assert check.check_id == "source_config"
    assert check.status == "error"
    assert "not valid YAML" in check.message
    assert str(path) in check.message
    assert secret not in str(check.to_dict())


def test_diagnostic_service_warns_for_missing_model_env_without_exposing_secret() -> None:
    service = DiagnosticApplicationService(env={}, checks=[])

    check = service._check_model_config()

    assert check.status == "warning"
    assert "DASHSCOPE_API_KEY" in check.message
    assert "sk-" not in str(check.to_dict())


def test_diagnostic_service_reports_model_config_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "routez": {},
                "model_groups": {
                    "writer-group": {
                        "deployments": [
                            {
                                "deployment_id": "writer-primary",
                                "provider": "openai-compatible",
                                "provider_name": "test-provider",
                                "model": "test-model",
                                "api_base": "https://llm.example/v1",
                                "api_key": "${TEST_LLM_API_KEY}",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = DiagnosticApplicationService(env={"NEWS_MODELS_CONFIG": str(path)}, checks=[])

    check = service._check_model_config()

    assert check.check_id == "model_config"
    assert check.status == "error"
    assert "unsupported field(s): routez" in check.message
    assert check.details["path"] == str(path)


def test_diagnostic_service_reports_model_config_parse_error_without_exposing_file_content(
    tmp_path: Path,
) -> None:
    secret = "sk-test-secret-value"
    path = tmp_path / "models.yaml"
    path.write_text(f"api_key: [{secret}\n", encoding="utf-8")
    service = DiagnosticApplicationService(env={"NEWS_MODELS_CONFIG": str(path)}, checks=[])

    check = service._check_model_config()

    assert check.check_id == "model_config"
    assert check.status == "error"
    assert "not valid YAML" in check.message
    assert str(path) in check.message
    assert secret not in str(check.to_dict())


def test_diagnostic_service_model_config_does_not_expose_secret() -> None:
    service = DiagnosticApplicationService(env={"DASHSCOPE_API_KEY": "secret-value"}, checks=[])

    check = service._check_model_config()

    assert check.status == "ok"
    assert "secret-value" not in str(check.to_dict())
