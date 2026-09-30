"""Research process execution composition.

The process owns one registry and one manifest.  Individual parser adapters
receive only the profile they are allowed to use; they cannot create a local
provider or downgrade to host execution.
"""

from __future__ import annotations

from functools import lru_cache
import os

from framework.execution_environment import (
    ExecutionEnvironmentRegistry,
    ExecutionProfile,
    ExecutionProfileRegistry,
    RuntimeCompositionManifest,
    RuntimeExecutionComposition,
    DeploymentCapabilityEvidence,
    DeploymentRollbackEvidence,
    build_runtime_execution_composition,
)
from infrastructure.execution_environment.docker import DockerExecutionEnvironment


PRODUCTION_RUNTIME_COMPOSITION_ID = "newsroom-runtime"
PRODUCTION_RUNTIME_COMPOSITION_VERSION = "1"
RUNTIME_TRUSTED_PROFILE_ID = "runtime-trusted-in-process"
RESEARCH_MARKER_PROFILE_ID = "research-parser-marker"
RESEARCH_MINERU_PROFILE_ID = "research-parser-mineru"

# Keep the Research names as aliases while all process roots resolve the same
# manifest.  The parser profiles are part of the shared policy catalog even
# when a particular process does not invoke a parser.
RESEARCH_RUNTIME_COMPOSITION_ID = PRODUCTION_RUNTIME_COMPOSITION_ID
RESEARCH_RUNTIME_COMPOSITION_VERSION = PRODUCTION_RUNTIME_COMPOSITION_VERSION
@lru_cache(maxsize=1)
def _shared_docker_provider() -> DockerExecutionEnvironment:
    """Probe Docker once per interpreter and reuse its immutable provider."""

    return DockerExecutionEnvironment()


def build_process_execution_composition(
    *,
    required_provider_ids: tuple[str, ...] = (),
    expected_manifest_fingerprint: str | None = None,
    deployment_capability_evidence: tuple[DeploymentCapabilityEvidence, ...] = (),
    deployment_rollback_evidence: tuple[DeploymentRollbackEvidence, ...] = (),
) -> RuntimeExecutionComposition:
    """Build the shared execution composition used by every process root.

    Docker availability is intentionally represented in the provider
    capability fingerprint.  An unavailable daemon remains a typed admission
    block; this factory never substitutes an in-process or host subprocess
    provider.
    """

    if expected_manifest_fingerprint is None:
        expected_manifest_fingerprint = os.environ.get(
            "NEWSROOM_RUNTIME_COMPOSITION_FINGERPRINT"
        )
    execution_registry = ExecutionEnvironmentRegistry()
    execution_registry.register(_shared_docker_provider())
    profile_registry = ExecutionProfileRegistry()
    profile_registry.register(
        RUNTIME_TRUSTED_PROFILE_ID,
        ExecutionProfile.trusted_in_process(),
    )
    profile_registry.register(
        RESEARCH_MARKER_PROFILE_ID,
        ExecutionProfile.external_process(
            provider_id="docker",
            allowed_argv_prefixes=(("marker_single",),),
        ),
    )
    profile_registry.register(
        RESEARCH_MINERU_PROFILE_ID,
        ExecutionProfile.external_process(
            provider_id="docker",
            allowed_argv_prefixes=(("mineru",),),
        ),
    )
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id=RESEARCH_RUNTIME_COMPOSITION_ID,
        version=RESEARCH_RUNTIME_COMPOSITION_VERSION,
        profile_registry=profile_registry,
        execution_registry=execution_registry,
        metadata={
            "profile_catalog": {
                "trusted_in_process": [RUNTIME_TRUSTED_PROFILE_ID],
                "sandboxed": [],
                "external_process": [
                    RESEARCH_MARKER_PROFILE_ID,
                    RESEARCH_MINERU_PROFILE_ID,
                ],
            },
            "parser_profiles": {
                "marker": RESEARCH_MARKER_PROFILE_ID,
                "mineru": RESEARCH_MINERU_PROFILE_ID,
            },
        },
        deployment_capability_evidence=deployment_capability_evidence,
        deployment_rollback_evidence=deployment_rollback_evidence,
    )
    return build_runtime_execution_composition(
        manifest=manifest,
        profile_registry=profile_registry,
        execution_registry=execution_registry,
        required_provider_ids=required_provider_ids,
        expected_manifest_fingerprint=expected_manifest_fingerprint,
    )


def build_research_execution_composition() -> RuntimeExecutionComposition:
    """Build the Research view of the shared process composition."""

    return build_process_execution_composition(
        required_provider_ids=("docker",),
    )


__all__ = [
    "PRODUCTION_RUNTIME_COMPOSITION_ID",
    "PRODUCTION_RUNTIME_COMPOSITION_VERSION",
    "RESEARCH_MARKER_PROFILE_ID",
    "RESEARCH_MINERU_PROFILE_ID",
    "RESEARCH_RUNTIME_COMPOSITION_ID",
    "RESEARCH_RUNTIME_COMPOSITION_VERSION",
    "RUNTIME_TRUSTED_PROFILE_ID",
    "build_process_execution_composition",
    "build_research_execution_composition",
]
