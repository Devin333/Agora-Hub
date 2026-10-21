from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionOutcome,
    ExecutionPolicyViolationError,
    ExecutionProfile,
    ExecutionReceipt,
    ExecutionStatus,
    FakeExecutionEnvironment,
)
from framework.shared.attempts import AttemptContext, bind_attempt_context
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.research.document_execution_adapter import (
    ResearchParserExecutionAdapter,
    _execution_failure_diagnostic,
)


def _identity(
    *,
    attempt: int = 1,
    node_instance_id: str = "compile-document-1",
    activity_id: str = "compile-document-activity",
) -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-parser",
        graph_id="research-graph",
        graph_version="1.0.0",
        graph_ref="research-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="compile_document",
        node_instance_id=node_instance_id,
        activity_id=activity_id,
        attempt=attempt,
    )


def _adapter(
    tmp_path: Path,
    captured: list[object],
    *,
    allowed_argv_prefixes: tuple[tuple[str, ...], ...] = (("marker",),),
    provider_available: bool = True,
    **adapter_kwargs,
) -> ResearchParserExecutionAdapter:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=provider_available,
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
        allowed_argv_prefixes=allowed_argv_prefixes,
    )
    # The adapter is provider-neutral; the provider id is the only value it
    # needs to resolve.  Reuse the profile with the fake provider.
    return ResearchParserExecutionAdapter(
        execution_environment=registry,
        profile=profile,
        **adapter_kwargs,
    )


def test_parser_adapter_cache_identity_uses_only_declared_configuration(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])

    first = adapter.cache_identity()
    adapter.last_outcome = object()

    assert adapter.cache_identity() == first
    assert first["profile"] == adapter._profile.to_dict()
    assert first["resource_limits"] == adapter._resource_limits.to_dict()
    assert first["cancellation_grace_seconds"] == 5.0
    assert first["provider_identity"] == "unknown"
    assert "last_outcome" not in first


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
            "MARKER_MODEL_SOURCE=modelscope",
            "-v",
            f"{input_dir}:/input",
            "-v",
            f"{output_dir}:/output",
            "marker:latest",
            "marker",
            "-p",
            "/input/input.pdf",
            "-o",
            "/output",
        ],
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    assert outcome.receipt.status is ExecutionStatus.SUCCEEDED
    request = captured[0]
    assert request.graph_identity == _identity()
    assert request.profile.mode.value == "external_process"
    assert request.image == "marker:latest"
    assert request.argv[:2] == ("marker", "-p")
    assert Path(request.argv[2]).resolve() == (input_dir / "input.pdf").resolve()
    assert request.read_roots == (str(input_dir.resolve()),)
    assert request.write_roots == (str(output_dir.resolve()),)
    assert request.environment == {"MARKER_MODEL_SOURCE": "modelscope"}
    assert request.resource_limits.max_processes == 1
    assert request.timeout_seconds == 60


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
                "marker:latest",
                "marker",
                "-p",
                "/input/input.pdf",
            ],
            timeout_seconds=60,
            execution_identity=None,
            paper_id="paper-1",
            backend="marker",
        )
    assert error.value.reason_code == "graph_identity_required"


def _command(tmp_path: Path, *, environment: str = "MARKER_MODEL_SOURCE=modelscope") -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-e",
        environment,
        "-v",
        f"{tmp_path / 'input'}:/input",
        "-v",
        f"{tmp_path / 'output'}:/output",
        "marker:latest",
        "marker",
        "-p",
        "/input/input.pdf",
        "-o",
        "/output",
    ]


