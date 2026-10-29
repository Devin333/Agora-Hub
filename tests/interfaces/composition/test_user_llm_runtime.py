from __future__ import annotations

import json
import threading

import pytest

from backend.foundation.llm_connections import (
    ConnectionOwner,
    CredentialBinding,
    LLMPurpose,
    ModelCapabilities,
    ModelConnection,
    ModelConnectionNotFoundError,
    ModelConnectionStoreUnavailableError,
    ModelConnectionUnavailable,
    ModelConnectionUnavailableReason,
    PurposeRoutedLLMClient,
    StoredConnection,
    connection_call_window,
    current_llm_owner,
    llm_call_scope,
    owner_from_actor_metadata,
)
from framework.llm.models import LLMRequest, LLMResponse
from framework.llm.models.reasoning import ReasoningEffort
from framework.shared.secret_text import SecretText
from infrastructure.security.credential_cipher import AesGcmCredentialCipher, CredentialKeyRing
from interfaces.composition.user_llm_runtime import (
    ConnectionClientOptions,
    UserLLMClientResolver,
    client_settings,
)
from framework.llm.providers import build_provider_client

E = ReasoningEffort
ALICE = ConnectionOwner("alice", tenant_id="lab")
BOB = ConnectionOwner("bob", tenant_id="lab")
CIPHER = AesGcmCredentialCipher(CredentialKeyRing(current_version=1, keys={1: b"k" * 32}))


def _connection(owner=ALICE, connection_id="conn-1", revision=1, **changes) -> ModelConnection:
    values = dict(
        connection_id=connection_id,
        owner=owner,
        provider="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-6.1-sol",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_effort=E.MEDIUM,
        capabilities=ModelCapabilities(reasoning_efforts=(E.LOW, E.MEDIUM, E.HIGH)),
        credential_configured=True,
        revision=revision,
    )
    values.update(changes)
    return ModelConnection(**values)


class InMemoryRepository:
    """Just enough of ModelConnectionRepository for the resolver."""

    def __init__(self) -> None:
        self.rows: dict[tuple[ConnectionOwner, str], StoredConnection] = {}
        self.assignments: dict[tuple[ConnectionOwner, LLMPurpose], str] = {}
        self.credential_loads = 0
        self.unavailable = False

    def put(self, connection: ModelConnection, secret: str) -> None:
        sealed = CIPHER.encrypt(SecretText(secret), binding=CredentialBinding(connection.owner, connection.connection_id))
        self.rows[(connection.owner, connection.connection_id)] = StoredConnection(connection, sealed)

    def assign(self, owner, purpose, connection_id) -> None:
        self.assignments[(owner, purpose)] = connection_id

    def resolve_purpose(self, owner, purpose):
        if self.unavailable:
            raise ModelConnectionStoreUnavailableError()
        connection_id = self.assignments.get((owner, purpose))
        stored = self.rows.get((owner, connection_id))
        return None if stored is None else stored.connection

    def load_with_credential(self, owner, connection_id):
        self.credential_loads += 1
        stored = self.rows.get((owner, connection_id))
        if stored is None:
            raise ModelConnectionNotFoundError()
        return stored


class RecordingClient:
    def __init__(self, connection, api_key):
        self.connection = connection
        self.api_key = api_key.reveal()
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return LLMResponse(content=f"{self.connection.model}:{self.api_key}")

    def stream(self, request):
        self.requests.append(request)
        return iter(())


@pytest.fixture
def setup():
    repository = InMemoryRepository()
    built: list[RecordingClient] = []

    def builder(connection, api_key):
        client = RecordingClient(connection, api_key)
        built.append(client)
        return client

    resolver = UserLLMClientResolver(repository, CIPHER, client_builder=builder)
    return repository, resolver, built


def _ask(client: PurposeRoutedLLMClient, **request) -> LLMResponse:
    return client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}], **request))


def test_each_owner_uses_their_own_connection_and_key(setup) -> None:
    repository, resolver, _ = setup
    repository.put(_connection(), "sk-alice")
    repository.put(_connection(owner=BOB, model="gpt-6-luna"), "sk-bob")
    repository.assign(ALICE, LLMPurpose.ANSWER, "conn-1")
    repository.assign(BOB, LLMPurpose.ANSWER, "conn-1")
    client = resolver.routed_client(LLMPurpose.ANSWER)

    with llm_call_scope(ALICE):
        assert _ask(client).content == "gpt-6.1-sol:sk-alice"
    with llm_call_scope(BOB):
        assert _ask(client).content == "gpt-6-luna:sk-bob"
    assert current_llm_owner() is None


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (lambda repo: None, ModelConnectionUnavailableReason.NOT_ASSIGNED),
        (lambda repo: setattr(repo, "unavailable", True), ModelConnectionUnavailableReason.STORE_UNAVAILABLE),
    ],
)
def test_missing_connection_never_falls_back(setup, prepare, reason) -> None:
    repository, resolver, built = setup
    prepare(repository)
    with llm_call_scope(ALICE), pytest.raises(ModelConnectionUnavailable) as caught:
        _ask(resolver.routed_client(LLMPurpose.EVIDENCE_SUMMARY))
    assert caught.value.reason is reason
    assert caught.value.purpose is LLMPurpose.EVIDENCE_SUMMARY
    assert built == []


