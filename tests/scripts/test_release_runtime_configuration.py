from __future__ import annotations

from pathlib import Path

import pytest

from scripts.local_desktop.common import DesktopOperationError, atomic_write_json
from scripts.local_desktop.runtime_config import candidate_document, runtime_config_paths
from tests.scripts.test_local_desktop_release import _manager


def _candidate_document() -> dict[str, object]:
    return candidate_document(
        {
            "llm": {
                "provider": "openai-compatible",
                "route_id": "writer-primary",
                "model": "research-model-1",
                "base_url": "https://api.example.test/v1",
            },
            "source": {
                "preset": "arxiv-default",
                "api_url": "https://export.arxiv.org/api/query",
                "timeout_seconds": 30,
            },
        },
        revision=1,
    )


def test_release_rebind_preserves_source_and_dependency_identity_and_verifies_bound_config(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    original = manager.verify(release)
    document = _candidate_document()

    rebound = manager.rebind_runtime_configuration(release, runtime_config_document=document)
    rebound_path = Path(rebound["release_path"])
    manifest = rebound["manifest"]

    assert rebound_path != release
    assert manifest["identity"]["source_revision"] == original["identity"]["source_revision"]
    assert manifest["identity"]["source_digest"] == original["identity"]["source_digest"]
    assert manifest["identity"]["dependency_digest"] == original["identity"]["dependency_digest"]
    assert manifest["identity"]["config_digest"] != original["identity"]["config_digest"]
    assert (rebound_path / ".desktop-app/config/models.json").is_file()
    assert manifest["config"]["external_config_dependencies"][0]["mode"] == "bound"

    with pytest.raises(DesktopOperationError) as before_commit:
        manager.verify(rebound_path)
    assert before_commit.value.reason_code == "desktop_release_config_incompatible"

    atomic_write_json(runtime_config_paths(manager.config).document, document)
    verified = manager.verify(rebound_path)
    environment = manager.bind_runtime_environment(rebound_path, verified, {})

    assert verified["identity"] == manifest["identity"]
    assert environment["NEWS_RESEARCH_LLM_MODEL"] == "research-model-1"
    assert environment["NEWS_RESEARCH_LLM_BASE_URL"] == "https://api.example.test/v1"
    assert environment["NEWS_RESEARCH_ARXIV_API_URL"] == "https://export.arxiv.org/api/query"
    assert Path(environment["NEWS_RESEARCH_MODELS_CONFIG"]).is_relative_to(rebound_path)


def test_release_rebind_rejects_bound_models_drift(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    document = _candidate_document()
    rebound = manager.rebind_runtime_configuration(release, runtime_config_document=document)
    rebound_path = Path(rebound["release_path"])
    atomic_write_json(runtime_config_paths(manager.config).document, document)
    (rebound_path / ".desktop-app/config/models.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(rebound_path)

    assert raised.value.reason_code in {"desktop_release_checksum_mismatch", "desktop_release_config_drift"}


def test_release_rebind_can_disable_llm_without_inheriting_packaged_models(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    enabled = _candidate_document()
    enabled_rebound = manager.rebind_runtime_configuration(release, runtime_config_document=enabled)
    enabled_path = Path(enabled_rebound["release_path"])
    atomic_write_json(runtime_config_paths(manager.config).document, enabled)
    manager.activate(enabled_path)

    disabled = candidate_document(
        {
            "llm": None,
            "source": {
                "preset": "arxiv-default",
                "api_url": "https://export.arxiv.org/api/query?search_query=all",
                "timeout_seconds": 30,
            },
        },
        revision=2,
    )
    disabled_rebound = manager.rebind_runtime_configuration(enabled_path, runtime_config_document=disabled)
    disabled_path = Path(disabled_rebound["release_path"])
    manifest = disabled_rebound["manifest"]

    assert manifest["identity"]["source_digest"] == enabled_rebound["manifest"]["identity"]["source_digest"]
    assert manifest["identity"]["dependency_digest"] == enabled_rebound["manifest"]["identity"]["dependency_digest"]
    assert manifest["desktop_runtime_configuration"]["source_release_id"] == "release-one"
    assert not (disabled_path / ".desktop-app/config/models.json").exists()
    assert all(item["name"] not in {"NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG"} for item in manifest["config"]["external_config_dependencies"])

    atomic_write_json(runtime_config_paths(manager.config).document, disabled)
    verified = manager.verify(disabled_path)
    environment = manager.bind_runtime_environment(
        disabled_path,
        verified,
        {
            "NEWS_MODELS_CONFIG": "C:/stale/models.json",
            "NEWS_RESEARCH_MODELS_CONFIG": "C:/stale/models.json",
            "NEWS_RESEARCH_LLM_MODEL": "stale-model",
            "NEWSROOM_DESKTOP_RESEARCH_API_KEY": "stale-secret",
        },
    )
    assert "NEWS_MODELS_CONFIG" not in environment
    assert "NEWS_RESEARCH_MODELS_CONFIG" not in environment
    assert "NEWS_RESEARCH_LLM_MODEL" not in environment
    assert "NEWSROOM_DESKTOP_RESEARCH_API_KEY" not in environment
    assert environment["NEWS_RESEARCH_ARXIV_API_URL"].endswith("search_query=all")