def test_parser_adapter_rejects_injected_replay_outcome_shortcut(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    outcome = adapter(
        _command(tmp_path),
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    with pytest.raises(TypeError, match="unexpected keyword argument 'replay_outcome'"):
        adapter(
            _command(tmp_path, environment="TORCH_DEVICE=cpu"),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
            replay_outcome=outcome,
        )

    assert len(captured) == 1


def test_parser_adapter_blocks_live_execution_during_replay(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    replay_context = AttemptContext.create(
        idempotency_key="research-parser-replay-missing",
        admission_details={"replay": True},
    )

    with bind_attempt_context(replay_context):
        with pytest.raises(ExecutionPolicyViolationError) as error:
            adapter(
                _command(tmp_path),
                timeout_seconds=60,
                execution_identity=_identity(),
                paper_id="paper-1",
                backend="marker",
            )

    assert error.value.reason_code == "parser_live_execution_forbidden_during_replay"
    assert captured == []


def test_parser_adapter_blocks_cancelled_attempt_before_live_provider(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    context = AttemptContext.create(idempotency_key="research-parser-cancelled")
    context.cancel()

    with bind_attempt_context(context):
        with pytest.raises(ExecutionPolicyViolationError) as error:
            adapter(
                _command(tmp_path),
                timeout_seconds=60,
                execution_identity=_identity(),
                paper_id="paper-1",
                backend="marker",
            )

    assert error.value.reason_code == "parser_execution_cancelled"
    assert error.value.details["status"] == "cancelled"
    assert captured == []


def test_parser_adapter_preserves_indeterminate_receipt_and_stops(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    captured: list[object] = []
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
                status=ExecutionStatus.INDETERMINATE,
                started_at=now,
                finished_at=now,
                termination_confirmed=False,
                reason_code="timeout_termination_unknown",
            )
        )

    class _Store:
        def publish(self, *, artifact_type, payload, metadata=None):
            assert artifact_type == "parser-execution-evidence"
            assert payload["receipt"]["status"] == "indeterminate"
            return "artifact://research/parser-execution-evidence/failed-immutable"

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, outcome_factory))
    adapter = ResearchParserExecutionAdapter(
        execution_environment=registry,
        profile=ExecutionProfile.external_process(
            provider_id="test-provider",
            allowed_argv_prefixes=(("marker",),),
        ),
        execution_evidence_store=_Store(),
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as error:
        adapter(
            _command(tmp_path),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert len(captured) == 1
    assert error.value.details["status"] == "indeterminate"
    assert error.value.details["termination_confirmed"] is False
    assert error.value.details["receipt_checksum"].startswith("sha256:")
    assert error.value.details["execution_receipt_ref"].startswith(
        "execution-receipt://exec:"
    )
    assert error.value.details["execution_evidence_artifact_ref"] == (
        "artifact://research/parser-execution-evidence/failed-immutable"
    )


def test_parser_adapter_propagates_unconfirmed_state_before_evidence_publish_failure(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    captured: list[object] = []
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
                status=ExecutionStatus.INDETERMINATE,
                started_at=now,
                finished_at=now,
                termination_confirmed=False,
                reason_code="timeout_termination_unknown",
            )
        )

    class _FailingStore:
        def publish(self, *, artifact_type, payload, metadata=None):
            assert artifact_type == "parser-execution-evidence"
            assert payload["receipt"]["status"] == "indeterminate"
            raise RuntimeError("evidence publish failed")

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, outcome_factory))
    adapter = ResearchParserExecutionAdapter(
        execution_environment=registry,
        profile=ExecutionProfile.external_process(
            provider_id="test-provider",
            allowed_argv_prefixes=(("marker",),),
        ),
        execution_evidence_store=_FailingStore(),
    )
    attempt_context = AttemptContext.create(
        idempotency_key="research-parser-unconfirmed-publish-failure"
    )

    with bind_attempt_context(attempt_context):
        with pytest.raises(RuntimeError, match="evidence publish failed"):
            adapter(
                _command(tmp_path),
                timeout_seconds=60,
                execution_identity=_identity(),
                paper_id="paper-1",
                backend="marker",
            )

    assert len(captured) == 1
    assert attempt_context.has_indeterminate_descendant is True
    assert attempt_context.has_unconfirmed_descendant is True
    assert adapter.last_outcome.receipt.status is ExecutionStatus.INDETERMINATE


