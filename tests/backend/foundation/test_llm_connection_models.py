from __future__ import annotations

import pytest

from backend.foundation.llm_connections import (
    ConnectionOwner,
    LLMPurpose,
    ModelCapabilities,
    ModelConnection,
    ModelConnectionUnavailable,
    ModelConnectionUnavailableReason,
    PurposeAssignment,
)
from framework.llm.models.reasoning import ReasoningEffort
from framework.llm.providers import ProviderKind

E = ReasoningEffort
OWNER = ConnectionOwner("alice", tenant_id="lab")


def _connection(**changes) -> ModelConnection:
    values = dict(
        connection_id="conn-1",
        owner=OWNER,
        provider="openai",
        display_name="  OpenAI  ",
        base_url="https://api.openai.com/v1/",
        model="gpt-6.1-sol",
        context_window_tokens=1_050_000,
        capabilities=ModelCapabilities(reasoning_efforts=(E.MAX, E.LOW, E.MEDIUM)),
        reasoning_effort="medium",
    )
    values.update(changes)
    return ModelConnection(**values)


def test_connection_normalizes_its_fields() -> None:
    connection = _connection()
    assert connection.provider is ProviderKind.OPENAI
    assert connection.display_name == "OpenAI"
    assert connection.base_url == "https://api.openai.com/v1"
    assert connection.reasoning_effort is E.MEDIUM
    assert connection.capabilities.reasoning_efforts == (E.LOW, E.MEDIUM, E.MAX)
    assert connection.deployment_id == "conn-1@1"
    assert connection.revised(revision=4).deployment_id == "conn-1@4"


@pytest.mark.parametrize(
    "changes",
    [
        {"reasoning_effort": "xhigh"},
        {"connection_id": "has space"},
        {"connection_id": "../escape"},
        {"display_name": "line\nbreak"},
        {"base_url": "ftp://files.example.com"},
        {"context_window_tokens": 100},
        {"context_window_tokens": True},
        {"max_output_tokens": 2_000_000},
        {"revision": 0},
        {"provider": "cohere"},
    ],
)
def test_connection_rejects_invalid_settings(changes) -> None:
    with pytest.raises((TypeError, ValueError)):
        _connection(**changes)


def test_owner_identifiers_are_validated() -> None:
    assert ConnectionOwner("u-1").tenant_scope == ""
    with pytest.raises(ValueError):
        ConnectionOwner("bad id")
    with pytest.raises(ValueError):
        ConnectionOwner("alice", tenant_id="")


def test_capabilities_round_trip() -> None:
    capabilities = ModelCapabilities(reasoning_efforts=(E.HIGH, E.OFF), thinking_off="between_tools")
    assert ModelCapabilities.from_dict(capabilities.to_dict()) == capabilities


def test_assignment_and_unavailable_error_are_public_safe() -> None:
    assert PurposeAssignment("answer", "conn-1").purpose is LLMPurpose.ANSWER
    error = ModelConnectionUnavailable(LLMPurpose.EVIDENCE_SUMMARY, ModelConnectionUnavailableReason.NOT_ASSIGNED)
    assert error.to_public_dict() == {
        "code": "model_connection_required",
        "message": "Configure a model connection for this feature in settings.",
        "purpose": "evidence_summary",
        "reason": "not_assigned",
        "retryable": False,
    }
    assert ModelConnectionUnavailable("answer", "store_unavailable").retryable
