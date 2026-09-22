from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import pytest

from framework.agent.artifacts.stores import FilesystemArtifactStore
from framework.events import (
    EventRuntime,
    EventSchemaCatalog,
    EventStoreContentionError,
    TransactionalStateSnapshot,
)
from framework.agent.artifacts.models import (
    ArtifactRef,
    ArtifactWriteRequest,
    canonical_artifact_relative_path,
)
from framework.harness.subagents.tool_evidence import (
    CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
    ChildToolEvidenceCompleteness,
    ChildToolEvidenceLimits,
    ChildToolEvidenceScope,
    DurableChildToolEvidenceOwner,
    MetadataFirstChildToolEvidenceAuthorization,
)
from framework.shared.json import stable_json_dumps
from framework.shared.attempts import (
    AttemptContext,
    AttemptIdentity,
    AttemptOutcome,
    AttemptState,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolEvidencePersistenceError,
    ToolExecutor,
    ToolObservation,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
    ToolStatus,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore


class _StateStore:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], TransactionalStateSnapshot] = {}
        self.lock = Lock()
        self.contentions = 0
        self.before_cas: Callable[[], None] | None = None
        self.after_cas: Callable[[TransactionalStateSnapshot], None] | None = None

    def get_event(self, event_id, *, tenant_id=None):
        return None

    def read_stream(self, request):
        raise AssertionError("tool evidence owner must not read an event stream")

    def get_stream_high_watermark(self, stream_id, *, tenant_id=None):
        return None

    def load_transactional_state(self, namespace, key):
        with self.lock:
            return self.states.get((namespace, key))

    def publish(self, event, *, expected_last_sequence=None, unit_of_work=None):
        raise AssertionError("tool evidence owner must not publish standalone events")

    def publish_batch(self, events, *, expected_last_sequence=None):
        raise AssertionError("tool evidence owner must not publish standalone events")

    def publish_batch_with_state_cas(self, *args, **kwargs):
        raise AssertionError("tool evidence owner uses the isolated state CAS")

    def compare_and_swap_transactional_state(
        self,
        next_snapshot,
        *,
        expected_revision,
        expected_checksum,
    ):
        callback = self.before_cas
        self.before_cas = None
        if callback is not None:
            callback()
        with self.lock:
            if self.contentions:
                self.contentions -= 1
                raise EventStoreContentionError("injected contention")
            key = (next_snapshot.namespace, next_snapshot.key)
            current = self.states.get(key)
            if current is None:
                if expected_revision is not None or expected_checksum is not None:
                    raise EventStoreContentionError("missing expected state")
            elif (
                current.revision != expected_revision
                or current.checksum != expected_checksum
            ):
                raise EventStoreContentionError("state changed")
            self.states[key] = next_snapshot
        callback_after = self.after_cas
        if callback_after is not None:
            callback_after(next_snapshot)
        return next_snapshot


class _Artifacts:
    def __init__(self) -> None:
        self.storage: dict[str, bytes] = {}
        self.refs: dict[str, ArtifactRef] = {}
        self.read_count = 0

    def write(self, request: ArtifactWriteRequest) -> ArtifactRef:
        assert request.artifact_id is not None
        content = request.content_bytes()
        existing = self.storage.get(request.artifact_id)
        if existing is not None and existing != content:
            raise ValueError("immutable artifact identity conflict")
        self.storage[request.artifact_id] = content
        ref = ArtifactRef(
            artifact_id=request.artifact_id,
            run_id=request.run_id,
            scope_kind=request.scope_kind,
            graph_id=request.graph_id,
            graph_version=request.graph_version,
            graph_ref=request.graph_ref,
            graph_checksum=request.graph_checksum,
            node_id=request.node_id,
            node_instance_id=request.node_instance_id,
            graph_checkpoint_ref=request.graph_checkpoint_ref,
            activity_id=request.activity_id,
            attempt=request.attempt,
            artifact_type=request.artifact_type,
            path=canonical_artifact_relative_path(
                request,
                artifact_id=request.artifact_id,
            ),
            content_type=request.content_type,
            size_bytes=len(content),
            checksum=sha256(content).hexdigest(),
            redacted=request.redacted,
            created_at=request.created_at,
            metadata=dict(request.metadata),
        )
        self.refs[request.artifact_id] = ref
        return ref

    def read(self, ref: ArtifactRef) -> bytes:
        self.read_count += 1
        return self.storage[ref.artifact_id]

    def exists(self, ref: ArtifactRef) -> bool:
        return ref.artifact_id in self.storage


