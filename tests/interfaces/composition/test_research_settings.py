from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from framework.harness.runtime import GraphArtifactRolloutMode
from interfaces.composition.research_errors import (
    ResearchConfigurationError,
    ResearchRuntimeUnavailableError,
)
from interfaces.composition.research_settings import (
    ResearchArtifactSettings,
    ResearchRuntimeSettings,
)


def _minimum_env(secret: str = "sk-test-research-secret") -> dict[str, str]:
    return {"DASHSCOPE_API_KEY": secret}


def test_from_env_builds_immutable_defaults_without_creating_storage(
    tmp_path: Path,
) -> None:
    secret = "sk-test-research-secret"
    settings = ResearchRuntimeSettings.from_env(
        _minimum_env(secret),
        cwd=tmp_path,
    )

    assert settings.research_root == (tmp_path / ".newsroom" / "research").resolve()
    assert settings.artifact.root == (tmp_path / ".newsroom" / "runs").resolve()
    assert settings.run_store.root == (
        tmp_path / ".newsroom" / "research" / "runs"
    ).resolve()
    assert settings.rag.local_root == (
        tmp_path / ".newsroom" / "research" / "chunks"
    ).resolve()
    assert settings.source.provider == "arxiv"
    assert settings.source.api_url == "https://export.arxiv.org/api/query"
    assert settings.source.cache_size == 128
    assert settings.source.package_max_bytes == 120_000_000
    assert settings.llm.route_id == "writer-primary"
    assert settings.llm.models_config_path is None
    assert settings.llm.provider == "openai-compatible"
    assert settings.llm.api_key_env == "DASHSCOPE_API_KEY"
    assert settings.llm.max_attempts == 2
    assert settings.parser.backends == ("mineru", "marker")
    assert settings.parser.allow_abstract_fallback is True
    assert settings.rag.backend == "local"
    assert settings.rag.qdrant_url is None
    assert settings.rag.max_rounds == 6
    assert settings.rag.max_replans == 1
    assert settings.rag.max_queries == 12
    assert settings.rag.max_source_reads == 24
    assert settings.rag.max_context_items == 8
    assert settings.rag.max_context_tokens == 4_096
    assert settings.rag.max_worker_calls == 16
    assert settings.rag.memory_enabled is False
    assert settings.rag.memory_namespace_refs == ()
    assert settings.run_store.write_schema_version == "v2"
    assert settings.run_store.supported_schema_versions == ("v1", "v2")
    assert settings.run_store.rollback_schema_versions == ("v1", "v2")
    assert settings.run_store.reconciliation_max_runs == 100
    assert settings.graph_artifact_persistence.mode is GraphArtifactRolloutMode.ENFORCE
    assert (
        settings.graph_artifact_persistence.policy_version
        == "graph-artifact-policy@1"
    )
    assert settings.graph_artifact_persistence.inline_max_bytes == 32 * 1024
    assert settings.graph_artifact_persistence.max_artifacts_per_run == 200
    assert (
        settings.graph_artifact_persistence.max_materialized_bytes_per_run
        == 500 * 1024 * 1024
    )
    assert not settings.artifact.root.exists()
    assert not settings.research_root.exists()

    with pytest.raises(FrozenInstanceError):
        settings.research_root = tmp_path  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        settings.llm.model = "other"  # type: ignore[misc]

    projection = json.dumps(asdict(settings), default=str, sort_keys=True)
    assert secret not in projection
    assert secret not in repr(settings)


@pytest.mark.parametrize(
    "extra_env",
    (
        {"NEWS_RAG_MEMORY": "true"},
        {
            "NEWS_RAG_MEMORY": "true",
            "NEWS_RESEARCH_RAG_MEMORY_NAMESPACE_REFS": "research:latest",
        },
        {
            "NEWS_RAG_MEMORY": "true",
            "NEWS_RESEARCH_RAG_MEMORY_NAMESPACE_REFS": (
                "memory-namespace://" + "a" * 64 + ","
                "memory-namespace://" + "a" * 64
            ),
        },
    ),
)
def test_enabled_rag_memory_requires_unique_exact_namespace_revisions(
    tmp_path: Path,
    extra_env: dict[str, str],
) -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(
            {**_minimum_env(), **extra_env},
            cwd=tmp_path,
        )
    assert "research.rag.memory_namespace_refs" in exc_info.value.capabilities


