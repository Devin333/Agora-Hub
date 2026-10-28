from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from urllib.error import HTTPError

import pytest

from framework.llm import LLMRequest
from framework.llm.clients import (
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    bind_llm_endpoint_source,
)
from interfaces.services.model_credentials import (
    CredentialCipher,
    LocalJsonModelCredentialRepository,
    ModelCredentialConfigurationError,
    ModelCredentialError,
    ModelCredentialService,
    load_model_provider_catalog,
    parse_model_provider_catalog,
)
from tests.interfaces.api.hosted_support import REPO_ROOT

CATALOG_PATH = REPO_ROOT / "configs" / "hosted-llm-providers.yaml"
KEY_A = "sk-account-a-0001"
KEY_B = "sk-account-b-0002"


def _master(seed: int) -> bytes:
    return bytes((seed + index) % 256 for index in range(32))


def _service(tmp_path, *, master: bytes | None = None, transport=None) -> ModelCredentialService:
    return ModelCredentialService(
        catalog=load_model_provider_catalog(CATALOG_PATH),
        cipher=CredentialCipher(master or _master(1)),
        repository=LocalJsonModelCredentialRepository.under(tmp_path),
        verification_transport=transport,
    )


def test_stored_key_never_leaves_the_service_and_file_holds_only_ciphertext(tmp_path) -> None:
    service = _service(tmp_path)

    view = service.save(
        "user-a", provider_id="deepseek", model="deepseek-v4-pro", api_key=KEY_A, expected_revision=None
    )

    payload = view.to_dict()
    assert payload["status"] == "unverified"
    assert payload["keyHint"] == KEY_A[-4:]
    assert KEY_A not in json.dumps(payload)
    stored = (tmp_path / "model-credentials.json").read_text(encoding="utf-8")
    assert KEY_A not in stored
    endpoint = service.endpoint_for("user-a")
    assert endpoint.api_key == KEY_A
    assert endpoint.base_url == "https://api.deepseek.com"
    assert KEY_A not in repr(endpoint)


def test_catalog_rejects_unknown_provider_or_model(tmp_path) -> None:
    service = _service(tmp_path)

    for provider, model in (("openai", "gpt-x"), ("deepseek", "deepseek-v1")):
        with pytest.raises(ModelCredentialError) as raised:
            service.save("user-a", provider_id=provider, model=model, api_key=KEY_A, expected_revision=None)
        assert raised.value.code == "model_provider_unsupported"


def test_stale_revision_is_a_conflict_and_model_switch_keeps_key(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save(
        "user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=None
    )

    with pytest.raises(ModelCredentialError) as conflict:
        service.save(
            "user-a", provider_id="deepseek", model="deepseek-v4-pro", api_key=None, expected_revision=None
        )
    assert conflict.value.code == "model_credential_conflict"

    switched = service.save(
        "user-a",
        provider_id="deepseek",
        model="deepseek-v4-pro",
        api_key=None,
        expected_revision=first.revision,
    )
    assert switched.model == "deepseek-v4-pro"
    assert switched.revision == first.revision + 1
    assert service.endpoint_for("user-a").api_key == KEY_A


def test_ciphertext_moved_to_another_account_does_not_open(tmp_path) -> None:
    service = _service(tmp_path)
    service.save("user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=None)
    service.save("user-b", provider_id="deepseek", model="deepseek-flash", api_key=KEY_B, expected_revision=None)
    path = tmp_path / "model-credentials.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    records = {item["userId"]: item for item in document["credentials"]}
    for field in ("keyId", "nonce", "ciphertext"):
        records["user-b"][field] = records["user-a"][field]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ModelCredentialError) as raised:
        service.endpoint_for("user-b")
    assert raised.value.code == "model_credential_required"
    assert service.endpoint_for("user-a").api_key == KEY_A


def test_master_key_change_reports_rotation_and_requires_new_key(tmp_path) -> None:
    _service(tmp_path, master=_master(1)).save(
        "user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=None
    )
    rotated = _service(tmp_path, master=_master(2))

    view = rotated.view("user-a")
    assert view.status == "key_rotated"
    assert view.key_hint is None
    with pytest.raises(ModelCredentialError) as raised:
        rotated.endpoint_for("user-a")
    assert raised.value.code == "model_credential_key_rotated"

    renewed = rotated.save(
        "user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=view.revision
    )
    assert renewed.status == "unverified"
    assert rotated.endpoint_for("user-a").api_key == KEY_A


def test_missing_credential_fails_closed_through_the_framework_error(tmp_path) -> None:
    resolve = _service(tmp_path).endpoint_source("user-without-key")

    with pytest.raises(LLMCredentialUnavailableError) as raised:
        resolve()
    assert raised.value.code == "model_credential_required"


def test_verify_sends_exactly_one_minimal_request_and_records_result(tmp_path) -> None:
    calls = []

    def transport(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)))
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "p"}, "finish_reason": "length"}]}
        ).encode("utf-8")

    service = _service(tmp_path, transport=transport)
    service.save("user-a", provider_id="dashscope", model="deepseek-v4.1-flash", api_key=KEY_A, expected_revision=None)

    view = service.verify("user-a")

    assert view.status == "verified"
    assert len(calls) == 1
    url, authorization, body = calls[0]
    assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert authorization == f"Bearer {KEY_A}"
    assert body["max_tokens"] == 1
    assert body["model"] == "deepseek-v4.1-flash"