class _WriteOutcomeUnknownArtifacts(_Artifacts):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after_first_write = True

    def write(self, request: ArtifactWriteRequest) -> ArtifactRef:
        ref = super().write(request)
        if self.fail_after_first_write:
            self.fail_after_first_write = False
            raise OSError("injected unknown write outcome")
        return ref


class _Authorization:
    def __init__(self) -> None:
        self.authorized: list[tuple[str, str]] = []
        self.denied = False

    def authorize_artifact_read(
        self,
        scope,
        *,
        artifact_ref: ArtifactRef,
        artifact_type: str,
        expected_artifact_checksum: str,
        expected_byte_size: int,
    ) -> None:
        self.authorized.append((artifact_ref.artifact_id, artifact_type))
        if self.denied:
            raise ToolEvidencePersistenceError("artifact read is not authorized")


def _scope(**overrides: Any) -> ChildToolEvidenceScope:
    values = {
        "parent_graph_identity": GraphExecutionIdentity(
            run_id="run-tool-owner",
            graph_id="tool-owner-graph",
            graph_version="1",
            graph_ref="tool-owner-graph@1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analysis",
            node_instance_id="analysis:1",
            activity_id="analysis-activity",
            attempt=1,
        ),
        "stage_id": "research.analysis",
        "plan_id": "plan-tool-owner",
        "plan_version": 1,
        "group_id": "group-tool-owner",
        "wave_id": "wave-tool-owner-1",
        "task_id": "structure",
        "task_instance_id": "structure:1",
        "task_attempt": 1,
        "task_instance_checksum": "sha256:" + "b" * 64,
        "binding_ref": "subagent-binding://structure@1",
        "binding_checksum": "sha256:" + "c" * 64,
        "spawn_operation_key": "spawn://group-tool-owner/wave-1/structure-1",
        "owner_scope": "tenant-a/research",
        "tenant_id": "tenant-a",
        "input_grant_ref": "artifact://input-grant/structure-1",
        "input_grant_checksum": "sha256:" + "d" * 64,
        "policy_checksum": "sha256:" + "e" * 64,
        "reservation_key": "reservation://structure-1",
        "reservation_revision": 1,
        "allocation_checksum": "sha256:" + "f" * 64,
        "limits": ChildToolEvidenceLimits(
            max_logical_calls=4,
            max_physical_attempts=4,
        ),
    }
    values.update(overrides)
    return ChildToolEvidenceScope(**values)


def _owner(
    scope: ChildToolEvidenceScope,
    state: Any,
    artifacts: Any,
    authorization: Any,
    *,
    state_reader: Any | None = None,
) -> DurableChildToolEvidenceOwner:
    return DurableChildToolEvidenceOwner(
        scope,
        state_runtime=state,
        state_reader=state if state_reader is None else state_reader,
        artifact_store=artifacts,
        authorization_port=authorization,
        clock=lambda: datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )


def _call(
    owner: DurableChildToolEvidenceOwner,
    *,
    iteration: int = 1,
    ordinal: int = 0,
    call_id: str = "tool-call-1",
) -> ToolCall:
    turn = owner.commit_runner_turn(
        {
            "artifact_id": f"child:llm_call:{iteration}",
            "iteration": iteration,
            "request": {"messages": ["private candidate"]},
            "response": {"content": "tool candidate"},
            "metadata": {"agent_id": "structure-agent"},
        }
    )
    return ToolCall(
        tool_name="paper.lookup",
        arguments={"paper_id": "paper-1"},
        requested_by_agent_id="structure-agent",
        call_id=call_id,
        graph_identity=owner.scope.parent_graph_identity,
        metadata={
            "runner_turn_ref": turn.ref,
            "runner_turn_checksum": turn.checksum,
            "call_ordinal": ordinal,
        },
    )


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="paper.lookup",
        version="1.2.0",
        side_effect=ToolSideEffect.READ_ONLY,
        concurrency_safe=True,
    )


def _identity(attempt: int) -> AttemptIdentity:
    return AttemptIdentity(
        operation_id="tool:tool-call-1",
        operation_kind="tool_call",
        idempotency_key="tool:tool-call-1",
        attempt_id=f"attempt-{attempt}",
        local_attempt_no=attempt,
    )