def test_from_env_normalizes_all_research_configuration_groups(tmp_path: Path) -> None:
    env = {
        "NEWS_RESEARCH_ROOT": "state/../research-state",
        "NEWS_RESEARCH_ARTIFACT_ROOT": "artifacts",
        "NEWS_RESEARCH_RUN_STORE_ROOT": "stores/runs",
        "NEWS_RESEARCH_SOURCE_PROVIDER": "ARXIV",
        "NEWS_RESEARCH_ARXIV_API_URL": "https://arxiv.example/api/query/",
        "NEWS_RESEARCH_SOURCE_CACHE_SIZE": "256",
        "NEWS_RESEARCH_SOURCE_TIMEOUT_SECONDS": "45.5",
        "NEWS_RESEARCH_SOURCE_METADATA_MAX_BYTES": "8000000",
        "NEWS_RESEARCH_SOURCE_PACKAGE_MAX_BYTES": "200000000",
        "NEWS_RESEARCH_LLM_PROVIDER": "OpenAI-Compatible",
        "NEWS_RESEARCH_LLM_ROUTE_ID": "research-analysis-v2",
        "NEWS_RESEARCH_MODELS_CONFIG": "config/research-models.json",
        "NEWS_RESEARCH_LLM_BASE_URL": "https://llm.example/v1/",
        "NEWS_RESEARCH_LLM_MODEL": "vendor/model-v2",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "RESEARCH_LLM_SECRET",
        "RESEARCH_LLM_SECRET": "sk-never-retained",
        "NEWS_RESEARCH_LLM_TIMEOUT_SECONDS": "33.25",
        "NEWS_RESEARCH_LLM_MAX_ATTEMPTS": "3",
        "NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS": "16000",
        "NEWS_RESEARCH_LLM_MAX_OUTPUT_TOKENS": "3000",
        "NEWS_RESEARCH_PARSER_BACKENDS": "marker,pymupdf",
        "NEWS_RESEARCH_ALLOW_ABSTRACT_FALLBACK": "false",
        "NEWS_RESEARCH_PARSER_TIMEOUT_SECONDS": "450",
        "NEWS_RESEARCH_PARSER_MAX_DOCUMENT_BYTES": "210000000",
        "NEWS_RESEARCH_RAG_BACKEND": "QDRANT",
        "NEWS_RESEARCH_RAG_LOCAL_ROOT": "rag/chunks",
        "NEWS_RESEARCH_RAG_QDRANT_URL": "https://qdrant.example:6333/",
        "NEWS_RESEARCH_RAG_COLLECTION": "research_chunks_v2",
        "NEWS_RESEARCH_RAG_VECTOR_SIZE": "1536",
        "NEWS_RESEARCH_RAG_MAX_ROUNDS": "4",
        "NEWS_RESEARCH_RAG_MAX_REPLANS": "2",
        "NEWS_RESEARCH_RAG_MAX_QUERIES": "12",
        "NEWS_RESEARCH_RAG_MAX_SOURCE_READS": "24",
        "NEWS_RESEARCH_RAG_MAX_MEMORY_HITS": "10",
        "NEWS_RESEARCH_RAG_MAX_CONTEXT_ITEMS": "20",
        "NEWS_RESEARCH_RAG_MAX_CONTEXT_TOKENS": "12000",
        "NEWS_RESEARCH_RAG_MAX_WORKER_CALLS": "6",
        "NEWS_RAG_MEMORY": "true",
        "NEWS_RESEARCH_RAG_MEMORY_NAMESPACE_REFS": (
            "memory-namespace://" + "b" * 64 + ","
            "memory-namespace://" + "a" * 64
        ),
        "NEWS_RESEARCH_ARTIFACT_MAX_BYTES": "24000000",
        "NEWS_RESEARCH_RUN_RECORD_MAX_BYTES": "25000000",
        "NEWS_RESEARCH_RUN_WRITE_SCHEMA_VERSION": "v2",
        "NEWS_RESEARCH_RUN_SUPPORTED_SCHEMA_VERSIONS": "v1,v2",
        "NEWS_RESEARCH_RUN_ROLLBACK_SCHEMA_VERSIONS": "v1,v2",
        "NEWS_RESEARCH_RUN_RECONCILIATION_MAX_RUNS": "17",
    }

    settings = ResearchRuntimeSettings.from_env(env, cwd=tmp_path)

    assert settings.research_root == (tmp_path / "research-state").resolve()
    assert settings.artifact.root == (tmp_path / "artifacts").resolve()
    assert settings.run_store.root == (tmp_path / "stores" / "runs").resolve()
    assert settings.source.provider == "arxiv"
    assert settings.source.api_url == "https://arxiv.example/api/query"
    assert settings.source.cache_size == 256
    assert settings.source.timeout_seconds == 45.5
    assert settings.llm.provider == "openai-compatible"
    assert settings.llm.route_id == "research-analysis-v2"
    assert settings.llm.models_config_path == (
        tmp_path / "config" / "research-models.json"
    ).resolve()
    assert settings.llm.base_url == "https://llm.example/v1"
    assert settings.llm.model == "vendor/model-v2"
    assert settings.llm.api_key_env == "RESEARCH_LLM_SECRET"
    assert settings.llm.timeout_seconds == 33.25
    assert settings.llm.max_attempts == 3
    assert settings.parser.backends == ("marker", "pymupdf")
    assert settings.parser.allow_abstract_fallback is False
    assert settings.rag.backend == "qdrant"
    assert settings.rag.qdrant_url == "https://qdrant.example:6333"
    assert settings.rag.local_root == (tmp_path / "rag" / "chunks").resolve()
    assert settings.rag.vector_size == 1536
    assert settings.rag.max_replans == 2
    assert settings.rag.memory_enabled is True
    assert settings.rag.memory_namespace_refs == (
        "memory-namespace://" + "a" * 64,
        "memory-namespace://" + "b" * 64,
    )
    assert settings.artifact.max_bytes == 24_000_000
    assert settings.run_store.max_record_bytes == 25_000_000
    assert settings.run_store.write_schema_version == "v2"
    assert settings.run_store.supported_schema_versions == ("v1", "v2")
    assert settings.run_store.rollback_schema_versions == ("v1", "v2")
    assert settings.run_store.reconciliation_max_runs == 17


