from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.local_desktop.runtime_config as runtime_config_module
from scripts.desktop_app.config import resolve_desktop_app_config
from scripts.local_desktop.common import DesktopOperationError, atomic_write_json
from scripts.local_desktop.runtime_config import (
    FIXED_CREDENTIAL_ENV,
    FIXED_CREDENTIAL_REF,
    RuntimeConfigManager,
    candidate_document,
    models_config_payload,
    read_runtime_config_document,
    runtime_environment_overlay,
    runtime_config_paths_for_data_root,
)


class FakeReleaseOwner:
    def __init__(self, root: Path, *, fail_candidate_activation: bool = False) -> None:
        self.active_path = root / "releases/base"
        self.active_path.mkdir(parents=True)
        self.active_manifest = {"identity": {"release_id": "base-release"}}
        self.fail_candidate_activation = fail_candidate_activation
        self.activations: list[Path] = []
        self.rebinds: list[dict[str, object]] = []

    def active(self):
        return self.active_path, self.active_manifest

    def rebind_runtime_configuration(self, release, *, runtime_config_document):
        candidate = self.active_path.parent / "base-release-config-test"
        candidate.mkdir(exist_ok=True)
        self.rebinds.append({"release": Path(release), "document": runtime_config_document})
        return {
            "release_path": str(candidate),
            "manifest": {"identity": {"release_id": "base-release-config-test"}},
            "reused": False,
        }

    def activate(self, release):
        selected = Path(release)
        self.activations.append(selected)
        if self.fail_candidate_activation and "config-test" in selected.name:
            raise DesktopOperationError("desktop_release_activation_failed", "activation failed")
        self.active_path = selected
        self.active_manifest = {"identity": {"release_id": selected.name}}
        return {"release_path": str(selected), "release_id": selected.name}


class FakeCredentialProvider:
    def configured(self, credential_ref: str) -> bool:
        assert credential_ref == FIXED_CREDENTIAL_REF
        return True

    def project_environment(self, credential_ref: str, *, environment_name: str):
        raise AssertionError("configuration owner must not read credential values")


def _config(tmp_path: Path):
    resource = tmp_path / "runtime"
    resource.mkdir()
    return resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "data", environ={})


def _candidate() -> dict[str, object]:
    return {
        "llm": {
            "route_id": "writer-primary",
            "provider": "openai-compatible",
            "model": "research-model-1",
            "base_url": "https://api.example.test/v1",
            "credential_ref": FIXED_CREDENTIAL_REF,
            "credential_env": FIXED_CREDENTIAL_ENV,
        },
        "source": {
            "preset": "arxiv-default",
            "api_url": "https://export.arxiv.org/api/query",
            "timeout_seconds": 45,
        },
    }


def test_candidate_uses_existing_model_and_research_settings_validators() -> None:
    document = candidate_document(_candidate(), revision=3)
    models = models_config_payload(document)
    environment = runtime_environment_overlay(document, models_path=Path(r"C:\owned\models.json"))

    assert models["routes"]["writer-primary"]["deployment_id"] == "desktop-research-primary"
    assert models["deployments"][0]["model"] == "research-model-1"
    assert environment["NEWS_RESEARCH_LLM_MODEL"] == "research-model-1"
    assert environment["NEWS_RESEARCH_LLM_BASE_URL"] == "https://api.example.test/v1"
    assert environment["NEWS_RESEARCH_LLM_API_KEY_ENV"] == FIXED_CREDENTIAL_ENV
    assert environment["NEWS_RESEARCH_ARXIV_API_URL"] == "https://export.arxiv.org/api/query"
    assert environment["NEWS_RESEARCH_SOURCE_TIMEOUT_SECONDS"] == "45"