def _outcome(attempt: int, state: AttemptState) -> AttemptOutcome[Any]:
    context = AttemptContext.create(
        idempotency_key="tool:tool-call-1",
        operation_id="tool:tool-call-1",
        operation_kind="tool_call",
        attempt_id=f"attempt-{attempt}",
        local_attempt_no=attempt,
    )
    return AttemptOutcome(
        context=context,
        state=state,
        value={"ok": True} if state is AttemptState.SUCCEEDED else None,
        error=(RuntimeError("expected failure") if state is AttemptState.FAILED else None),
        termination_confirmed=True,
    )


def _observation(call: ToolCall, *, status: ToolStatus) -> ToolObservation:
    return ToolObservation(
        call=call,
        result=ToolResult(
            status=status,
            output={"paper": "verified"} if status is ToolStatus.SUCCEEDED else None,
            error_type=("ExpectedFailure" if status is ToolStatus.FAILED else None),
            call_id=call.call_id,
            tool_name=call.tool_name,
            graph_identity=call.graph_identity,
            termination_confirmed=True,
        ),
    )


def test_zero_tool_scope_closes_with_authoritative_complete_index() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()

    index = owner.close_scope()
    reopened = _owner(scope, state, artifacts, authorization).close_scope()

    assert index == reopened
    assert index.completeness is ChildToolEvidenceCompleteness.COMPLETE
    assert index.disposition == "NO_TOOL_REQUESTS"
    assert index.registered_logical_calls == 0
    assert index.admitted_physical_attempts == 0
    assert index.unresolved_attempts == 0


def test_normal_retries_preserve_all_physical_receipts_and_budget_counts() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())

    owner.admit_attempt(call, _identity(1))
    owner.record_attempt_terminal(call, _outcome(1, AttemptState.FAILED))
    owner.admit_attempt(call, _identity(2))
    owner.record_attempt_terminal(call, _outcome(2, AttemptState.SUCCEEDED))
    receipt_ref = owner.commit_observation(
        _observation(call, status=ToolStatus.SUCCEEDED),
        _definition(),
    )
    index = owner.close_scope()

    assert receipt_ref.startswith("child-tool-evidence-")
    assert index.completeness is ChildToolEvidenceCompleteness.COMPLETE
    assert index.disposition == "TOOL_INVOCATIONS_RECORDED"
    assert index.registered_logical_calls == 1
    assert index.admitted_physical_attempts == 2
    assert index.known_terminal_attempts == 2
    assert index.unresolved_attempts == 0


def test_fresh_owner_never_reacquires_an_existing_unresolved_intent() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    first = _owner(scope, state, artifacts, authorization)
    first.open_scope()
    call = _call(first)
    first.register_call(call)
    first.bind_tool(call, _definition())
    first.admit_attempt(call, _identity(1))

    reopened = _owner(scope, state, artifacts, authorization)
    reopened.open_scope()
    with pytest.raises(ToolEvidencePersistenceError, match="cannot be reacquired"):
        reopened.admit_attempt(call, _identity(1))
    physical_calls = 0

    def handler(_arguments):
        nonlocal physical_calls
        physical_calls += 1
        return {"must_not": "execute"}

    registry = ToolRegistry()
    registry.register(_definition(), handler)
    with pytest.raises(ToolEvidencePersistenceError, match="physical intent"):
        ToolExecutor(registry, execution_evidence=reopened).execute(
            call,
            ToolPolicy(
                allowed_tools=["paper.lookup"],
                require_explicit_allowlist=True,
                require_approval_for_side_effects=False,
            ),
        )
    index = reopened.close_scope()

    assert physical_calls == 0
    assert index.completeness is ChildToolEvidenceCompleteness.INCOMPLETE
    assert index.disposition == "UNKNOWN"
    assert index.admitted_physical_attempts == 1
    assert index.known_terminal_attempts == 0
    assert index.unresolved_attempts == 2


def test_scope_close_retries_cas_and_includes_concurrent_closed_request() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    pending = _call(owner, iteration=2, call_id="concurrent-rejection")

    def concurrent_registration() -> None:
        owner.register_call(pending)
        rejection = ToolObservation(
            call=pending,
            result=ToolResult(
                status=ToolStatus.BLOCKED,
                error_type="ToolPermissionError",
                call_id=pending.call_id,
                tool_name=pending.tool_name,
                graph_identity=pending.graph_identity,
            ),
        )
        owner.commit_observation(rejection, None)

    state.before_cas = concurrent_registration
    index = owner.close_scope()

    assert index.completeness is ChildToolEvidenceCompleteness.COMPLETE
    assert index.disposition == "NO_TOOL_INVOCATIONS"
    assert index.registered_logical_calls == 1
    assert index.rejected_logical_calls == 1
    assert index.admitted_physical_attempts == 0