def test_from_env_normalizes_explicit_graph_artifact_persistence_snapshot(
    tmp_path: Path,
) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            **_minimum_env(),
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MODE": "ENFORCE",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_POLICY_VERSION": "graph-artifact-policy@2",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_READABLE_POLICY_VERSIONS": (
                "graph-artifact-policy@1,graph-artifact-policy@2"
            ),
            "NEWS_RESEARCH_GRAPH_ARTIFACT_INLINE_MAX_BYTES": "4096",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_INLINE_MAX_DEPTH": "6",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_INLINE_MAX_KEYS": "64",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_SUMMARY_MAX_BYTES": "2048",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_SUMMARY_MAX_TOKENS": "512",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_SAMPLE_MAX_BYTES": "8192",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_BYTES": "10485760",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_PER_RUN": "25",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_MATERIALIZED_BYTES_PER_RUN": "20971520",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_PER_TENANT": "2500",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_MATERIALIZED_BYTES_PER_TENANT": "209715200",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_PER_CLASS": "1000",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_MATERIALIZED_BYTES_PER_CLASS": "104857600",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_CONTEXT_REFS": "4",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_CONTEXT_LOADED_BYTES": "1048576",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_CONTEXT_LOADED_TOKENS": "262144",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_DEDUP_SCOPE": "TENANT_CHECKSUM_MEDIA_TYPE",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_CACHE_TTL_SECONDS": "3600",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_QUOTA_ALERT_BASIS_POINTS": "7500",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_GC_BACKLOG_ALERT_BYTES": "104857600",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_CACHE_STAMPEDE_MISS_THRESHOLD": "12",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_EPHEMERAL_DAYS": "2",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_RUN_DAYS": "45",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_EVIDENCE_DAYS": "365",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_REPORT_DAYS": "3650",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_CACHE_DAYS": "2",
        },
        cwd=tmp_path,
    )

    config = settings.graph_artifact_persistence
    assert config.mode is GraphArtifactRolloutMode.ENFORCE
    assert config.policy_version == "graph-artifact-policy@2"
    assert config.readable_policy_versions == (
        "graph-artifact-policy@1",
        "graph-artifact-policy@2",
    )
    assert config.inline_max_bytes == 4096
    assert config.max_artifact_bytes == 10 * 1024 * 1024
    assert config.max_materialized_bytes_per_run == 20 * 1024 * 1024
    assert config.max_materialized_bytes_per_tenant == 200 * 1024 * 1024
    assert config.max_materialized_bytes_per_class == 100 * 1024 * 1024
    assert config.quota_alert_threshold_basis_points == 7500
    assert config.gc_backlog_alert_bytes == 100 * 1024 * 1024
    assert config.cache_stampede_miss_threshold == 12
    assert config.retention.report_days == 3650


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MODE", "worker_selected"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MODE", "legacy"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MODE", "shadow"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MODE", "read_only"),
        (
            "NEWS_RESEARCH_GRAPH_ARTIFACT_POLICY_VERSION",
            "graph-artifact-policy@2",
        ),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_INLINE_MAX_BYTES", "0"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_BYTES", "999999999"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_CACHE_TTL_SECONDS", "30"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_PER_TENANT", "1"),
        (
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MAX_MATERIALIZED_BYTES_PER_TENANT",
            "1024",
        ),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_QUOTA_ALERT_BASIS_POINTS", "0"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_CACHE_STAMPEDE_MISS_THRESHOLD", "1"),
        ("NEWS_RESEARCH_GRAPH_ARTIFACT_RETENTION_REPORT_DAYS", "0"),
    ],
)
def test_graph_artifact_configuration_fails_closed_without_echoing_value(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(
            {**_minimum_env(), name: value},
            cwd=tmp_path,
        )

    error = exc_info.value
    assert error.capabilities == ("research.graph_artifact_persistence",)
    assert value not in str(error)
    assert value not in json.dumps(error.to_public_dict(), sort_keys=True)


def test_from_env_uses_existing_shared_llm_artifact_and_parser_names(
    tmp_path: Path,
) -> None:
    env = {
        "NEWS_LLM_PROVIDER": "openai-compatible",
        "NEWS_LLM_BASE_URL": "https://shared-llm.example/v1",
        "NEWS_LLM_MODEL": "shared-model",
        "NEWS_LLM_API_KEY_ENV": "SHARED_LLM_KEY",
        "SHARED_LLM_KEY": "shared-secret",
        "NEWS_ARTIFACT_ROOT": "shared-runs",
        "NEWSROOM_PDF_PARSER_CASCADE": "mineru,pymupdf",
    }

    settings = ResearchRuntimeSettings.from_env(env, cwd=tmp_path)

    assert settings.llm.base_url == "https://shared-llm.example/v1"
    assert settings.llm.model == "shared-model"
    assert settings.llm.api_key_env == "SHARED_LLM_KEY"
    assert settings.artifact.root == (tmp_path / "shared-runs").resolve()
    assert settings.parser.backends == ("mineru", "pymupdf")


def test_from_env_uses_standard_openai_credential_when_no_key_indirection_is_set(
    tmp_path: Path,
) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            "OPENAI_API_KEY": "sk-standard-openai-secret",
            "OPENAI_BASE_URL": "https://openai-compatible.example/v1",
            "OPENAI_MODEL": "standard-model",
        },
        cwd=tmp_path,
    )

    assert settings.llm.api_key_env == "OPENAI_API_KEY"
    assert settings.llm.base_url == "https://openai-compatible.example/v1"
    assert settings.llm.model == "standard-model"