@pytest.mark.parametrize(
    "candidate,code",
    [
        ({"llm": {"model": "sk-secret-value", "base_url": "https://api.example/v1"}}, "desktop_runtime_secret_rejected"),
        ({"llm": {"model": "m", "base_url": "https://user:pass@example.test/v1"}}, "desktop_runtime_url_invalid"),
        ({"llm": {"model": "m", "base_url": "https://api.example/v1", "credential_env": "ARBITRARY_KEY"}}, "desktop_runtime_credential_reference_invalid"),
        ({"source": {"preset": "news-default"}}, "desktop_runtime_source_preset_invalid"),
        ({"source": {"timeout_seconds": True}}, "desktop_runtime_source_timeout_invalid"),
        ({"source": {"api_url": "https://export.arxiv.org/api/query?api_key=secret"}}, "desktop_runtime_url_invalid"),
        ({"source": {"api_url": "https://export.arxiv.org/api/token/query"}}, "desktop_runtime_url_invalid"),
        ({"llm": {"model": "m", "base_url": "https://api.example/v1?token=secret"}}, "desktop_runtime_url_invalid"),
        ({"models_path": "C:/arbitrary.json"}, "desktop_runtime_candidate_unknown_field"),
    ],
)
def test_candidate_rejects_secrets_arbitrary_bindings_and_news_sources(candidate, code: str) -> None:
    with pytest.raises(DesktopOperationError) as raised:
        candidate_document(candidate, revision=1)
    assert raised.value.reason_code == code


def test_preview_and_apply_use_cas_one_time_confirmation_and_immutable_rebind(tmp_path: Path) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root)
    manager = RuntimeConfigManager(config, release_manager=releases, credential_provider=FakeCredentialProvider())

    preview = manager.preview(_candidate())
    token = preview.details["confirmation_token"]
    result = manager.apply(_candidate(), expected_revision=0, confirmation_token=token)

    stored = read_runtime_config_document(manager.paths.document)
    assert result.status == "APPLIED"
    assert stored["revision"] == 1
    assert stored["research"]["llm"]["model"] == "research-model-1"
    assert releases.rebinds[0]["release"].name == "base"
    assert releases.activations[-1].name == "base-release-config-test"
    assert (config.data_root / "desktop/config/models.json").is_file()
    assert not manager.paths.journal.exists()
    with pytest.raises(DesktopOperationError) as replay:
        manager.apply(_candidate(), expected_revision=0, confirmation_token=token)
    assert replay.value.reason_code == "desktop_runtime_config_revision_conflict"


def test_apply_rolls_back_document_and_active_release_when_activation_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root, fail_candidate_activation=True)
    manager = RuntimeConfigManager(config, release_manager=releases)
    token = manager.preview(_candidate()).details["confirmation_token"]

    with pytest.raises(DesktopOperationError) as raised:
        manager.apply(_candidate(), expected_revision=0, confirmation_token=token)

    assert raised.value.reason_code == "desktop_release_activation_failed"
    assert read_runtime_config_document(manager.paths.document)["revision"] == 0
    assert releases.activations[-1].name == "base"
    assert not manager.paths.journal.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_preview_rejects_private_config_junction_before_writing_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    private_root = config.data_root / "desktop/config"
    private_root.parent.mkdir(parents=True)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(private_root), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")
    manager = RuntimeConfigManager(config, release_manager=FakeReleaseOwner(config.data_root))
    try:
        with pytest.raises(DesktopOperationError) as raised:
            manager.preview(_candidate())
        assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert not any(target.iterdir())
    finally:
        if os.path.lexists(private_root):
            os.rmdir(private_root)


def test_desktop_config_projects_applied_model_and_arxiv_settings_consistently(tmp_path: Path) -> None:
    resource = tmp_path / "runtime-projection"
    data = tmp_path / "data-projection"
    resource.mkdir()
    document = candidate_document(_candidate(), revision=1)
    models_path = data / "desktop/config/models.json"
    atomic_write_json(models_path, models_config_payload(document))
    atomic_write_json(data / "desktop/config/research-runtime.json", document)

    config = resolve_desktop_app_config(resource_root=resource, data_root=data, environ={})

    assert config.desktop.runtime_config_path == data / "config.json"
    assert not config.desktop.runtime_config_path.exists()
    assert config.environment["NEWS_MODELS_CONFIG"] == str(models_path)
    assert config.environment["NEWS_RESEARCH_MODELS_CONFIG"] == str(models_path)
    assert config.environment["NEWS_RESEARCH_LLM_MODEL"] == "research-model-1"
    assert config.environment["NEWS_RESEARCH_LLM_BASE_URL"] == "https://api.example.test/v1"
    assert config.environment["NEWS_RESEARCH_LLM_API_KEY_ENV"] == FIXED_CREDENTIAL_ENV
    assert config.environment["NEWS_RESEARCH_ARXIV_API_URL"] == "https://export.arxiv.org/api/query"
    assert FIXED_CREDENTIAL_ENV not in config.environment


