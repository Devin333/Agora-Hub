from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from interfaces.composition.research_settings import research_storage_paths_from_env
from interfaces.services.paper_rag_transcript_store import SCHEMA_VERSION as PAPER_RAG_TRANSCRIPT_SCHEMA

STORAGE_SCHEMAS = {
    "auth_users": "newsroom_auth_users.v2",
    "auth_sessions": "newsroom_auth_sessions.v1",
    "auth_otp": "newsroom_auth_otp_challenges.v1",
    "auth_oauth": "newsroom_auth_oauth_states.v1",
    "research_history": "newsroom_research_history.v1",
    "research_imports": "newsroom.research-import/v1",
    "runtime_artifacts": "newsroom.runtime-artifacts/v1",
    "research_run_store": "newsroom.research-run-store/v2",
    "research_rag_local": "newsroom.local-chunk-store/v1",
    "research_rag_transcripts": PAPER_RAG_TRANSCRIPT_SCHEMA,
    "research_paper_catalog": "newsroom.research-paper-catalog/v1",
    "research_graph_results": "newsroom.graph-result-store/v1",
    "research_harness_side_effects": "newsroom.harness-side-effect-store/v1",
    "research_harness_node_output": "newsroom.harness-node-output/v1",
    "desktop_control": "newsroom.research-desktop-control/v1",
}


@dataclass(frozen=True)
class ResearchStoragePaths:
    research_root: Path
    artifact_root: Path
    run_store_root: Path
    rag_local_root: Path
    rag_transcript_root: Path
    rag_backend: str
    rag_collection: str


def resolve_research_storage_paths(project_root: Path, environ: Mapping[str, str]) -> ResearchStoragePaths:
    """Resolve Research-owned paths through the application's credential-free storage contract."""
    root = project_root.resolve(strict=False)
    application_paths = research_storage_paths_from_env(environ, cwd=root)
    rag_backend = str(environ.get("NEWS_RESEARCH_RAG_BACKEND") or "local").strip().lower()
    if rag_backend not in {"local", "qdrant"}:
        raise ValueError("NEWS_RESEARCH_RAG_BACKEND must be local or qdrant")
    collection = str(environ.get("NEWS_RESEARCH_RAG_COLLECTION") or "research_paper_chunks").strip()
    if not collection:
        raise ValueError("NEWS_RESEARCH_RAG_COLLECTION cannot be empty")
    return ResearchStoragePaths(
        application_paths.research_root,
        application_paths.artifact_root,
        application_paths.run_store_root,
        application_paths.rag_local_root,
        application_paths.rag_transcript_root,
        rag_backend,
        collection,
    )


def storage_schema(owner_id: str) -> str:
    try:
        return STORAGE_SCHEMAS[owner_id]
    except KeyError as exc:
        raise ValueError(f"unknown local desktop storage owner: {owner_id}") from exc


__all__ = ["ResearchStoragePaths", "STORAGE_SCHEMAS", "resolve_research_storage_paths", "storage_schema"]