def test_scope_identity_conflict_is_rejected_on_reopen() -> None:
    original = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    _owner(original, state, artifacts, authorization).open_scope()
    conflicting = replace(original, owner_scope="tenant-b/research")

    with pytest.raises(ToolEvidencePersistenceError, match="ownership conflict"):
        _owner(conflicting, state, artifacts, authorization).open_scope()


def test_corrupt_receipt_fails_scope_close_instead_of_building_complete_index() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    owner.admit_attempt(call, _identity(1))
    owner.record_attempt_terminal(call, _outcome(1, AttemptState.SUCCEEDED))
    receipt = owner.commit_observation(
        _observation(call, status=ToolStatus.SUCCEEDED),
        _definition(),
    )
    document = json.loads(artifacts.storage[receipt].decode("utf-8"))
    document["payload"]["body"]["outcome"] = "FAILED"
    artifacts.storage[receipt] = stable_json_dumps(document).encode("utf-8")

    with pytest.raises(ToolEvidencePersistenceError, match="unreadable|checksum"):
        owner.close_scope()


def test_artifact_authorization_failure_occurs_before_payload_read() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    owner.commit_runner_turn(
        {
            "artifact_id": "child:llm_call:1",
            "iteration": 1,
            "request": {},
            "response": {},
            "metadata": {},
        }
    )
    authorization.denied = True
    reads_before = artifacts.read_count

    with pytest.raises(ToolEvidencePersistenceError, match="not authorized"):
        owner.commit_runner_turn(
            {
                "artifact_id": "child:llm_call:1",
                "iteration": 1,
                "request": {},
                "response": {},
                "metadata": {},
            }
        )

    assert artifacts.read_count == reads_before


def test_persisted_unknown_terminal_and_receipt_keep_index_incomplete() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    owner.admit_attempt(call, _identity(1))
    context = AttemptContext.create(
        idempotency_key="tool:tool-call-1",
        operation_id="tool:tool-call-1",
        operation_kind="tool_call",
        attempt_id="attempt-1",
        local_attempt_no=1,
    )
    owner.record_attempt_terminal(
        call,
        AttemptOutcome(
            context=context,
            state=AttemptState.INDETERMINATE,
            termination_confirmed=False,
            indeterminate=True,
            reason_code="tool_effect_indeterminate",
        ),
    )
    owner.commit_observation(
        ToolObservation(
            call=call,
            result=ToolResult(
                status=ToolStatus.FAILED,
                error_type="ToolIndeterminateError",
                call_id=call.call_id,
                tool_name=call.tool_name,
                graph_identity=call.graph_identity,
                termination_confirmed=False,
                indeterminate=True,
            ),
        ),
        _definition(),
    )

    index = owner.close_scope()

    assert index.completeness is ChildToolEvidenceCompleteness.INCOMPLETE
    assert index.disposition == "UNKNOWN"
    assert index.admitted_physical_attempts == 1
    assert index.known_terminal_attempts == 1
    assert index.unresolved_attempts == 2


def test_physical_retry_budget_is_cumulative_across_owner_reopen() -> None:
    scope = _scope(
        limits=ChildToolEvidenceLimits(
            max_logical_calls=2,
            max_physical_attempts=1,
        )
    )
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    owner.admit_attempt(call, _identity(1))
    owner.record_attempt_terminal(call, _outcome(1, AttemptState.FAILED))

    reopened = _owner(scope, state, artifacts, authorization)
    with pytest.raises(ToolEvidencePersistenceError, match="budget exhausted"):
        reopened.admit_attempt(call, _identity(2))

    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    assert snapshot.payload["physical_attempt_count"] == 1


def test_registration_retries_transient_cas_without_duplicate_logical_call() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    state.contentions = 1

    owner.register_call(call)

    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    assert len(snapshot.payload["logical_calls"]) == 1


def test_runner_turn_size_limit_rejects_before_artifact_write() -> None:
    scope = _scope(
        limits=ChildToolEvidenceLimits(
            max_logical_calls=1,
            max_physical_attempts=1,
            max_runner_turn_bytes=32,
        )
    )
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()

    with pytest.raises(ToolEvidencePersistenceError, match="size limit"):
        owner.commit_runner_turn(
            {
                "artifact_id": "oversized-runner-turn",
                "iteration": 1,
                "request": {"content": "x" * 128},
                "response": {},
                "metadata": {},
            }
        )

    assert artifacts.storage == {}