def test_effect_scope_is_honest_about_guided_public_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = RuntimeConfigManager(config, release_manager=FakeReleaseOwner(config.data_root))

    preview = manager.preview(_candidate())

    assert preview.details["effect_scope"] == {
        "llm_consumers": ["research", "guided-intent"],
        "source_consumers": ["research-arxiv-provider"],
        "not_changed": ["guided-paper-search-public-cache", "guided-project-search"],
    }



def test_before_stop_admission_uses_same_transaction_lock_and_apply_rechecks_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root)
    manager = RuntimeConfigManager(config, release_manager=releases)
    preview = manager.preview(_candidate())
    entered: list[Path] = []
    real_lock = runtime_config_module.exclusive_lock

    def tracked_lock(path: Path):
        entered.append(path)
        return real_lock(path)

    monkeypatch.setattr(runtime_config_module, "exclusive_lock", tracked_lock)
    admission = manager.admit(
        _candidate(),
        expected_revision=0,
        confirmation_token=preview.details["confirmation_token"],
    )
    result = manager.apply(
        _candidate(),
        expected_revision=0,
        confirmation_token=preview.details["confirmation_token"],
    )

    assert admission["candidate_document"]["revision"] == 1
    assert result.status == "APPLIED"
    assert entered == [manager.paths.lock, manager.paths.lock]

def test_apply_rechecks_revision_inside_transaction_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root)
    manager = RuntimeConfigManager(config, release_manager=releases)
    first_preview = manager.preview(_candidate())
    second_candidate = _candidate() | {
        "llm": _candidate()["llm"] | {"model": "research-model-2"},
    }
    second_preview = manager.preview(second_candidate)

    manager.apply(_candidate(), expected_revision=0, confirmation_token=first_preview.details["confirmation_token"])
    with pytest.raises(DesktopOperationError) as raised:
        manager.apply(second_candidate, expected_revision=0, confirmation_token=second_preview.details["confirmation_token"])

    assert raised.value.reason_code == "desktop_runtime_config_revision_conflict"
    assert read_runtime_config_document(manager.paths.document)["research"]["llm"]["model"] == "research-model-1"
    assert len(releases.rebinds) == 1


def test_config_resolution_repairs_missing_and_tampered_models_under_cas_lock(tmp_path: Path) -> None:
    resource = tmp_path / "runtime-repair"
    data = tmp_path / "data-repair"
    resource.mkdir()
    document = candidate_document(_candidate(), revision=4)
    atomic_write_json(data / "desktop/config/research-runtime.json", document)

    first = resolve_desktop_app_config(resource_root=resource, data_root=data, environ={})
    models_path = data / "desktop/config/models.json"
    assert models_path.is_file()
    expected = models_config_payload(document)
    assert json.loads(models_path.read_text(encoding="utf-8")) == expected

    models_path.write_text("{}", encoding="utf-8")
    second = resolve_desktop_app_config(resource_root=resource, data_root=data, environ={})
    assert json.loads(models_path.read_text(encoding="utf-8")) == expected
    assert second.environment["NEWS_RESEARCH_LLM_MODEL"] == "research-model-1"
    assert first.environment["NEWS_RESEARCH_MODELS_CONFIG"] == second.environment["NEWS_RESEARCH_MODELS_CONFIG"]



