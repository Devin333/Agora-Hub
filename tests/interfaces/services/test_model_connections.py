from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from urllib.error import HTTPError

import pytest

from framework.llm import LLMRequest
from framework.llm.clients import (
    InvocationLLMClient,
    LLMCredentialUnavailableError,
    LLMWireProtocol,
    OpenAICompatibleClient,
    bind_llm_endpoint_source,
)
from framework.llm.clients.openai_compatible import LLMRetryPolicy
from framework.llm.models.reasoning import ReasoningEffort, ReasoningWire
from infrastructure.security.outbound_address import OutboundAddressPolicy
from interfaces.services.model_connections import (
    ConnectionDraft,
    CredentialCipher,
    LocalJsonModelConnectionRepository,
    ModelConnectionConfigurationError,
    ModelConnectionError,
    ModelConnectionService,
    ModelPurpose,
    PurposeRequirement,
    migrate_single_key_credentials,
    parse_model_provider_catalog,
)
from interfaces.services.model_connections.cipher import single_key_credential_binding

KEY_A = "sk-account-a-0001"
KEY_B = "sk-account-b-0002"
KEY_C = "sk-ant-account-c-0003"

CATALOG = {
    "schema_version": "hosted-llm-providers.v2",
    "providers": [
        {
            "id": "deepseek",
            "label": "DeepSeek 官方",
            "protocol": "openai_chat",
            "base_url": "https://api.deepseek.com",
            "default_model": "deepseek-flash",
            "models": [
                {
                    "id": "deepseek-flash",
                    "label": "DeepSeek V4.1 Flash",
                    "context_window_tokens": 1_000_000,
                    "max_output_tokens": 384_000,
                    "reasoning": {"parameter": "deepseek_thinking", "efforts": ["off", "low", "high", "max"], "default": "high"},
                    "structured_output": {"mode": "json_object", "revision": "r1", "source": "https://example.com"},
                },
                {
                    "id": "deepseek-small",
                    "label": "Small",
                    "context_window_tokens": 16_000,
                    "reasoning": {"parameter": "none"},
                    "structured_output": {"mode": "none", "revision": "r1", "source": "https://example.com"},
                },
            ],
        },
        {
            "id": "anthropic",
            "label": "Anthropic",
            "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "default_model": "claude-sonnet-5-5",
            "models": [
                {
                    "id": "claude-sonnet-5-5",
                    "label": "Claude Sonnet 5.5",
                    "context_window_tokens": 1_000_000,
                    "max_output_tokens": 128_000,
                    "reasoning": {"parameter": "anthropic_effort", "efforts": ["low", "medium", "high", "xhigh", "max"], "default": "high"},
                    "structured_output": {"mode": "json_object", "revision": "r1", "source": "https://example.com"},
                }
            ],
        },
        {
            "id": "custom-openai-compatible",
            "label": "自定义（OpenAI 兼容）",
            "protocol": "openai_chat",
            "custom": True,
            "reasoning_parameters": ["none", "openai_reasoning_effort", "openrouter_reasoning"],
            "structured_output_modes": ["json_object", "none"],
        },
    ],
}

REQUIREMENTS = {
    ModelPurpose.ANSWER: PurposeRequirement(input_tokens=32_000, output_tokens=900),
    ModelPurpose.EVIDENCE_SUMMARY: PurposeRequirement(input_tokens=8_000, output_tokens=3_000),
    ModelPurpose.SECTION_NAVIGATION: PurposeRequirement(input_tokens=8_000, output_tokens=1_500),
    ModelPurpose.CANDIDATE_GENERATION: PurposeRequirement(
        input_tokens=100_000, output_tokens=8_000, structured_output=True
    ),
}


def _master(seed: int) -> bytes:
    return bytes((seed + index) % 256 for index in range(32))


class _AnthropicLister:
    def __init__(self, models, calls):
        self._models = models
        self._calls = calls

    def list_models(self):
        self._calls.append("anthropic:list")
        return tuple(self._models)

    def complete(self, request):  # pragma: no cover - never reached in these tests
        raise AssertionError("no completion expected")


def _probe_factory(transport=None, *, anthropic_models=("claude-sonnet-5-5",), calls=None):
    calls = calls if calls is not None else []

    def build(endpoint):
        if endpoint.protocol is LLMWireProtocol.ANTHROPIC_MESSAGES:
            return _AnthropicLister(anthropic_models, calls)
        return OpenAICompatibleClient.for_endpoint(
            endpoint,
            timeout_seconds=5,
            transport=transport or (lambda request, timeout: (_ for _ in ()).throw(AssertionError("no call"))),
            retry_policy=LLMRetryPolicy(max_attempts=1),
        )

    return build