def test_zero_physical_budget_still_records_authoritative_rejection() -> None:
    scope = _scope(
        limits=ChildToolEvidenceLimits(
            max_logical_calls=1,
            max_physical_attempts=0,
        )
    )
    state = _StateStore()
    artifacts = _Artifacts()
    owner = _owner(scope, state, artifacts, _Authorization())
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.commit_observation(
        ToolObservation(
            call=call,
            result=ToolResult(
                status=ToolStatus.BLOCKED,
                error_type="ToolPermissionError",
                call_id=call.call_id,
                tool_name=call.tool_name,
                graph_identity=call.graph_identity,
            ),
        ),
        None,
    )

    index = owner.close_scope()

    assert index.completeness is ChildToolEvidenceCompleteness.COMPLETE
    assert index.disposition == "NO_TOOL_INVOCATIONS"
    assert index.rejected_logical_calls == 1
    assert index.admitted_physical_attempts == 0


def test_physical_attempt_cannot_switch_registered_operation_key() -> None:
    scope = _scope()
    state = _StateStore()
    owner = _owner(scope, state, _Artifacts(), _Authorization())
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    switched = replace(
        _identity(1),
        operation_id="tool:other-call",
        idempotency_key="tool:other-call",
    )

    with pytest.raises(ToolEvidencePersistenceError, match="operation identity"):
        owner.admit_attempt(call, switched)

    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    assert snapshot.payload["physical_attempt_count"] == 0


def test_closed_index_rereads_every_referenced_receipt() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    authorization = _Authorization()
    owner = _owner(scope, state, artifacts, authorization)
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    owner.admit_attempt(call, _identity(1))
    owner.record_attempt_terminal(call, _outcome(1, AttemptState.SUCCEEDED))
    receipt = owner.commit_observation(
        _observation(call, status=ToolStatus.SUCCEEDED),
        _definition(),
    )
    owner.close_scope()
    document = json.loads(artifacts.storage[receipt].decode("utf-8"))
    document["payload"]["body"]["outcome"] = "FAILED"
    artifacts.storage[receipt] = stable_json_dumps(document).encode("utf-8")

    with pytest.raises(ToolEvidencePersistenceError, match="unreadable|checksum"):
        _owner(scope, state, artifacts, authorization).close_scope()


def test_close_winning_terminal_race_keeps_unknown_and_rejects_late_terminal() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    owner = _owner(scope, state, artifacts, _Authorization())
    owner.open_scope()
    call = _call(owner)
    owner.register_call(call)
    owner.bind_tool(call, _definition())
    owner.admit_attempt(call, _identity(1))
    won: list[Any] = []

    def close_after_terminal_artifact_commit(
        snapshot: TransactionalStateSnapshot,
    ) -> None:
        reservations = snapshot.payload["artifact_reservations"]
        if not any(
            item["status"] == "COMMITTED"
            and item["artifact_type"].endswith("attempt-terminal.v1")
            for item in reservations.values()
        ):
            return
        state.after_cas = None
        won.append(owner.close_scope())

    state.after_cas = close_after_terminal_artifact_commit
    with pytest.raises(ToolEvidencePersistenceError, match="scope is closed"):
        owner.record_attempt_terminal(call, _outcome(1, AttemptState.SUCCEEDED))

    assert len(won) == 1
    index = won[0]
    assert index.completeness is ChildToolEvidenceCompleteness.INCOMPLETE
    assert index.disposition == "UNKNOWN"
    assert index.known_terminal_attempts == 0
    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    assert snapshot.payload["known_terminal_count"] == 0
    assert snapshot.payload["logical_calls"][next(iter(snapshot.payload["logical_calls"]))][
        "attempts"
    ][0]["terminal"] is None


def test_full_envelope_capacity_is_reserved_before_artifact_write() -> None:
    scope = _scope(
        limits=ChildToolEvidenceLimits(
            max_logical_calls=1,
            max_physical_attempts=0,
            max_runner_turn_bytes=1024,
            max_total_evidence_bytes=128,
        )
    )
    state = _StateStore()
    artifacts = _Artifacts()
    owner = _owner(scope, state, artifacts, _Authorization())
    owner.open_scope()

    with pytest.raises(ToolEvidencePersistenceError, match="capacity exhausted"):
        owner.commit_runner_turn({"response": "small body"})

    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    assert snapshot.payload["artifact_reservations"] == {}
    assert snapshot.payload["total_evidence_bytes"] == 0
    assert artifacts.storage == {}