def test_missing_llm_credential_is_typed_and_never_exposes_environment_values(
    tmp_path: Path,
) -> None:
    secret_like_url = "https://secret-host.example/v1"
    env = {
        "NEWS_RESEARCH_LLM_BASE_URL": secret_like_url,
        "NEWS_RESEARCH_LLM_MODEL": "secret-model-name",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "MISSING_RESEARCH_SECRET",
    }

    with pytest.raises(ResearchRuntimeUnavailableError) as exc_info:
        ResearchRuntimeSettings.from_env(env, cwd=tmp_path)

    error = exc_info.value
    assert error.capabilities == ("research.llm.credential",)
    assert error.error_code == "research_runtime_unavailable"
    assert error.retryable is False
    public = json.dumps(error.to_public_dict(), sort_keys=True)
    assert secret_like_url not in public
    assert "secret-model-name" not in public
    assert "MISSING_RESEARCH_SECRET" not in public


@pytest.mark.parametrize(
    ("name", "value", "capability"),
    [
        ("NEWS_RESEARCH_SOURCE_CACHE_SIZE", "0", "research.source.cache_size"),
        ("NEWS_RESEARCH_SOURCE_PACKAGE_MAX_BYTES", "1024", "research.source.package_max_bytes"),
        ("NEWS_RESEARCH_LLM_MAX_ATTEMPTS", "6", "research.llm.max_attempts"),
        ("NEWS_RESEARCH_LLM_MAX_ATTEMPTS", "not-a-number", "research.llm.max_attempts"),
        ("NEWS_RESEARCH_PARSER_TIMEOUT_SECONDS", "inf", "research.parser.timeout"),
        ("NEWS_RESEARCH_RAG_MAX_ROUNDS", "9", "research.rag.max_rounds"),
        ("NEWS_RESEARCH_RAG_MAX_REPLANS", "-1", "research.rag.max_replans"),
        ("NEWS_RESEARCH_RAG_MAX_CONTEXT_TOKENS", "128", "research.rag.max_context_tokens"),
        ("NEWS_RESEARCH_ARTIFACT_MAX_BYTES", "0", "research.storage.artifact_max_bytes"),
        (
            "NEWS_RESEARCH_RUN_RECORD_MAX_BYTES",
            "999999999",
            "research.storage.run_record_max_bytes",
        ),
    ],
)
def test_from_env_rejects_unbounded_or_malformed_limits_without_echoing_values(
    tmp_path: Path,
    name: str,
    value: str,
    capability: str,
) -> None:
    env = {**_minimum_env(), name: value}

    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(env, cwd=tmp_path)

    assert exc_info.value.capabilities == (capability,)
    assert value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("name", "value", "capability"),
    [
        (
            "NEWS_RESEARCH_LLM_BASE_URL",
            "https://user:sk-private-value@llm.example/v1",
            "research.llm.base_url",
        ),
        (
            "NEWS_RESEARCH_ARXIV_API_URL",
            "file:///private/source",
            "research.source.api_url",
        ),
        (
            "NEWS_RESEARCH_LLM_API_KEY_ENV",
            "sk-private-value",
            "research.llm.credential",
        ),
    ],
)
def test_from_env_rejects_unsafe_endpoint_or_secret_indirection_without_echo(
    tmp_path: Path,
    name: str,
    value: str,
    capability: str,
) -> None:
    env = {**_minimum_env(), name: value}

    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(env, cwd=tmp_path)

    assert exc_info.value.capabilities == (capability,)
    assert value not in str(exc_info.value)
    assert "sk-private-value" not in json.dumps(exc_info.value.to_public_dict())