@pytest.mark.parametrize(("status", "code"), ((401, "model_credential_rejected"), (503, "model_provider_unreachable")))
def test_verify_failure_is_not_retried(tmp_path, status: int, code: str) -> None:
    calls = []

    def transport(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, status, "failed", {}, BytesIO(b"{}"))

    service = _service(tmp_path, transport=transport)
    service.save("user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=None)

    with pytest.raises(ModelCredentialError) as raised:
        service.verify("user-a")

    assert raised.value.code == code
    assert len(calls) == 1
    assert service.view("user-a").status == "unverified"


def test_concurrent_requests_of_two_accounts_each_use_their_own_key(tmp_path) -> None:
    service = _service(tmp_path)
    service.save("user-a", provider_id="deepseek", model="deepseek-flash", api_key=KEY_A, expected_revision=None)
    service.save("user-b", provider_id="dashscope", model="deepseek-v4.1-flash", api_key=KEY_B, expected_revision=None)
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

    shared_client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            provider="hosted",
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
            api_key_env="NEWSROOM_TEST_KEY_THAT_MUST_NOT_BE_READ",
        ),
        transport=transport,
        credential_mode=LLMCredentialMode.INVOCATION,
    )

    def call(user_id: str) -> None:
        with bind_llm_endpoint_source(service.endpoint_source(user_id)):
            shared_client.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=4))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, ["user-a", "user-b"]))

    assert seen["user-a"] == [("https://api.deepseek.com/chat/completions", f"Bearer {KEY_A}")]
    assert seen["user-b"] == [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", f"Bearer {KEY_B}")
    ]


def test_master_key_must_be_32_bytes(tmp_path) -> None:
    short = base64.b64encode(b"x" * 16).decode("ascii")

    with pytest.raises(ModelCredentialConfigurationError):
        CredentialCipher.from_env({"NEWSROOM_CREDENTIAL_ENCRYPTION_KEY": short})
    with pytest.raises(ModelCredentialConfigurationError):
        CredentialCipher.from_env({})


def test_catalog_requires_https_and_enough_context() -> None:
    insecure = {
        "schema_version": "hosted-llm-providers.v1",
        "providers": [
            {
                "id": "p",
                "label": "P",
                "base_url": "http://example.com/v1",
                "default_model": "m",
                "models": [{"id": "m", "label": "M", "context_window_tokens": 1000}],
            }
        ],
    }
    with pytest.raises(ModelCredentialConfigurationError):
        parse_model_provider_catalog(insecure)

    catalog = load_model_provider_catalog(CATALOG_PATH)
    catalog.require_context_window(64_000)
    with pytest.raises(ModelCredentialConfigurationError):
        catalog.require_context_window(2_000_000)