def test_journal_recovery_restores_previous_disabled_projection_after_prepared_crash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root)
    manager = RuntimeConfigManager(config, release_manager=releases)
    previous = read_runtime_config_document(manager.paths.document)
    candidate = candidate_document(_candidate(), revision=1)
    candidate_release = config.data_root / "releases/candidate"
    candidate_release.mkdir(parents=True)
    atomic_write_json(manager.paths.models, models_config_payload(candidate))
    atomic_write_json(
        manager.paths.journal,
        {
            "schema": "newsroom.desktop-runtime-config-journal/v1",
            "status": "prepared",
            "previous_document": previous,
            "previous_release_path": str(releases.active_path),
            "candidate_document": candidate,
            "candidate_release_path": str(candidate_release),
        },
    )

    reopened_before_recovery = resolve_desktop_app_config(
        resource_root=config.resource_root, data_root=config.data_root, environ=config.environment
    )
    assert "NEWS_RESEARCH_LLM_PROVIDER" not in reopened_before_recovery.environment
    assert manager.paths.models.is_file()

    recovered = manager.recover_pending()
    reopened = resolve_desktop_app_config(
        resource_root=config.resource_root, data_root=config.data_root, environ=reopened_before_recovery.environment
    )

    assert recovered["status"] == "rolled_back"
    assert read_runtime_config_document(manager.paths.document) == previous
    assert not manager.paths.models.exists()
    assert not manager.paths.journal.exists()
    assert "NEWS_RESEARCH_LLM_PROVIDER" not in reopened.environment
    assert "NEWS_MODELS_CONFIG" not in reopened.environment


def test_journal_recovery_recreates_missing_candidate_models_before_reopen(tmp_path: Path) -> None:
    config = _config(tmp_path)
    releases = FakeReleaseOwner(config.data_root)
    manager = RuntimeConfigManager(config, release_manager=releases)
    previous = read_runtime_config_document(manager.paths.document)
    candidate = candidate_document(_candidate(), revision=1)
    candidate_release = config.data_root / "releases/candidate"
    candidate_release.mkdir(parents=True)
    atomic_write_json(manager.paths.document, candidate)
    atomic_write_json(
        manager.paths.journal,
        {
            "schema": "newsroom.desktop-runtime-config-journal/v1",
            "status": "config_written",
            "previous_document": previous,
            "previous_release_path": str(releases.active_path),
            "candidate_document": candidate,
            "candidate_release_path": str(candidate_release),
        },
    )
    assert not manager.paths.models.exists()

    recovered = manager.recover_pending()
    reopened = resolve_desktop_app_config(
        resource_root=config.resource_root, data_root=config.data_root, environ=config.environment
    )

    assert recovered["status"] == "completed"
    assert read_runtime_config_document(manager.paths.document) == candidate
    assert json.loads(manager.paths.models.read_text(encoding="utf-8")) == models_config_payload(candidate)
    assert reopened.environment["NEWS_RESEARCH_LLM_MODEL"] == "research-model-1"
    assert reopened.environment["NEWS_MODELS_CONFIG"] == str(manager.paths.models)

def test_config_resolution_preserves_candidate_models_while_journal_requires_recovery(tmp_path: Path) -> None:
    resource = tmp_path / "runtime-journal"
    data = tmp_path / "data-journal"
    resource.mkdir()
    paths = runtime_config_paths_for_data_root(data)
    current = read_runtime_config_document(paths.document)
    candidate = candidate_document(_candidate(), revision=1)
    atomic_write_json(paths.models, models_config_payload(candidate))
    atomic_write_json(
        paths.journal,
        {
            "schema": "newsroom.desktop-runtime-config-journal/v1",
            "status": "prepared",
            "previous_document": current,
            "previous_release_path": str(data / "releases/base"),
            "candidate_document": candidate,
            "candidate_release_path": str(data / "releases/candidate"),
        },
    )

    config = resolve_desktop_app_config(resource_root=resource, data_root=data, environ={})

    assert config.environment.get("NEWS_RESEARCH_LLM_MODEL") is None
    assert paths.models.is_file()
    assert json.loads(paths.models.read_text(encoding="utf-8")) == models_config_payload(candidate)


