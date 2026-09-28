from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionOutcome,
    ExecutionPolicyViolationError,
    ExecutionProfile,
    ExecutionReceipt,
    ExecutionStatus,
    FakeExecutionEnvironment,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.research.document_execution_adapter import (
    ResearchParserExecutionAdapter,
)


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-parser",
        graph_id="research-graph",
        graph_version="1.0.0",
        graph_ref="research-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="compile_document",
        node_instance_id="compile-document-1",
        activity_id="compile-document-activity",
        attempt=1,
    )


def _adapter(tmp_path: Path, captured: list[object]) -> ResearchParserExecutionAdapter:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_filesystem_roots=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_process_limits=True,
        enforces_timeout=True,
        supports_cancellation=True,
        confirms_termination=True,
    )

    def outcome_factory(request):
        captured.append(request)
        now = datetime.now(UTC)
        return ExecutionOutcome(
            receipt=ExecutionReceipt(
                execution_id=request.execution_id,
                tool_id=request.tool_id,
                graph_identity=request.graph_identity,
                operation_id=request.operation_id,
                attempt_id=request.attempt_id,
                provider_id="test-provider",
                provider_capability_checksum=capabilities.checksum,
                status=ExecutionStatus.SUCCEEDED,
                started_at=now,
                finished_at=now,
                termination_confirmed=True,
                reason_code="process_exit",
            )
        )

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, outcome_factory))
    profile = ExecutionProfile.external_process(
        provider_id="test-provider",
        allowed_argv_prefixes=(("mineru",),),
    )
    # The adapter is provider-neutral; the provider id is the only value it
    # needs to resolve.  Reuse the profile with the fake provider.
    return ResearchParserExecutionAdapter(
        execution_environment=registry,
        profile=profile,
    )


def test_parser_adapter_maps_roots_environment_and_identity(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    outcome = adapter(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "MINERU_MODEL_SOURCE=modelscope",
            "-v",
            f"{input_dir}:/input",
            "-v",
            f"{output_dir}:/output",
            "mineru:latest",
            "mineru",
            "-p",
            "/input/input.pdf",
            "-o",
            "/output",
        ],
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="mineru",
    )

    assert outcome.receipt.status is ExecutionStatus.SUCCEEDED
    request = captured[0]
    assert request.graph_identity == _identity()
    assert request.profile.mode.value == "external_process"
    assert request.image == "mineru:latest"
    assert request.argv[:2] == ("mineru", "-p")
    assert Path(request.argv[2]).resolve() == (input_dir / "input.pdf").resolve()
    assert request.read_roots == (str(input_dir.resolve()),)
    assert request.write_roots == (str(output_dir.resolve()),)
    assert request.environment == {"MINERU_MODEL_SOURCE": "modelscope"}


def test_parser_adapter_requires_exact_graph_identity(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])
    with pytest.raises(ExecutionPolicyViolationError) as error:
        adapter(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{tmp_path / 'input'}:/input",
                "-v",
                f"{tmp_path / 'output'}:/output",
                "mineru:latest",
                "mineru",
                "-p",
                "/input/input.pdf",
            ],
            timeout_seconds=60,
            execution_identity=None,
            paper_id="paper-1",
            backend="mineru",
        )
    assert error.value.reason_code == "graph_identity_required"