def test_unknown_artifact_write_retries_same_reserved_immutable_identity() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _WriteOutcomeUnknownArtifacts()
    owner = _owner(scope, state, artifacts, _Authorization())
    owner.open_scope()
    turn = {"artifact_id": "turn-1", "response": {"content": "candidate"}}

    with pytest.raises(ToolEvidencePersistenceError, match="failed to write"):
        owner.commit_runner_turn(turn)
    with pytest.raises(ToolEvidencePersistenceError, match="unresolved artifact write"):
        owner.close_scope()
    evidence = owner.commit_runner_turn(turn)

    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    reservations = snapshot.payload["artifact_reservations"]
    assert len(reservations) == 1
    assert next(iter(reservations.values()))["status"] == "COMMITTED"
    assert set(artifacts.storage) == {evidence.ref}
    assert snapshot.payload["total_evidence_bytes"] == sum(
        len(content) for content in artifacts.storage.values()
    )


def test_concrete_metadata_guard_rejects_tampered_owner_before_body_read() -> None:
    scope = _scope()
    state = _StateStore()
    artifacts = _Artifacts()
    owner = _owner(
        scope,
        state,
        artifacts,
        MetadataFirstChildToolEvidenceAuthorization(),
    )
    owner.open_scope()
    evidence = owner.commit_runner_turn({"artifact_id": "turn-1", "response": {}})
    snapshot = state.load_transactional_state(
        CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
        scope.child_scope_key,
    )
    assert snapshot is not None
    payload = dict(snapshot.payload)
    reservations = {
        key: dict(value) for key, value in payload["artifact_reservations"].items()
    }
    reservation = next(iter(reservations.values()))
    descriptor = dict(reservation["descriptor"])
    artifact_ref = dict(descriptor["artifact_ref"])
    artifact_ref["metadata"] = {
        **artifact_ref["metadata"],
        "owner_scope": "tenant-b/research",
    }
    descriptor["artifact_ref"] = artifact_ref
    reservation["descriptor"] = descriptor
    payload["artifact_reservations"] = reservations
    state.states[(snapshot.namespace, snapshot.key)] = TransactionalStateSnapshot.create(
        namespace=snapshot.namespace,
        key=snapshot.key,
        revision=snapshot.revision + 1,
        payload=payload,
    )
    reads_before = artifacts.read_count

    with pytest.raises(ToolEvidencePersistenceError, match="metadata does not match"):
        owner.commit_runner_turn({"artifact_id": "turn-1", "response": {}})

    assert evidence.ref in artifacts.storage
    assert artifacts.read_count == reads_before


def test_sqlite_and_filesystem_reopen_preserves_pending_intent_without_execution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "child-tool-evidence.sqlite3"
    artifact_root = tmp_path / "child-tool-artifacts"
    scope = _scope()
    first_store = SQLiteEventStore(database)
    first_runtime = EventRuntime(
        store=first_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    first = _owner(
        scope,
        first_runtime,
        FilesystemArtifactStore(artifact_root),
        MetadataFirstChildToolEvidenceAuthorization(),
        state_reader=first_store,
    )
    first.open_scope()
    first.commit_runner_turn({"artifact_id": "filesystem-turn-a", "response": {}})
    first.commit_runner_turn({"artifact_id": "filesystem-turn-b", "response": {}})
    call = _call(first)
    first.register_call(call)
    first.bind_tool(call, _definition())
    first.admit_attempt(call, _identity(1))

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened = _owner(
        scope,
        reopened_runtime,
        FilesystemArtifactStore(artifact_root),
        MetadataFirstChildToolEvidenceAuthorization(),
        state_reader=reopened_store,
    )
    physical_calls = 0

    def handler(_arguments):
        nonlocal physical_calls
        physical_calls += 1
        return {"must_not": "execute"}

    registry = ToolRegistry()
    registry.register(_definition(), handler)
    with pytest.raises(ToolEvidencePersistenceError, match="physical intent"):
        ToolExecutor(registry, execution_evidence=reopened).execute(
            call,
            ToolPolicy(
                allowed_tools=["paper.lookup"],
                require_explicit_allowlist=True,
                require_approval_for_side_effects=False,
            ),
        )
    index = reopened.close_scope()

    assert physical_calls == 0
    assert index.completeness is ChildToolEvidenceCompleteness.INCOMPLETE
    assert index.admitted_physical_attempts == 1
    assert index.known_terminal_attempts == 0