@pytest.mark.parametrize("persisted_null", [False, True])
def test_app_null_disables_actual_research_composition_despite_inherited_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted_null: bool
) -> None:
    import socket

    import interfaces.composition.research as research_composition
    from interfaces.composition.research_errors import ResearchRuntimeUnavailableError
    from interfaces.composition.research_settings import (
        DESKTOP_RESEARCH_LLM_ENABLED_ENV,
        ResearchRuntimeSettings,
    )
    from interfaces.services.research_service import ResearchAnalyzeInput, ResearchServiceError
    from scripts.local_desktop.config import sanitized_environment

    resource = tmp_path / "runtime"
    (resource / "configs").mkdir(parents=True)
    (resource / "configs/models.yaml").write_text("providers: {}\n", encoding="utf-8")
    data = tmp_path / "data"
    if persisted_null:
        atomic_write_json(
            runtime_config_paths_for_data_root(data).document,
            candidate_document({"llm": None}, revision=3),
        )
    inherited = {
        # An inherited enable claim must not override the trusted document.
        DESKTOP_RESEARCH_LLM_ENABLED_ENV: "1",
        "NEWS_LLM_PROVIDER": "openai-compatible",
        "NEWS_LLM_MODEL": "inherited-model",
        "NEWS_LLM_BASE_URL": "https://inherited.example/v1",
        "NEWS_LLM_API_KEY_ENV": "SYNTHETIC_PROVIDER_KEY",
        "SYNTHETIC_PROVIDER_KEY": "synthetic-provider-credential",
        "OPENAI_MODEL": "inherited-openai-model",
        "OPENAI_BASE_URL": "https://openai.example/v1",
        "OPENAI_API_KEY": "synthetic-openai-credential",
        "DASHSCOPE_API_KEY": "synthetic-dashscope-credential",
        FIXED_CREDENTIAL_ENV: "synthetic-desktop-credential",
    }

    def forbidden(*args, **kwargs):
        pytest.fail("disabled Research must not build a remote worker or access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(research_composition, "_build_configured_composition", forbidden)
    config = resolve_desktop_app_config(resource_root=resource, data_root=data, environ=inherited)
    # Exercise the real child environment projection too, not merely UI config.
    child_environment = sanitized_environment(config.desktop, base=config.environment)
    assert child_environment[DESKTOP_RESEARCH_LLM_ENABLED_ENV] == "0"
    assert child_environment["OPENAI_API_KEY"] == "synthetic-openai-credential"
    with pytest.raises(ResearchRuntimeUnavailableError) as disabled:
        ResearchRuntimeSettings.from_env(child_environment, cwd=resource)
    assert disabled.value.capabilities == ("research.llm.route",)

    composition = research_composition.build_research_runtime_composition(
        settings_factory=lambda: ResearchRuntimeSettings.from_env(child_environment, cwd=resource)
    )
    assert composition.available is False
    assert composition.settings is None
    assert composition.availability_error.capabilities == ("research.llm.route",)
    with pytest.raises(ResearchServiceError) as raised:
        composition.service.analyze_paper(
            ResearchAnalyzeInput(paper_id="2607.00001", source_url="https://arxiv.org/abs/2607.00001")
        )
    assert raised.value.code == "research_runtime_unavailable"
    assert raised.value.status_code == 503
    assert raised.value.details["capabilities"] == ["research.llm.route"]
    assert not (data / "runs/artifacts").exists()
    composition.close()


def test_app_configured_llm_overrides_inherited_disable_and_builds_actual_settings(tmp_path: Path) -> None:
    from interfaces.composition.research_settings import (
        DESKTOP_RESEARCH_LLM_ENABLED_ENV,
        ResearchRuntimeSettings,
    )

    resource = tmp_path / "runtime-enabled"
    resource.mkdir()
    data = tmp_path / "data-enabled"
    atomic_write_json(
        runtime_config_paths_for_data_root(data).document,
        candidate_document(_candidate(), revision=1),
    )
    config = resolve_desktop_app_config(
        resource_root=resource,
        data_root=data,
        environ={
            DESKTOP_RESEARCH_LLM_ENABLED_ENV: "0",
            FIXED_CREDENTIAL_ENV: "synthetic-desktop-credential",
        },
    )

    settings = ResearchRuntimeSettings.from_env(config.environment, cwd=resource)

    assert config.environment[DESKTOP_RESEARCH_LLM_ENABLED_ENV] == "1"
    assert settings.llm.model == "research-model-1"
    assert settings.llm.api_key_env == FIXED_CREDENTIAL_ENV
    assert settings.llm.models_config_path == runtime_config_paths_for_data_root(data).models
