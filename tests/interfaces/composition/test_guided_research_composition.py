from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.research.ports.guided_research import GuidedIntentCommand, GuidedResearchError
from framework.llm.clients.config import OpenAICompatibleDeploymentConfig
from framework.llm.clients.openai_compatible import OpenAICompatibleConfig
from interfaces.api.app import create_app
import interfaces.composition.guided_research as composition
from interfaces.composition.research_settings import DESKTOP_RESEARCH_LLM_ENABLED_ENV
from scripts.local_desktop.runtime_config import (
    FIXED_CREDENTIAL_ENV,
    FIXED_CREDENTIAL_REF,
    candidate_document,
    models_config_payload,
    runtime_environment_overlay,
)


@pytest.fixture(autouse=True)
def isolated_composition(monkeypatch):
    # Windows asyncio creates a loopback self-pipe internally. Allocate it before
    # denying sockets so application code has no network exception/allow-list.
    loop = asyncio.new_event_loop()
    for name in tuple(composition.os.environ):
        if name.startswith(("NEWS_RESEARCH_", "NEWS_LLM_", "OPENAI_")) or name in {
            DESKTOP_RESEARCH_LLM_ENABLED_ENV, "NEWS_MODELS_CONFIG", FIXED_CREDENTIAL_ENV,
            "DASHSCOPE_API_KEY",
        }:
            monkeypatch.delenv(name, raising=False)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("guided composition regression must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)
    monkeypatch.setattr(composition, "build_research_application_service", lambda: SimpleNamespace())
    composition.reset_guided_research_service()
    yield loop
    composition.reset_guided_research_service()
    if not loop.is_closed():
        loop.close()


def _forbid_model_resolution(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled intent must not resolve a model, client, or worker")

    for name in ("load_openai_compatible_deployment", "OpenAICompatibleClient", "StructuredGuidedIntentCandidateWorker"):
        monkeypatch.setattr(composition, name, forbidden)


def _client(loop):
    return TestClient(create_app(
        guided_research_service_factory=composition.build_guided_research_service,
        audit_emitter_factory=None,
    ), backend_options={"loop_factory": lambda: loop})


def test_explicit_disabled_intent_has_real_503_admission_without_inherited_provider(isolated_composition, monkeypatch):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, "0")
    monkeypatch.setenv("NEWS_LLM_API_KEY", "global-fake-key")
    monkeypatch.setenv("NEWS_LLM_MODEL", "global-default-model")
    monkeypatch.setenv(FIXED_CREDENTIAL_ENV, "retained-vault-fake-key")
    _forbid_model_resolution(monkeypatch)

    response = _client(isolated_composition).post("/api/v1/research/guided/intent", json={"question": "找 agent 论文"})

    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "guided_intent_unavailable"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["user_action_required"] is True
    assert payload["error"]["details"]["code"] == "research_runtime_unavailable"
    assert payload["error"]["details"]["capabilities"] == ["research.llm.route"]
    assert "global-fake-key" not in response.text
    assert "retained-vault-fake-key" not in response.text
    assert payload["data"] is None


def test_disabled_intent_does_not_disable_real_public_cache_search(tmp_path: Path, isolated_composition, monkeypatch):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, "0")
    _forbid_model_resolution(monkeypatch)
    cache = tmp_path / "verified-cache.json"
    cache.write_text(json.dumps({
        "collectedAt": "2026-10-14T00:00:00Z",
        "papers": [{
            "id": "paper-fixture", "slug": "paper-fixture", "title": "Agent evaluation",
            "abstractSnippet": "A cached public paper about agent evaluation.", "authors": ["Fixture Author"],
            "publishedAt": "2026-10-01T00:00:00Z", "paperUrl": "https://arxiv.org/abs/2610.00001",
            "isPublished": True,
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("NEWSROOM_PAPERS_DATA_PATH", str(cache))

    response = _client(isolated_composition).post("/api/v1/research/guided/search", json={
        "source": "papers",
        "intent": {"summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None},
    })

    assert response.status_code == 200, response.text
    assert response.json()["data"]["results"][0]["id"] == "paper-fixture"
    assert response.json()["data"]["total"] == 1


def test_disabled_intent_does_not_fabricate_search_when_public_cache_is_missing(tmp_path: Path, isolated_composition, monkeypatch):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, "0")
    monkeypatch.setenv("NEWSROOM_PAPERS_DATA_PATH", str(tmp_path / "missing-cache.json"))
    _forbid_model_resolution(monkeypatch)

    response = _client(isolated_composition).post("/api/v1/research/guided/search", json={
        "source": "papers",
        "intent": {"summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None},
    })

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "guided_papers_source_unavailable"
    assert response.json()["data"] is None


def _enable_owned_model(tmp_path: Path, monkeypatch):
    document = candidate_document({"llm": {
        "route_id": "app-guided-route", "provider": "openai-compatible", "model": "app-bound-model",
        "base_url": "https://app-bound.example.test/v1", "credential_ref": FIXED_CREDENTIAL_REF,
        "credential_env": FIXED_CREDENTIAL_ENV,
    }}, revision=1)
    models = tmp_path / "owned-models.json"
    models.write_text(json.dumps(models_config_payload(document)), encoding="utf-8")
    for name, value in runtime_environment_overlay(document, models_path=models).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(FIXED_CREDENTIAL_ENV, "owned-fake-key")
    # These global overrides must not replace the owner's route/deployment.
    monkeypatch.setenv("NEWS_LLM_MODEL", "wrong-packaged-model")
    monkeypatch.setenv("NEWS_LLM_BASE_URL", "https://wrong.example.test/v1")
    monkeypatch.setenv("NEWS_LLM_API_KEY_ENV", "GLOBAL_FAKE_KEY")
    monkeypatch.setenv("GLOBAL_FAKE_KEY", "global-fake-key")
    return models


def test_enabled_intent_uses_real_owned_loader_and_fake_worker_without_network(tmp_path: Path, isolated_composition, monkeypatch):
    models = _enable_owned_model(tmp_path, monkeypatch)
    real_loader = composition.load_openai_compatible_deployment
    loads = []
    clients = []

    def load(path=None, **kwargs):
        loads.append((path, kwargs))
        return real_loader(path, **kwargs)

    class FakeWorker:
        def __init__(self, client, *, managed_output):
            clients.append(client)
            assert managed_output is False

        def generate_candidate(self, payload):
            return {"summary": "找 agent 论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None}

    monkeypatch.setattr(composition, "load_openai_compatible_deployment", load)
    monkeypatch.setattr(composition, "StructuredGuidedIntentCandidateWorker", FakeWorker)

    response = _client(isolated_composition).post("/api/v1/research/guided/intent", json={"question": "找 agent 论文"})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["query"] == "agent"
    assert loads == [(models, {"route_id": "app-guided-route", "apply_environment_overrides": False})]
    assert clients[0].config.model == "app-bound-model"
    assert clients[0].config.base_url == "https://app-bound.example.test/v1"
    assert clients[0].config.api_key_env == FIXED_CREDENTIAL_ENV
    assert clients[0].config.timeout_seconds <= 45


@pytest.mark.parametrize("field,value", [
    ("NEWS_RESEARCH_LLM_MODEL", "mismatched-model"),
    ("NEWS_RESEARCH_LLM_BASE_URL", "https://mismatched.example.test/v1"),
    ("NEWS_RESEARCH_LLM_PROVIDER", "mismatched-provider"),
    ("NEWS_RESEARCH_LLM_API_KEY_ENV", "GLOBAL_FAKE_KEY"),
    ("NEWS_RESEARCH_LLM_ROUTE_ID", "missing-route"),
])
def test_enabled_intent_rejects_mismatched_owner_bindings(tmp_path: Path, isolated_composition, monkeypatch, field, value):
    _enable_owned_model(tmp_path, monkeypatch)
    monkeypatch.setenv(field, value)

    def forbidden_worker(*_args, **_kwargs):
        raise AssertionError("mismatched owner binding must not build a worker")

    monkeypatch.setattr(composition, "StructuredGuidedIntentCandidateWorker", forbidden_worker)
    response = _client(isolated_composition).post("/api/v1/research/guided/intent", json={"question": "找论文"})

    assert response.status_code == 503
    assert response.json()["error"]["details"]["code"] == "research_configuration_invalid"


@pytest.mark.parametrize("marker", ["invalid", "1"])
def test_explicit_marker_never_falls_back_to_packaged_models_without_owned_path(monkeypatch, marker):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, marker)
    _forbid_model_resolution(monkeypatch)

    with pytest.raises(GuidedResearchError) as raised:
        composition.build_guided_research_service()._application.interpret(GuidedIntentCommand(question="找论文"))

    assert raised.value.status_code == 503
    assert raised.value.details["code"] == "research_configuration_invalid"


@pytest.mark.parametrize("owned_path", [None, "", " \t"])
@pytest.mark.parametrize("shared_config", ["absent", "packaged", "inherited"])
def test_enabled_intent_without_owned_path_rejects_before_shared_defaults_or_credentials(
    tmp_path: Path, isolated_composition, monkeypatch, owned_path, shared_config,
):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, "1")
    monkeypatch.chdir(tmp_path)
    if owned_path is not None:
        monkeypatch.setenv("NEWS_RESEARCH_MODELS_CONFIG", owned_path)
    if shared_config != "absent":
        if shared_config == "packaged":
            poison = tmp_path / "configs" / "models.yaml"
        else:
            poison = tmp_path / "shared-models.json"
            monkeypatch.setenv("NEWS_MODELS_CONFIG", str(poison))
        poison.parent.mkdir(parents=True, exist_ok=True)
        poison.write_text("{poison-shared-default", encoding="utf-8")
    # Run the real settings/composition path without credentials. The dedicated
    # owner path must be admitted before either shared config or key resolution.
    _forbid_model_resolution(monkeypatch)

    response = _client(isolated_composition).post(
        "/api/v1/research/guided/intent", json={"question": "Find agent papers"},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "guided_intent_unavailable"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["user_action_required"] is True
    assert payload["error"]["details"]["code"] == "research_configuration_invalid"
    assert payload["error"]["details"]["capabilities"] == ["research.llm.route"]
    assert payload["data"] is None
    assert "poison-shared-default" not in response.text


@pytest.mark.parametrize("marker", ["1", "invalid"])
def test_explicit_marker_admission_precedes_runtime_settings_resolution(monkeypatch, marker):
    monkeypatch.setenv(DESKTOP_RESEARCH_LLM_ENABLED_ENV, marker)

    def forbidden_settings(*_args, **_kwargs):
        raise AssertionError("unadmitted desktop config must not resolve runtime settings or credentials")

    monkeypatch.setattr(composition.ResearchRuntimeSettings, "from_env", forbidden_settings)
    _forbid_model_resolution(monkeypatch)

    with pytest.raises(GuidedResearchError) as raised:
        composition.build_guided_research_service()._application.interpret(
            GuidedIntentCommand(question="Find agent papers"),
        )

    assert raised.value.status_code == 503
    assert raised.value.details["code"] == "research_configuration_invalid"
    assert raised.value.details["capabilities"] == ["research.llm.route"]


def test_unmarked_profile_keeps_existing_default_loader_contract(monkeypatch):
    loads = []
    configs = []
    deployment = OpenAICompatibleDeploymentConfig(
        deployment_id="ordinary", route_id="ordinary-route",
        config=OpenAICompatibleConfig(
            provider="openai-compatible", model="ordinary-model", base_url="https://ordinary.example.test/v1",
            api_key_env="ORDINARY_KEY", timeout_seconds=80,
        ),
    )

    def load(*args, **kwargs):
        loads.append((args, kwargs))
        return deployment

    class FakeWorker:
        def __init__(self, client, *, managed_output):
            configs.append(client.config)

        def generate_candidate(self, payload):
            return {"summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None}

    monkeypatch.setattr(composition, "load_openai_compatible_deployment", load)
    monkeypatch.setattr(composition, "StructuredGuidedIntentCandidateWorker", FakeWorker)
    composition.build_guided_research_service()

    assert loads == [((), {})]
    assert configs == [replace(deployment.config, timeout_seconds=45)]


def test_enabled_real_worker_rejects_missing_fixed_key_without_inheriting_global_key(
    tmp_path: Path, isolated_composition, monkeypatch,
):
    _enable_owned_model(tmp_path, monkeypatch)
    monkeypatch.delenv(FIXED_CREDENTIAL_ENV)
    monkeypatch.setenv("NEWS_LLM_API_KEY", "inherited-global-fake-key")

    response = _client(isolated_composition).post("/api/v1/research/guided/intent", json={"question": "Find agent papers"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "guided_intent_unavailable"
    assert response.json()["data"] is None
    assert "inherited-global-fake-key" not in response.text


@pytest.mark.parametrize("failure", ["missing", "malformed", "disabled-deployment"])
def test_enabled_intent_fails_closed_for_unusable_owned_models(
    tmp_path: Path, isolated_composition, monkeypatch, failure,
):
    models = _enable_owned_model(tmp_path, monkeypatch)
    if failure == "missing":
        models.unlink()
    elif failure == "malformed":
        models.write_text("{invalid", encoding="utf-8")
    else:
        payload = json.loads(models.read_text(encoding="utf-8"))
        payload["deployments"][0]["enabled"] = False
        models.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden_worker(*_args, **_kwargs):
        raise AssertionError("unusable owned models must not build a worker")

    monkeypatch.setattr(composition, "StructuredGuidedIntentCandidateWorker", forbidden_worker)
    response = _client(isolated_composition).post("/api/v1/research/guided/intent", json={"question": "Find agent papers"})

    assert response.status_code == 503
    assert response.json()["error"]["details"]["code"] == "research_configuration_invalid"
    assert response.json()["data"] is None
