from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.research.application.single_paper_runtime import (
    _generate_research_candidate_with_prepared_input,
)
from backend.research.domain.personal_memory import PersonalMemorySourceRef
from backend.research.domain.personal_memory_context import (
    PersonalMemoryContext,
    PersonalMemoryContextItem,
)
from framework.shared.graph_identity import GraphExecutionIdentity


def _context() -> PersonalMemoryContext:
    return PersonalMemoryContext(
        policy_revision=5,
        items=(PersonalMemoryContextItem(
            memory_id="pmem_" + "1" * 32,
            revision=3,
            content="Prefer methods-first explanations.",
            category="reading_preference",
            source_refs=(PersonalMemorySourceRef(
                source_type="user_input", source_ref="user://alice/preference",
            ),),
        ),),
    )


class _Provider:
    def __init__(self) -> None:
        self.context = _context()
        self.calls = 0
        self.revoked = False

    def current(self) -> PersonalMemoryContext:
        self.calls += 1
        if self.revoked:
            raise PermissionError("model processing permission revoked")
        return self.context


def _workspace(provider=None):
    return SimpleNamespace(
        request=SimpleNamespace(personal_memory_context=provider),
        input_context=None,
        used_personal_memory_context=None,
    )


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-1", graph_id="research.graph", graph_version="1",
        graph_ref="research.graph@1", graph_checksum="sha256:" + "1" * 64,
        node_id="analysis", node_instance_id="analysis:1",
        activity_id="activity-1", attempt=1,
    )


def _invoke(worker, workspace, *, identity=None):
    return _generate_research_candidate_with_prepared_input(
        worker, task="candidate_three_minute_read", workspace=workspace,
        payload={"objective": "Explain the method"}, execution_identity=identity,
    )


@pytest.mark.parametrize("with_identity", [False, True])
def test_memory_analysis_forwards_single_transport_attempt_and_exact_identity(with_identity) -> None:
    provider = _Provider()
    workspace = _workspace(provider)
    identity = _identity() if with_identity else None
    calls = []

    class Worker:
        def generate_candidate(self, *, task, payload, max_transport_attempts, execution_identity=None):
            calls.append((task, payload, max_transport_attempts, execution_identity))
            return {"candidate": "real worker result"}

    assert _invoke(Worker(), workspace, identity=identity) == {"candidate": "real worker result"}
    assert provider.calls == 1
    assert len(calls) == 1
    task, payload, attempts, actual_identity = calls[0]
    assert task == "candidate_three_minute_read"
    assert payload["objective"] == "Explain the method"
    assert payload["personal_memory_context"] == provider.context.to_worker_payload()
    assert attempts == 1
    assert actual_identity is identity
    assert workspace.used_personal_memory_context is provider.context


@pytest.mark.parametrize("with_identity", [False, True])
def test_no_memory_analysis_preserves_worker_without_transport_budget_interface(with_identity) -> None:
    workspace = _workspace()
    identity = _identity() if with_identity else None
    calls = []

    class Worker:
        def generate_candidate(self, *, task, payload, execution_identity=None):
            calls.append((task, payload, execution_identity))
            return {"candidate": "ordinary result"}

    assert _invoke(Worker(), workspace, identity=identity) == {"candidate": "ordinary result"}
    assert calls == [("candidate_three_minute_read", {"objective": "Explain the method"}, identity)]
    assert workspace.used_personal_memory_context is None


def test_memory_analysis_rejects_unsupported_worker_before_invocation_or_usage_receipt() -> None:
    provider = _Provider()
    workspace = _workspace(provider)
    calls = []

    class Worker:
        def generate_candidate(self, *, task, payload):
            calls.append(payload)
            return {}

    with pytest.raises(TypeError, match="must support max_transport_attempts"):
        _invoke(Worker(), workspace)
    assert calls == []
    assert provider.calls == 1
    assert workspace.used_personal_memory_context is None


def test_memory_analysis_rejects_positional_only_transport_budget() -> None:
    workspace = _workspace(_Provider())
    calls = []

    class Worker:
        def generate_candidate(self, max_transport_attempts, /, *, task, payload):
            calls.append(payload)
            return {}

    with pytest.raises(TypeError, match="must support max_transport_attempts"):
        _invoke(Worker(), workspace)
    assert calls == []
    assert workspace.used_personal_memory_context is None


@pytest.mark.parametrize("failure", [TypeError, TimeoutError])
def test_memory_analysis_never_retries_worker_error_without_transport_budget(failure) -> None:
    provider = _Provider()
    workspace = _workspace(provider)
    calls = []

    class Worker:
        def generate_candidate(self, *, task, payload, max_transport_attempts):
            calls.append(max_transport_attempts)
            raise failure("injected worker failure")

    with pytest.raises(failure, match="injected worker failure"):
        _invoke(Worker(), workspace)
    assert calls == [1]
    assert provider.calls == 1
    assert workspace.used_personal_memory_context is provider.context


@pytest.mark.parametrize("next_state", ["unchanged", "revoked", "changed"])
def test_each_memory_analysis_invocation_revalidates_frozen_context(next_state) -> None:
    provider = _Provider()
    original = provider.context
    workspace = _workspace(provider)
    calls = []

    class Worker:
        def generate_candidate(self, *, task, payload, max_transport_attempts):
            calls.append((payload["personal_memory_context"], max_transport_attempts))
            return {}

    worker = Worker()
    _invoke(worker, workspace)
    if next_state == "revoked":
        provider.revoked = True
        with pytest.raises(PermissionError, match="permission revoked"):
            _invoke(worker, workspace)
    elif next_state == "changed":
        provider.context = replace(original, policy_revision=original.policy_revision + 1)
        with pytest.raises(ValueError, match="changed during the research run"):
            _invoke(worker, workspace)
    else:
        _invoke(worker, workspace)
    assert provider.calls == 2
    assert len(calls) == (2 if next_state == "unchanged" else 1)
    assert all(payload == original.to_worker_payload() and attempts == 1 for payload, attempts in calls)
    assert workspace.used_personal_memory_context is original