def _service(tmp_path, *, master: bytes | None = None, transport=None, outbound=None, ids=None, **kwargs):
    counter = iter(ids or [f"mc-{index:04d}" for index in range(1, 100)])
    return ModelConnectionService(
        catalog=parse_model_provider_catalog(CATALOG),
        cipher=CredentialCipher(master or _master(1)),
        repository=LocalJsonModelConnectionRepository.under(tmp_path),
        requirements=REQUIREMENTS,
        probe_client_factory=kwargs.pop("probe", None) or _probe_factory(transport),
        outbound_policy=outbound or OutboundAddressPolicy(),
        id_factory=lambda: next(counter),
        **kwargs,
    )


def _deepseek(**overrides) -> ConnectionDraft:
    values = {"provider_id": "deepseek", "model": "deepseek-flash", "api_key": KEY_A}
    values.update(overrides)
    return ConnectionDraft(**values)


def _custom(**overrides) -> ConnectionDraft:
    values = {
        "provider_id": "custom-openai-compatible",
        "model": "qwen3-max",
        "api_key": KEY_B,
        "base_url": "https://gateway.example.com/v1",
        "context_window_tokens": 128_000,
        "max_output_tokens": 16_000,
        "reasoning_parameter": "openai_reasoning_effort",
        "reasoning_effort": "low",
        "structured_output": "json_object",
    }
    values.update(overrides)
    return ConnectionDraft(**values)


def _model_list(*model_ids: str) -> bytes:
    return json.dumps({"object": "list", "data": [{"id": item, "object": "model"} for item in model_ids]}).encode()


def _http_error(status: int):
    def transport(request, timeout):
        raise HTTPError(request.full_url, status, "failed", {}, BytesIO(b"{}"))

    return transport


def _connection(view, connection_id):
    return next(item for item in view["connections"] if item["id"] == connection_id)


# -- storage and keys ---------------------------------------------------------


def test_key_is_sealed_never_returned_and_unassigned_purposes_fail_closed(tmp_path) -> None:
    service = _service(tmp_path)

    view = service.create("user-a", _deepseek(), expected_revision=0)

    connection = _connection(view, "mc-0001")
    assert connection["status"] == "unverified"
    assert connection["credentialConfigured"] is True
    assert connection["keyHint"] == KEY_A[-4:]
    assert connection["reasoningEffort"] is None
    assert KEY_A not in json.dumps(view)
    assert KEY_A not in (tmp_path / "model-connections.json").read_text(encoding="utf-8")
    assert view["assignments"] == {purpose.value: None for purpose in ModelPurpose}
    with pytest.raises(ModelConnectionError) as missing:
        service.endpoint_for("user-a", ModelPurpose.ANSWER)
    assert missing.value.code == "model_connection_required"
    assert missing.value.details["purpose"] == "answer"
    assert missing.value.details["purposeLabel"] == "回答"


def test_ciphertext_moved_onto_another_connection_does_not_open(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)
    service.create("user-a", _deepseek(api_key=KEY_B, label="second"), expected_revision=1)
    service.assign("user-a", {"answer": "mc-0002"}, expected_revision=2)
    path = tmp_path / "model-connections.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    first, second = document["accounts"][0]["connections"]
    second["sealed"] = first["sealed"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ModelConnectionError) as raised:
        service.endpoint_for("user-a", ModelPurpose.ANSWER)

    assert raised.value.code == "model_connection_key_missing"


def test_master_key_change_reports_rotation(tmp_path) -> None:
    _service(tmp_path, master=_master(1)).create("user-a", _deepseek(), expected_revision=0)
    rotated = _service(tmp_path, master=_master(2))

    connection = _connection(rotated.view("user-a"), "mc-0001")

    assert connection["status"] == "key_rotated"
    assert connection["keyHint"] is None
    with pytest.raises(ModelConnectionError) as raised:
        rotated.update("user-a", "mc-0001", _deepseek(api_key=None), expected_revision=1)
    assert raised.value.code == "model_connection_key_rotated"
    rotated.update("user-a", "mc-0001", _deepseek(api_key=KEY_B), expected_revision=1)
    assert _connection(rotated.view("user-a"), "mc-0001")["status"] == "unverified"


def test_stale_revision_is_a_conflict(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)

    with pytest.raises(ModelConnectionError) as raised:
        service.create("user-a", _deepseek(), expected_revision=0)

    assert raised.value.code == "model_connection_conflict"


def test_connection_count_is_limited(tmp_path) -> None:
    service = _service(tmp_path, max_connections=2)
    service.create("user-a", _deepseek(), expected_revision=0)
    service.create("user-a", _deepseek(), expected_revision=1)

    with pytest.raises(ModelConnectionError) as raised:
        service.create("user-a", _deepseek(), expected_revision=2)

    assert raised.value.code == "model_connection_limit_reached"