def test_parser_adapter_rejects_untyped_environment_options(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])

    with pytest.raises(ExecutionPolicyViolationError) as error:
        adapter(
            _command(tmp_path, environment="LD_PRELOAD=/tmp/evil.so"),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert error.value.reason_code == "parser_environment_not_allowed"


def test_parser_adapter_admits_only_fixed_runtime_confinement_environment(
    tmp_path: Path,
) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    allowed = {
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }

    for index, (name, value) in enumerate(allowed.items(), start=1):
        adapter(
            _command(tmp_path, environment=f"{name}={value}"),
            timeout_seconds=60,
            execution_identity=_identity(attempt=1, activity_id=f"runtime-env-{index}"),
            paper_id="paper-1",
            backend="marker",
        )

    assert [request.environment for request in captured] == [
        {name: value} for name, value in allowed.items()
    ]


def test_parser_adapter_rejects_expanded_runtime_environment(
    tmp_path: Path,
) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    rejected = (
        "OPENBLAS_NUM_THREADS=2",
        "OMP_NUM_THREADS=4",
        "MKL_NUM_THREADS=0",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "PATH=C:/Windows/System32",
    )

    for environment in rejected:
        with pytest.raises(ExecutionPolicyViolationError) as error:
            adapter(
                _command(tmp_path, environment=environment),
                timeout_seconds=60,
                execution_identity=_identity(),
                paper_id="paper-1",
                backend="marker",
            )
        assert error.value.reason_code == "parser_environment_not_allowed"

    assert captured == []



def test_parser_adapter_clamps_timeout_to_typed_budget(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured, max_timeout_seconds=30)

    adapter(
        _command(tmp_path),
        timeout_seconds=300,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    assert captured[0].timeout_seconds == 30


def test_parser_adapter_maps_only_admitted_cache_mount_and_environment(tmp_path: Path) -> None:
    captured: list[object] = []
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    adapter = _adapter(tmp_path, captured)
    command = _command(tmp_path, environment="HF_HOME=/root/.cache/huggingface")
    command[5:5] = ["-v", f"{cache_dir}:/root/.cache"]

    adapter(
        command,
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    request = captured[0]
    assert str(cache_dir.resolve()) in request.read_roots
    assert request.environment == {
        "HF_HOME": str((cache_dir / "huggingface").resolve())
    }


def test_parser_adapter_rejects_duplicate_or_unapproved_mounts(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])
    duplicate = _command(tmp_path)
    duplicate[5:5] = ["-v", f"{tmp_path / 'other-input'}:/input"]
    with pytest.raises(ExecutionPolicyViolationError) as duplicate_error:
        adapter(
            duplicate,
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )
    assert duplicate_error.value.reason_code == "parser_roots_invalid"

    unapproved = _command(tmp_path)
    unapproved[5:5] = ["-v", f"{tmp_path / 'socket'}:/var/run/docker.sock"]
    with pytest.raises(ExecutionPolicyViolationError) as unapproved_error:
        adapter(
            unapproved,
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )
    assert unapproved_error.value.reason_code == "parser_roots_invalid"



def test_parser_adapter_pins_marker_executable_without_path_environment(
    tmp_path: Path,
) -> None:
    captured: list[object] = []
    adapter = _adapter(
        tmp_path,
        captured,
        allowed_argv_prefixes=(("/usr/local/bin/marker_single",),),
    )
    command = _command(tmp_path)
    command[command.index("marker")] = "marker_single"

    adapter(
        command,
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    assert captured[0].argv[0] == "/usr/local/bin/marker_single"
    assert "PATH" not in captured[0].environment


def test_parser_execution_identity_is_unique_per_graph_attempt_and_node_instance(
    tmp_path: Path,
) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)

    cases = (
        (_identity(attempt=1), "paper-1"),
        (_identity(attempt=2), "paper-1"),
        (_identity(attempt=1, node_instance_id="compile-document-2"), "paper-1"),
        (_identity(attempt=1, activity_id="compile-document-activity-2"), "paper-1"),
        (_identity(attempt=1), "paper-2"),
    )
    for identity, paper_id in cases:
        adapter(
            _command(tmp_path),
            timeout_seconds=60,
            execution_identity=identity,
            paper_id=paper_id,
            backend="marker",
        )

    execution_ids = [request.execution_id for request in captured]
    attempt_ids = [request.attempt_id for request in captured]
    assert len(execution_ids) == len(set(execution_ids)) == 5
    assert len(attempt_ids) == len(set(attempt_ids)) == 5
    assert all(value.startswith("exec:marker:") for value in execution_ids)
    assert all(value.startswith("attempt:marker:") for value in attempt_ids)



def test_parser_adapter_rejects_freeform_model_source_or_network_mount(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, [])
    with pytest.raises(ExecutionPolicyViolationError) as source_error:
        adapter(
            _command(tmp_path, environment="MARKER_MODEL_SOURCE=../../model"),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )
    assert source_error.value.reason_code == "parser_environment_not_allowed"

    network_mount = _command(tmp_path)
    network_mount[5:5] = ["-v", r"\\server\share:/root/.cache"]
    with pytest.raises(ExecutionPolicyViolationError) as mount_error:
        adapter(
            network_mount,
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )
    assert mount_error.value.reason_code == "parser_command_invalid"


def test_parser_adapter_rejects_gpu_request_without_typed_provider_capability(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, [])
    command = _command(tmp_path)
    command[3:3] = ["--gpus", "all"]

    with pytest.raises(ExecutionPolicyViolationError) as captured:
        adapter(
            command,
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert captured.value.reason_code == "parser_gpu_capability_unavailable"


def test_parser_adapter_publishes_actual_request_and_receipt_as_immutable_artifact(
    tmp_path: Path,
) -> None:
    published: list[dict[str, object]] = []

    class _Store:
        def publish(self, *, artifact_type, payload, metadata=None):
            published.append({
                "artifact_type": artifact_type,
                "payload": payload,
                "metadata": metadata,
            })
            return "artifact://research/parser-execution-evidence/immutable"

    captured: list[object] = []
    adapter = _adapter(tmp_path, captured, execution_evidence_store=_Store())
    outcome = adapter(
        _command(tmp_path),
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    assert adapter.evidence_ref_for(outcome) == (
        "artifact://research/parser-execution-evidence/immutable"
    )
    assert len(published) == 1
    artifact = published[0]
    assert artifact["artifact_type"] == "parser-execution-evidence"
    payload = artifact["payload"]
    assert payload["request"]["execution_id"] == outcome.receipt.execution_id
    assert payload["request"]["argv"] == list(captured[0].argv)
    assert payload["receipt"] == outcome.receipt.to_dict()
    assert payload["ref"].startswith("parser-execution-evidence://")
    assert payload["checksum"].startswith("sha256:")


def test_parser_adapter_maps_trusted_marker_model_cache_directory(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    command = _command(tmp_path, environment="MODEL_CACHE_DIR=/root/.cache/datalab/models")
    command[5:5] = ["-v", f"{cache_dir}:/root/.cache"]

    adapter(
        command,
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    request = captured[0]
    assert request.environment["MODEL_CACHE_DIR"] == str(
        (cache_dir / "datalab" / "models").resolve()
    )
    assert str(cache_dir.resolve()) in request.read_roots


def test_parser_adapter_maps_trusted_marker_offline_font_path(tmp_path: Path) -> None:
    captured: list[object] = []
    adapter = _adapter(tmp_path, captured)
    cache_dir = tmp_path / "cache"
    font = cache_dir / "static" / "fonts" / "GoNotoCurrent-Regular.ttf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"official-font-fixture")
    command = _command(
        tmp_path,
        environment="FONT_PATH=/root/.cache/static/fonts/GoNotoCurrent-Regular.ttf",
    )
    command[5:5] = ["-v", f"{cache_dir}:/root/.cache"]

    adapter(
        command,
        timeout_seconds=60,
        execution_identity=_identity(),
        paper_id="paper-1",
        backend="marker",
    )

    request = captured[0]
    assert request.environment["FONT_PATH"] == str(font.resolve())
    assert str(cache_dir.resolve()) in request.read_roots


def test_parser_adapter_rejects_untrusted_marker_font_path(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])

    with pytest.raises(ExecutionPolicyViolationError) as captured:
        adapter(
            _command(tmp_path, environment="FONT_PATH=/tmp/substitute.ttf"),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert captured.value.reason_code == "parser_environment_not_allowed"


def test_parser_adapter_rejects_untrusted_marker_model_cache_directory(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, [])

    with pytest.raises(ExecutionPolicyViolationError) as captured:
        adapter(
            _command(tmp_path, environment="MODEL_CACHE_DIR=/tmp/models"),
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert captured.value.reason_code == "parser_environment_not_allowed"


def test_execution_failure_diagnostic_is_bounded_and_uses_output_tail() -> None:
    now = datetime.now(UTC)
    identity = _identity()
    receipt = ExecutionReceipt(
        execution_id="exec:marker:failed",
        tool_id="research.document-parser.marker",
        graph_identity=identity,
        operation_id="research-parser:marker:paper-1",
        attempt_id="attempt:marker:failed",
        provider_id="test-provider",
        provider_capability_checksum="sha256:" + "e" * 64,
        status=ExecutionStatus.FAILED,
        started_at=now,
        finished_at=now,
        termination_confirmed=True,
        reason_code="process_exit_nonzero",
        exit_code=1,
    )
    details = _execution_failure_diagnostic(
        ExecutionOutcome(
            receipt=receipt,
            output=("prefix-" + "x" * 5000 + "-marker-root-cause").encode(),
            diagnostic="provider diagnostic",
        )
    )

    assert details["provider_diagnostic"] == "provider diagnostic"
    assert len(details["process_output_excerpt"]) == 4096
    assert details["process_output_excerpt"].endswith("-marker-root-cause")

def test_parser_adapter_reports_unavailable_provider_before_gpu_admission(
    tmp_path: Path,
) -> None:
    executed: list[object] = []
    adapter = _adapter(tmp_path, executed, provider_available=False)
    command = _command(tmp_path)
    command[3:3] = ["--gpus", "all"]

    with pytest.raises(ExecutionEnvironmentUnavailableError) as captured:
        adapter(
            command,
            timeout_seconds=60,
            execution_identity=_identity(),
            paper_id="paper-1",
            backend="marker",
        )

    assert captured.value.reason_code == "execution_environment_unavailable"
    assert captured.value.details["provider_id"] == "test-provider"
    assert captured.value.details["missing"] == ["provider_unavailable"]
    assert executed == []