def test_calls_outside_a_scope_have_no_caller(setup) -> None:
    _, resolver, _ = setup
    with pytest.raises(ModelConnectionUnavailable) as caught:
        _ask(resolver.routed_client(LLMPurpose.ANSWER))
    assert caught.value.reason is ModelConnectionUnavailableReason.NO_CALLER


def test_unreadable_key_is_reported_not_retried(setup) -> None:
    repository, resolver, _ = setup
    connection = _connection()
    foreign = CIPHER.encrypt(SecretText("sk"), binding=CredentialBinding(BOB, "conn-1"))
    repository.rows[(ALICE, "conn-1")] = StoredConnection(connection, foreign)
    repository.assign(ALICE, LLMPurpose.ANSWER, "conn-1")
    with llm_call_scope(ALICE), pytest.raises(ModelConnectionUnavailable) as caught:
        _ask(resolver.routed_client(LLMPurpose.ANSWER))
    assert caught.value.reason is ModelConnectionUnavailableReason.CREDENTIAL_UNREADABLE
    assert not caught.value.retryable


def test_clients_are_cached_per_revision_and_rebuilt_after_an_edit(setup) -> None:
    repository, resolver, built = setup
    repository.put(_connection(), "sk-old")
    repository.assign(ALICE, LLMPurpose.ANSWER, "conn-1")
    client = resolver.routed_client(LLMPurpose.ANSWER)
    with llm_call_scope(ALICE):
        _ask(client)
        _ask(client)
        assert (len(built), repository.credential_loads) == (1, 1)

        repository.put(_connection(revision=2, model="gpt-6-astra"), "sk-new")
        assert _ask(client).content == "gpt-6-astra:sk-new"
        assert (len(built), repository.credential_loads) == (2, 2)


def test_reassignment_takes_effect_on_the_next_call(setup) -> None:
    repository, resolver, _ = setup
    repository.put(_connection(), "sk-1")
    repository.put(_connection(connection_id="fast", model="gpt-6-luna"), "sk-2")
    repository.assign(ALICE, LLMPurpose.EVIDENCE_SUMMARY, "conn-1")
    client = resolver.routed_client(LLMPurpose.EVIDENCE_SUMMARY)
    with llm_call_scope(ALICE):
        assert _ask(client).content.startswith("gpt-6.1-sol")
        repository.assign(ALICE, LLMPurpose.EVIDENCE_SUMMARY, "fast")
        assert _ask(client).content.startswith("gpt-6-luna")


def test_request_is_pointed_at_the_connection_model_and_output_cap(setup) -> None:
    repository, resolver, built = setup
    repository.put(_connection(max_output_tokens=8_000), "sk")
    repository.assign(ALICE, LLMPurpose.ANSWER, "conn-1")
    with llm_call_scope(ALICE):
        _ask(resolver.routed_client(LLMPurpose.ANSWER), model="settings-model", max_tokens=50_000)
    sent = built[0].requests[0]
    assert (sent.model, sent.max_tokens) == ("gpt-6.1-sol", 8_000)


def test_scope_is_isolated_between_threads(setup) -> None:
    repository, resolver, _ = setup
    repository.put(_connection(), "sk-alice")
    repository.assign(ALICE, LLMPurpose.ANSWER, "conn-1")
    errors = []

    def other_thread():
        try:
            _ask(resolver.routed_client(LLMPurpose.ANSWER))
        except ModelConnectionUnavailable as exc:
            errors.append(exc.reason)

    with llm_call_scope(ALICE):
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
    assert errors == [ModelConnectionUnavailableReason.NO_CALLER]


@pytest.mark.parametrize(
    ("window", "max_output", "requested", "expected"),
    [
        (1_050_000, 128_000, 2_048, (1_000_000, 2_048)),
        (200_000, 8_000, 16_000, (192_000, 8_000)),
        (8_192, None, 6_000, (4_096, 4_096)),
    ],
)
def test_call_window_follows_the_connection(window, max_output, requested, expected) -> None:
    connection = _connection(context_window_tokens=window, max_output_tokens=max_output)
    budget = connection_call_window(connection, requested_output_tokens=requested)
    assert (budget.max_input_tokens, budget.max_output_tokens) == expected


def test_owner_is_recovered_from_persisted_actor_metadata() -> None:
    assert owner_from_actor_metadata({"user_id": "alice", "tenant_id": "lab"}) == ALICE
    assert owner_from_actor_metadata({"user_id": "alice", "tenant_id": ""}) == ConnectionOwner("alice")
    assert owner_from_actor_metadata({"tenant_id": "lab"}) is None
    assert owner_from_actor_metadata({"user_id": "has space"}) is None
    assert owner_from_actor_metadata(None) is None


def test_client_settings_carry_the_connection_into_the_vendor_client() -> None:
    sent = []

    def transport(request, _timeout):
        sent.append(request)
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    connection = _connection(reasoning_effort=E.HIGH)
    settings = client_settings(connection, SecretText("sk-user"), options=ConnectionClientOptions())
    assert settings.deployment_id == "conn-1@1"
    client = build_provider_client(settings, transport=transport)
    client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}], max_tokens=5))
    body = json.loads(sent[0].data)
    assert body["model"] == "gpt-6.1-sol" and body["reasoning_effort"] == "high"
    assert sent[0].get_header("Authorization") == "Bearer sk-user"