# -- drafts ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("draft", "code"),
    (
        (_deepseek(provider_id="openai"), "model_provider_unsupported"),
        (_deepseek(model="deepseek-v1"), "model_provider_unsupported"),
        (_deepseek(reasoning_effort="medium"), "model_connection_invalid"),
        (_deepseek(base_url="https://elsewhere.example.com"), "model_connection_invalid"),
        (_deepseek(context_window_tokens=2_000_000), "model_connection_invalid"),
        (_deepseek(api_key="short"), "model_connection_invalid"),
        (_deepseek(api_key=None), "model_connection_key_required"),
        (_custom(base_url=None), "model_connection_invalid"),
        (_custom(base_url="ftp://gateway.example.com"), "model_connection_invalid"),
        (_custom(base_url="http://gateway.example.com/v1"), "model_endpoint_not_allowed"),
        (_custom(context_window_tokens=None), "model_connection_invalid"),
        (_custom(max_output_tokens=128_000), "model_connection_invalid"),
        (_custom(reasoning_parameter="anthropic_effort"), "model_connection_invalid"),
        (_custom(reasoning_parameter="none", reasoning_effort="low"), "model_connection_invalid"),
        (_custom(structured_output="native_strict"), "model_connection_invalid"),
        (_custom(model="bad model id"), "model_connection_invalid"),
    ),
)
def test_drafts_are_checked_against_the_catalog(tmp_path, draft, code) -> None:
    with pytest.raises(ModelConnectionError) as raised:
        _service(tmp_path).create("user-a", draft, expected_revision=0)

    assert raised.value.code == code


def test_custom_connection_keeps_what_the_user_entered(tmp_path) -> None:
    service = _service(tmp_path)

    connection = _connection(service.create("user-a", _custom(label=" 网关 "), expected_revision=0), "mc-0001")

    assert connection["label"] == "网关"
    assert connection["custom"] is True
    assert connection["baseUrl"] == "https://gateway.example.com/v1"
    assert connection["contextWindowTokens"] == 128_000
    assert connection["reasoningParameter"] == "openai_reasoning_effort"
    assert connection["reasoningEffort"] == "low"
    assert connection["structuredOutput"] == "json_object"


def test_allowlisted_private_gateway_may_use_plain_http(tmp_path) -> None:
    outbound = OutboundAddressPolicy(allowed_hosts=frozenset({"host.docker.internal"}))
    service = _service(tmp_path, outbound=outbound)

    view = service.create(
        "user-a", _custom(base_url="http://host.docker.internal:11434/v1"), expected_revision=0
    )

    assert _connection(view, "mc-0001")["baseUrl"] == "http://host.docker.internal:11434/v1"


# -- update and delete ------------------------------------------------------------


def test_update_without_key_keeps_the_key_and_model_change_needs_new_verification(tmp_path) -> None:
    service = _service(tmp_path, transport=lambda request, timeout: _model_list("deepseek-flash", "deepseek-small"))
    service.create("user-a", _deepseek(), expected_revision=0)
    service.verify("user-a", "mc-0001")

    relabelled = service.update("user-a", "mc-0001", _deepseek(api_key=None, label="主力"), expected_revision=2)
    assert _connection(relabelled, "mc-0001")["status"] == "verified"

    switched = service.update(
        "user-a", "mc-0001", _deepseek(api_key=None, model="deepseek-small"), expected_revision=3
    )
    connection = _connection(switched, "mc-0001")
    assert connection["status"] == "unverified"
    assert connection["keyHint"] == KEY_A[-4:]
    service.assign("user-a", {"section_navigation": "mc-0001"}, expected_revision=4)
    assert service.endpoint_for("user-a", ModelPurpose.SECTION_NAVIGATION).api_key == KEY_A


def test_stored_key_is_never_sent_to_a_new_destination(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _custom(), expected_revision=0)

    for draft in (
        _custom(api_key=None, base_url="https://attacker.example.com/v1"),
        _deepseek(api_key=None),
    ):
        with pytest.raises(ModelConnectionError) as raised:
            service.update("user-a", "mc-0001", draft, expected_revision=1)
        assert raised.value.code == "model_connection_key_required"
    with pytest.raises(ModelConnectionError) as discovery:
        service.discover(
            "user-a",
            provider_id="custom-openai-compatible",
            base_url="https://attacker.example.com/v1",
            connection_id="mc-0001",
        )
    assert discovery.value.code == "model_connection_key_required"


def test_update_that_no_longer_fits_an_assigned_purpose_is_refused(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _custom(), expected_revision=0)
    service.assign("user-a", {"candidate_generation": "mc-0001"}, expected_revision=1)

    with pytest.raises(ModelConnectionError) as raised:
        service.update("user-a", "mc-0001", _custom(api_key=None, context_window_tokens=64_000), expected_revision=2)

    assert raised.value.code == "model_connection_window_too_small"
    assert raised.value.details["purpose"] == "candidate_generation"