@pytest.mark.parametrize(
    ("name", "value", "capability"),
    [
        ("NEWS_RESEARCH_SOURCE_PROVIDER", "rss", "research.source.provider"),
        ("NEWS_RESEARCH_PARSER_BACKENDS", "pymupdf", "research.parser.backends"),
        ("NEWS_RESEARCH_PARSER_BACKENDS", "marker,marker", "research.parser.backends"),
        ("NEWS_RESEARCH_ALLOW_ABSTRACT_FALLBACK", "sometimes", "research.parser.abstract_fallback"),
        ("NEWS_RESEARCH_RAG_BACKEND", "memory", "research.rag.backend"),
        ("NEWS_RESEARCH_RAG_COLLECTION", "bad collection", "research.rag.collection"),
    ],
)
def test_from_env_rejects_unsupported_option_sets(
    tmp_path: Path,
    name: str,
    value: str,
    capability: str,
) -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(
            {**_minimum_env(), name: value},
            cwd=tmp_path,
        )

    assert exc_info.value.capabilities == (capability,)
    assert value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("overrides", "capability"),
    [
        (
            {"NEWS_RESEARCH_RUN_WRITE_SCHEMA_VERSION": "v2", "NEWS_RESEARCH_RUN_SUPPORTED_SCHEMA_VERSIONS": "v2"},
            "research.storage.run_store",
        ),
        (
            {"NEWS_RESEARCH_RUN_WRITE_SCHEMA_VERSION": "v2", "NEWS_RESEARCH_RUN_ROLLBACK_SCHEMA_VERSIONS": "v1"},
            "research.storage.run_store",
        ),
        (
            {"NEWS_RESEARCH_RUN_WRITE_SCHEMA_VERSION": "v3"},
            "research.storage.run_store",
        ),
        (
            {"NEWS_RESEARCH_RUN_SUPPORTED_SCHEMA_VERSIONS": "v1,v1"},
            "research.storage.run_store",
        ),
    ],
)
def test_v2_writer_rejects_non_dual_reader_or_rollback_target(
    tmp_path: Path,
    overrides: dict[str, str],
    capability: str,
) -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(
            {**_minimum_env(), **overrides},
            cwd=tmp_path,
        )

    assert exc_info.value.capabilities == (capability,)