def test_delete_clears_its_assignments(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)
    service.create("user-a", _deepseek(api_key=KEY_B), expected_revision=1)
    service.assign("user-a", {"answer": "mc-0001", "evidence_summary": "mc-0002"}, expected_revision=2)

    view = service.delete("user-a", "mc-0001", expected_revision=3)

    assert [item["id"] for item in view["connections"]] == ["mc-0002"]
    assert view["assignments"]["answer"] is None
    assert view["assignments"]["evidence_summary"] == "mc-0002"
    with pytest.raises(ModelConnectionError) as raised:
        service.delete("user-a", "mc-0001", expected_revision=4)
    assert raised.value.code == "model_connection_not_found"


def test_accounts_cannot_reach_each_others_connections(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)

    for call in (
        lambda: service.update("user-b", "mc-0001", _deepseek(api_key=None), expected_revision=0),
        lambda: service.delete("user-b", "mc-0001", expected_revision=0),
        lambda: service.assign("user-b", {"answer": "mc-0001"}, expected_revision=0),
        lambda: service.verify("user-b", "mc-0001"),
    ):
        with pytest.raises(ModelConnectionError) as raised:
            call()
        assert raised.value.code == "model_connection_not_found"
    assert service.view("user-b")["connections"] == []


# -- assignment and window admission ---------------------------------------------


def test_assignment_admits_only_connections_whose_window_holds_the_purpose(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(model="deepseek-small"), expected_revision=0)

    view = service.view("user-a")
    fits = _connection(view, "mc-0001")["fits"]
    assert fits["section_navigation"] == {"fits": True, "requiredWindow": 9_500, "reason": None}
    assert fits["answer"]["fits"] is False
    assert fits["candidate_generation"]["reason"] == "model_connection_structured_output_required"

    with pytest.raises(ModelConnectionError) as small:
        service.assign("user-a", {"answer": "mc-0001"}, expected_revision=1)
    assert small.value.code == "model_connection_window_too_small"
    assert small.value.details == {
        "purpose": "answer",
        "purposeLabel": "回答",
        "requiredWindow": 32_900,
        "contextWindow": 16_000,
    }
    with pytest.raises(ModelConnectionError) as unstructured:
        service.assign("user-a", {"candidate_generation": "mc-0001"}, expected_revision=1)
    assert unstructured.value.code == "model_connection_structured_output_required"

    assigned = service.assign("user-a", {"section_navigation": "mc-0001"}, expected_revision=1)
    assert assigned["assignments"]["section_navigation"] == "mc-0001"
    cleared = service.assign("user-a", {"section_navigation": None}, expected_revision=2)
    assert cleared["assignments"]["section_navigation"] is None


def test_reasoning_reserves_output_in_the_required_window(tmp_path) -> None:
    service = _service(tmp_path)
    service.create(
        "user-a",
        _custom(context_window_tokens=40_000, max_output_tokens=None, reasoning_effort="high"),
        expected_revision=0,
    )

    fits = _connection(service.view("user-a"), "mc-0001")["fits"]["answer"]

    # 32k prompt + 900 visible + 16384 reserved for high reasoning.
    assert fits == {"fits": False, "requiredWindow": 49_284, "reason": "model_connection_window_too_small"}


def test_an_unchosen_tier_is_budgeted_at_the_models_declared_default(tmp_path) -> None:
    # deepseek-flash declares default: high. With no effort chosen, nothing is
    # sent and DeepSeek thinks at high, so the window must hold high's 16384.
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)
    service.assign("user-a", {"answer": "mc-0001"}, expected_revision=1)

    fits = _connection(service.view("user-a"), "mc-0001")["fits"]["answer"]
    endpoint = service.endpoint_for("user-a", ModelPurpose.ANSWER)

    assert fits["requiredWindow"] == 32_000 + 900 + 16_384
    assert endpoint.reasoning.effort is None
    assert endpoint.reasoning.effective_effort is ReasoningEffort.HIGH
    assert endpoint.reasoning.chat_fields() == {}


def test_preview_admits_a_draft_exactly_as_saving_would(tmp_path) -> None:
    service = _service(tmp_path)

    preview = service.preview(_deepseek(api_key=None))
    service.create("user-a", _deepseek(), expected_revision=0)
    saved = _connection(service.view("user-a"), "mc-0001")["fits"]

    assert preview["fits"] == saved
    assert preview["effectiveReasoningEffort"] == "high"
    assert preview["contextWindowTokens"] == 1_000_000
    assert preview["structuredOutput"] == "json_object"


def test_preview_of_a_custom_draft_reports_each_purpose_without_storing_or_calling(tmp_path) -> None:
    service = _service(tmp_path, transport=lambda request, timeout: (_ for _ in ()).throw(AssertionError("no call")))

    preview = service.preview(_custom(api_key=None, context_window_tokens=40_000, max_output_tokens=None, reasoning_effort="high"))

    assert preview["fits"]["answer"] == {"fits": False, "requiredWindow": 49_284, "reason": "model_connection_window_too_small"}
    assert preview["fits"]["section_navigation"]["fits"] is True
    assert preview["effectiveReasoningEffort"] == "high"
    assert not (tmp_path / "model-connections.json").exists()


@pytest.mark.parametrize(
    ("draft", "field"),
    (
        (_custom(api_key=None, context_window_tokens=None), "contextWindowTokens"),
        (_custom(api_key=None, base_url=None), "baseUrl"),
        (_deepseek(api_key=None, reasoning_effort="medium"), "reasoningEffort"),
    ),
)
def test_preview_reports_the_field_a_draft_still_needs(tmp_path, draft, field) -> None:
    with pytest.raises(ModelConnectionError) as raised:
        _service(tmp_path).preview(draft)

    assert raised.value.code == "model_connection_invalid"
    assert raised.value.details == {"field": field}


def test_unknown_purpose_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)

    with pytest.raises(ModelConnectionError) as raised:
        service.assign("user-a", {"translation": "mc-0001"}, expected_revision=1)

    assert raised.value.code == "model_connection_invalid"


# -- serving calls ----------------------------------------------------------------


def test_each_purpose_resolves_its_own_connection(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(reasoning_effort="max"), expected_revision=0)
    service.create(
        "user-a",
        ConnectionDraft(provider_id="anthropic", model="claude-sonnet-5-5", api_key=KEY_C, reasoning_effort="xhigh"),
        expected_revision=1,
    )
    service.assign(
        "user-a",
        {"answer": "mc-0002", "evidence_summary": "mc-0001", "candidate_generation": "mc-0001"},
        expected_revision=2,
    )
    resolve = service.endpoint_source("user-a")

    answer = resolve("answer")
    evidence = resolve("evidence_summary")

    assert answer.protocol is LLMWireProtocol.ANTHROPIC_MESSAGES
    assert (answer.base_url, answer.model, answer.api_key) == ("https://api.anthropic.com", "claude-sonnet-5-5", KEY_C)
    assert answer.reasoning.wire is ReasoningWire.ANTHROPIC_EFFORT
    assert answer.reasoning.effort is ReasoningEffort.XHIGH
    assert answer.context_window_tokens == 1_000_000
    assert answer.max_output_tokens == 128_000
    assert answer.connection_ref == "mc-0002@1"
    assert answer.structured_output_capability.mode == "json_object"
    assert (evidence.provider, evidence.api_key) == ("deepseek", KEY_A)
    assert evidence.reasoning.chat_fields() == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    with pytest.raises(LLMCredentialUnavailableError) as missing:
        resolve("section_navigation")
    assert (missing.value.code, missing.value.purpose) == ("model_connection_required", "section_navigation")


def test_custom_endpoint_declares_the_users_structured_output_mode(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _custom(), expected_revision=0)
    service.assign("user-a", {"candidate_generation": "mc-0001"}, expected_revision=1)

    endpoint = service.endpoint_for("user-a", ModelPurpose.CANDIDATE_GENERATION)

    assert endpoint.base_url == "https://gateway.example.com/v1"
    assert endpoint.structured_output_capability.provider == "custom-openai-compatible"
    assert endpoint.structured_output_capability.mode == "json_object"
    assert endpoint.reasoning.chat_fields() == {"reasoning_effort": "low"}


def test_catalog_withdrawal_surfaces_on_the_connection_and_its_calls(tmp_path) -> None:
    _service(tmp_path).create("user-a", _deepseek(), expected_revision=0)
    _service(tmp_path).assign("user-a", {"answer": "mc-0001"}, expected_revision=1)
    narrowed = json.loads(json.dumps(CATALOG))
    narrowed["providers"][0]["models"] = narrowed["providers"][0]["models"][1:]
    narrowed["providers"][0]["default_model"] = "deepseek-small"
    service = ModelConnectionService(
        catalog=parse_model_provider_catalog(narrowed),
        cipher=CredentialCipher(_master(1)),
        repository=LocalJsonModelConnectionRepository.under(tmp_path),
        requirements=REQUIREMENTS,
        probe_client_factory=_probe_factory(),
        outbound_policy=OutboundAddressPolicy(),
    )

    assert _connection(service.view("user-a"), "mc-0001")["status"] == "withdrawn"
    with pytest.raises(ModelConnectionError) as raised:
        service.endpoint_for("user-a", ModelPurpose.ANSWER)
    assert raised.value.code == "model_connection_withdrawn"