def test_from_env_rejects_existing_non_directory_storage_root(tmp_path: Path) -> None:
    file_root = tmp_path / "private-secret-root"
    file_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(
            {
                **_minimum_env(),
                "NEWS_RESEARCH_ARTIFACT_ROOT": str(file_root),
            },
            cwd=tmp_path,
        )

    assert exc_info.value.capabilities == ("research.storage.artifact_root",)
    assert str(file_root) not in str(exc_info.value)


def test_direct_settings_reject_relative_storage_root() -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchArtifactSettings(root=Path("relative/runs"), max_bytes=1024)

    assert exc_info.value.capabilities == ("research.storage.artifact_root",)


def test_from_env_maps_invalid_base_path_to_typed_error() -> None:
    with pytest.raises(ResearchConfigurationError) as exc_info:
        ResearchRuntimeSettings.from_env(_minimum_env(), cwd="invalid\x00root")

    assert exc_info.value.capabilities == ("research.storage.root",)


def test_local_rag_does_not_validate_unused_qdrant_endpoint(tmp_path: Path) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            **_minimum_env(),
            "NEWS_RESEARCH_RAG_BACKEND": "local",
            "NEWS_QDRANT_URL": "not-a-url-with-sk-private-value",
            "NEWS_VECTOR_SIZE": "not-an-integer",
            "NEWS_EMBEDDING_DIMENSIONS": "also-not-an-integer",
        },
        cwd=tmp_path,
    )

    assert settings.rag.backend == "local"
    assert settings.rag.qdrant_url is None