def test_concurrent_requests_of_two_accounts_each_use_their_own_connection(tmp_path) -> None:
    service = _service(tmp_path)
    service.create("user-a", _deepseek(), expected_revision=0)
    service.assign("user-a", {"answer": "mc-0001"}, expected_revision=1)
    service.create("user-b", _custom(), expected_revision=0)
    service.assign("user-b", {"answer": "mc-0002"}, expected_revision=1)
    barrier = Barrier(2)
    seen: dict[str, list[tuple[str, str]]] = {"user-a": [], "user-b": []}

    def transport(request, timeout):
        # Both calls are in flight at once; neither may see the other's key.
        barrier.wait(timeout=5)
        owner = "user-a" if request.get_header("Authorization") == f"Bearer {KEY_A}" else "user-b"
        seen[owner].append((request.full_url, request.get_header("Authorization")))
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

    shared_client = InvocationLLMClient(
        "answer",
        lambda endpoint: OpenAICompatibleClient.for_endpoint(endpoint, timeout_seconds=10, transport=transport),
    )

    def call(user_id: str) -> None:
        with bind_llm_endpoint_source(service.endpoint_source(user_id)):
            shared_client.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=4))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, ["user-a", "user-b"]))

    assert seen["user-a"] == [("https://api.deepseek.com/chat/completions", f"Bearer {KEY_A}")]
    assert seen["user-b"] == [("https://gateway.example.com/v1/chat/completions", f"Bearer {KEY_B}")]


# -- verification and discovery ----------------------------------------------------


def test_verify_reads_the_model_list_once(tmp_path) -> None:
    calls = []

    def transport(request, timeout):
        calls.append((request.get_method(), request.full_url, request.get_header("Authorization"), request.data))
        return _model_list("deepseek-flash", "deepseek-v4-pro")

    service = _service(tmp_path, transport=transport)
    service.create("user-a", _deepseek(), expected_revision=0)

    view = service.verify("user-a", "mc-0001")

    assert _connection(view, "mc-0001")["status"] == "verified"
    assert _connection(view, "mc-0001")["verifiedAt"] is not None
    assert calls == [("GET", "https://api.deepseek.com/models", f"Bearer {KEY_A}", None)]
    # Verification does not change what calls send; cached clients stay valid.
    assert _connection(view, "mc-0001")["revision"] == 1


def test_verify_an_anthropic_connection_lists_its_models(tmp_path) -> None:
    calls: list[str] = []
    service = _service(tmp_path, probe=_probe_factory(calls=calls))
    service.create(
        "user-a", ConnectionDraft(provider_id="anthropic", model="claude-sonnet-5-5", api_key=KEY_C), expected_revision=0
    )

    view = service.verify("user-a", "mc-0001")

    assert _connection(view, "mc-0001")["status"] == "verified"
    assert calls == ["anthropic:list"]


@pytest.mark.parametrize(
    ("transport", "code", "status"),
    (
        (lambda request, timeout: _model_list("other-model"), "model_connection_model_unavailable", "unverified"),
        (_http_error(401), "model_connection_rejected", "unverified"),
        (_http_error(503), "model_provider_unreachable", "verified"),
        (lambda request, timeout: b"not json", "model_provider_unreachable", "verified"),
    ),
)
def test_verify_failures(tmp_path, transport, code, status) -> None:
    state = {"transport": lambda request, timeout: _model_list("deepseek-flash")}
    service = _service(tmp_path, transport=lambda request, timeout: state["transport"](request, timeout))
    service.create("user-a", _deepseek(), expected_revision=0)
    service.verify("user-a", "mc-0001")
    calls = []

    def counted(request, timeout):
        calls.append(request.full_url)
        return transport(request, timeout)

    state["transport"] = counted

    with pytest.raises(ModelConnectionError) as raised:
        service.verify("user-a", "mc-0001")

    assert raised.value.code == code
    assert len(calls) == 1
    # Only an answer about the key or the model clears an earlier result.
    assert _connection(service.view("user-a"), "mc-0001")["status"] == status


def test_verify_a_custom_address_without_a_model_list_sends_one_tiny_completion(tmp_path) -> None:
    calls = []

    def transport(request, timeout):
        calls.append((request.get_method(), request.full_url, json.loads(request.data) if request.data else None))
        if request.full_url.endswith("/models"):
            raise HTTPError(request.full_url, 404, "missing", {}, BytesIO(b"{}"))
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

    service = _service(tmp_path, transport=transport)
    service.create("user-a", _custom(), expected_revision=0)

    view = service.verify("user-a", "mc-0001")

    assert _connection(view, "mc-0001")["status"] == "verified"
    assert [item[:2] for item in calls] == [
        ("GET", "https://gateway.example.com/v1/models"),
        ("POST", "https://gateway.example.com/v1/chat/completions"),
    ]
    probe = calls[1][2]
    assert probe["model"] == "qwen3-max"
    assert probe["reasoning_effort"] == "none"
    assert probe["max_completion_tokens"] == 16


def test_verify_reports_a_refused_address_without_echoing_it(tmp_path) -> None:
    from infrastructure.security.pinned_https import PinnedUrllibTransport

    outbound = OutboundAddressPolicy(resolver=lambda _host, _port: ["10.0.0.7"])
    service = _service(tmp_path, outbound=outbound, transport=PinnedUrllibTransport.for_policy(outbound))
    service.create("user-a", _custom(), expected_revision=0)

    with pytest.raises(ModelConnectionError) as raised:
        service.verify("user-a", "mc-0001")

    assert raised.value.code == "model_endpoint_not_allowed"
    assert "gateway.example.com" not in str(raised.value)


def test_discover_lists_models_with_a_draft_key_that_is_not_stored(tmp_path) -> None:
    calls = []

    def transport(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization")))
        return _model_list("qwen3-max", "qwen3-coder", "qwen3-max")

    service = _service(tmp_path, transport=transport)

    models = service.discover(
        "user-a", provider_id="custom-openai-compatible", base_url="https://gateway.example.com/v1/", api_key=KEY_B
    )

    assert models == ("qwen3-coder", "qwen3-max")
    assert calls == [("https://gateway.example.com/v1/models", f"Bearer {KEY_B}")]
    assert not (tmp_path / "model-connections.json").exists()
    assert service.discover("user-a", provider_id="deepseek") == ("deepseek-flash", "deepseek-small")


def test_discover_with_the_stored_key_of_the_same_address(tmp_path) -> None:
    seen = []
    service = _service(
        tmp_path,
        transport=lambda request, timeout: seen.append(request.get_header("Authorization")) or _model_list("qwen3-max"),
    )
    service.create("user-a", _custom(), expected_revision=0)

    models = service.discover(
        "user-a",
        provider_id="custom-openai-compatible",
        base_url="https://gateway.example.com/v1",
        connection_id="mc-0001",
    )

    assert models == ("qwen3-max",)
    assert seen == [f"Bearer {KEY_B}"]


def test_discover_on_an_address_without_a_listing_asks_for_a_manual_model(tmp_path) -> None:
    service = _service(tmp_path, transport=_http_error(404))

    with pytest.raises(ModelConnectionError) as raised:
        service.discover(
            "user-a", provider_id="custom-openai-compatible", base_url="https://gateway.example.com/v1", api_key=KEY_B
        )

    assert raised.value.code == "model_discovery_unsupported"


# -- migration of the single per-account key -----------------------------------------------


def _single_key_file(tmp_path, cipher: CredentialCipher, *records: tuple[str, str, str, str]):
    credentials = []
    for user_id, provider_id, model, secret in records:
        sealed = cipher.seal(secret, single_key_credential_binding(user_id, provider_id))
        credentials.append(
            {
                "userId": user_id,
                "providerId": provider_id,
                "model": model,
                "keyId": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
                "keyHint": secret[-4:],
                "revision": 3,
                "updatedAt": "2026-10-20T08:00:00Z",
                "verifiedAt": "2026-10-21T08:00:00Z",
            }
        )
    path = tmp_path / "model-credentials.json"
    path.write_text(
        json.dumps({"schemaVersion": "agora_model_credentials.v1", "credentials": credentials}), encoding="utf-8"
    )
    return path


def test_single_key_becomes_one_connection_serving_every_purpose(tmp_path) -> None:
    cipher = CredentialCipher(_master(1))
    single_key = _single_key_file(tmp_path, cipher, ("user-a", "deepseek", "deepseek-flash", KEY_A))
    repository = LocalJsonModelConnectionRepository.under(tmp_path)

    moved = migrate_single_key_credentials(
        single_key_path=single_key,
        repository=repository,
        cipher=cipher,
        catalog=parse_model_provider_catalog(CATALOG),
        id_factory=lambda: "mc-migrated",
    )

    assert moved == 1
    assert not single_key.exists()
    assert (tmp_path / "model-credentials.json.migrated").exists()
    service = _service(tmp_path)
    view = service.view("user-a")
    connection = _connection(view, "mc-migrated")
    assert connection["status"] == "verified"
    assert connection["label"] == "DeepSeek 官方 · deepseek-flash"
    assert view["assignments"] == {purpose.value: "mc-migrated" for purpose in ModelPurpose}
    # Re-sealed under the connection binding, not left under the single-key one.
    assert service.endpoint_for("user-a", ModelPurpose.ANSWER).api_key == KEY_A
    assert KEY_A not in (tmp_path / "model-connections.json").read_text(encoding="utf-8")


def test_single_key_sealed_under_an_old_master_key_asks_for_the_key_again(tmp_path) -> None:
    single_key = _single_key_file(tmp_path, CredentialCipher(_master(9)), ("user-a", "deepseek", "deepseek-flash", KEY_A))
    cipher = CredentialCipher(_master(1))

    migrate_single_key_credentials(
        single_key_path=single_key,
        repository=LocalJsonModelConnectionRepository.under(tmp_path),
        cipher=cipher,
        catalog=parse_model_provider_catalog(CATALOG),
        id_factory=lambda: "mc-migrated",
    )

    service = _service(tmp_path)
    connection = _connection(service.view("user-a"), "mc-migrated")
    assert connection["status"] == "key_missing"
    assert connection["keyHint"] is None
    assert connection["verifiedAt"] is None
    with pytest.raises(ModelConnectionError) as raised:
        service.endpoint_for("user-a", ModelPurpose.ANSWER)
    assert raised.value.code == "model_connection_key_missing"
    with pytest.raises(ModelConnectionError) as keep:
        service.update("user-a", "mc-migrated", _deepseek(api_key=None), expected_revision=1)
    assert keep.value.code == "model_connection_key_missing"
    service.update("user-a", "mc-migrated", _deepseek(api_key=KEY_B), expected_revision=1)
    assert service.endpoint_for("user-a", ModelPurpose.ANSWER).api_key == KEY_B


def test_migration_never_overwrites_an_account_that_already_has_connections(tmp_path) -> None:
    cipher = CredentialCipher(_master(1))
    _service(tmp_path).create("user-a", _deepseek(api_key=KEY_B), expected_revision=0)
    single_key = _single_key_file(
        tmp_path,
        cipher,
        ("user-a", "deepseek", "deepseek-flash", KEY_A),
        ("user-b", "deepseek", "deepseek-flash", KEY_A),
    )

    moved = migrate_single_key_credentials(
        single_key_path=single_key,
        repository=LocalJsonModelConnectionRepository.under(tmp_path),
        cipher=cipher,
        catalog=parse_model_provider_catalog(CATALOG),
        id_factory=lambda: "mc-migrated",
    )

    assert moved == 1
    service = _service(tmp_path)
    assert [item["id"] for item in service.view("user-a")["connections"]] == ["mc-0001"]
    assert [item["id"] for item in service.view("user-b")["connections"]] == ["mc-migrated"]


def test_migration_without_a_single_key_file_does_nothing(tmp_path) -> None:
    moved = migrate_single_key_credentials(
        single_key_path=tmp_path / "model-credentials.json",
        repository=LocalJsonModelConnectionRepository.under(tmp_path),
        cipher=CredentialCipher(_master(1)),
        catalog=parse_model_provider_catalog(CATALOG),
    )

    assert moved == 0
    assert not (tmp_path / "model-connections.json").exists()


def test_malformed_single_key_file_stops_startup(tmp_path) -> None:
    single_key = tmp_path / "model-credentials.json"
    single_key.write_text(json.dumps({"credentials": [{"userId": "user-a"}]}), encoding="utf-8")

    with pytest.raises(ModelConnectionConfigurationError):
        migrate_single_key_credentials(
            single_key_path=single_key,
            repository=LocalJsonModelConnectionRepository.under(tmp_path),
            cipher=CredentialCipher(_master(1)),
            catalog=parse_model_provider_catalog(CATALOG),
        )
    assert single_key.exists()


# -- catalog ---------------------------------------------------------------------------


def _catalog_with(**provider_overrides):
    catalog = json.loads(json.dumps(CATALOG))
    catalog["providers"][0].update(provider_overrides)
    return catalog


@pytest.mark.parametrize(
    "catalog",
    (
        {**CATALOG, "schema_version": "hosted-llm-providers.v1"},
        _catalog_with(base_url="http://api.deepseek.com"),
        _catalog_with(protocol="grpc"),
        _catalog_with(default_model="missing"),
        _catalog_with(custom=True),
        _catalog_with(
            models=[{**CATALOG["providers"][0]["models"][0], "reasoning": {"parameter": "anthropic_effort", "efforts": ["low"]}}]
        ),
        _catalog_with(
            models=[{**CATALOG["providers"][0]["models"][0], "reasoning": {"parameter": "dashscope_enable_thinking", "efforts": ["high"]}}]
        ),
        _catalog_with(
            models=[{**CATALOG["providers"][0]["models"][0], "reasoning": {"parameter": "deepseek_thinking", "efforts": ["low"], "default": "max"}}]
        ),
        _catalog_with(models=[{key: value for key, value in CATALOG["providers"][0]["models"][0].items() if key != "reasoning"}]),
        _catalog_with(models=[{**CATALOG["providers"][0]["models"][0], "max_output_tokens": 1_000_000}]),
        _catalog_with(
            models=[{**CATALOG["providers"][0]["models"][0], "structured_output": {"mode": "native_strict", "revision": "r", "source": "s"}}]
        ),
    ),
)
def test_catalog_refuses_unreviewed_or_inconsistent_entries(catalog) -> None:
    with pytest.raises(ModelConnectionConfigurationError):
        parse_model_provider_catalog(catalog)


def test_custom_provider_takes_no_operator_address() -> None:
    catalog = json.loads(json.dumps(CATALOG))
    catalog["providers"][2]["base_url"] = "https://gateway.example.com"

    with pytest.raises(ModelConnectionConfigurationError):
        parse_model_provider_catalog(catalog)


def test_master_key_must_be_32_bytes() -> None:
    import base64

    with pytest.raises(ModelConnectionConfigurationError):
        CredentialCipher.from_env({"NEWSROOM_CREDENTIAL_ENCRYPTION_KEY": base64.b64encode(b"x" * 16).decode()})
    with pytest.raises(ModelConnectionConfigurationError):
        CredentialCipher.from_env({})
