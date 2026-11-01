from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from framework.llm.clients import LLMCredentialUnavailableError, resolve_invocation_endpoint
from interfaces.api import create_app
from interfaces.composition.hosted import build_hosted_runtime
from interfaces.services.deployment_profile import DeploymentConfigurationError
from interfaces.services.hosted_boundary import HostedRouteAccess, classify_hosted_route, is_model_probe_route
from interfaces.services.model_connections import CredentialCipher, ModelPurpose
from interfaces.services.model_connections.cipher import single_key_credential_binding
from tests.interfaces.api.hosted_support import (
    HOSTED_ORIGIN,
    TEST_ENCRYPTION_KEY,
    build_test_hosted_runtime,
    configure_hosted_env,
    hosted_env,
    mutation_headers,
    sign_in,
)

ADMIN_PASSWORD = "admin-password-0001"
USER_PASSWORD = "user-password-0002"
KEY_A = "sk-hosted-user-key-a"


class _EndpointProbeDesktopService:
    """Reports what the hosted boundary bound for the calling account."""

    def identity(self, owner):
        try:
            endpoint = resolve_invocation_endpoint("answer")
        except LLMCredentialUnavailableError as exc:
            bound = {"error": exc.code}
        else:
            bound = {"keyTail": endpoint.api_key[-4:], "model": endpoint.model}
        return {"owner": {"userId": owner.user_id}, "endpoint": bound}


def _client(monkeypatch, tmp_path, **env_overrides: str):
    configure_hosted_env(monkeypatch, tmp_path, **env_overrides)
    runtime = build_test_hosted_runtime(tmp_path, **env_overrides)
    app = create_app(
        hosted_runtime=runtime,
        research_desktop_service_factory=_EndpointProbeDesktopService,
        audit_emitter_factory=None,
    )
    return TestClient(app), runtime


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/v1/reports"),
        ("GET", "/api/v2/graph-runs"),
        ("GET", "/api/v1/storage/metrics"),
        ("GET", "/api/v2/graph-runs/run-1/artifacts/output"),
        ("GET", "/api/v1/memory/private-document"),
        ("GET", "/api/v1/events/projections/runs/run-1/status"),
        ("POST", "/api/v1/mcp/tools/news.research.analyze_paper/call"),
        ("POST", "/api/v1/mcp/resources/read"),
        ("GET", "/api/v1/admin/diagnose"),
        ("GET", "/health/dependencies"),
        ("POST", "/api/v1/auth/bootstrap"),
        ("POST", "/api/v1/auth/otp/request"),
        ("POST", "/api/v1/research/papers/analyze"),
        ("POST", "/api/v1/research/catalog/refresh"),
        ("GET", "/api/v1/research/../reports"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ),
)
def test_routes_outside_the_allow_list_are_absent(monkeypatch, tmp_path, method, path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    headers = mutation_headers(sign_in(runtime, "admin", ADMIN_PASSWORD, role="admin"))

    response = client.request(method, path, headers={**headers, "Authorization": "Bearer anything"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "hosted_route_unavailable"


def test_classification_keeps_research_writes_to_durable_operations() -> None:
    assert classify_hosted_route("POST", "/api/v1/research/desktop/operations/analyze") is HostedRouteAccess.RESEARCH
    assert classify_hosted_route("GET", "/api/v1/research/papers/p-1/document") is HostedRouteAccess.RESEARCH
    assert classify_hosted_route("POST", "/api/v1/research/papers/p-1/ask") is HostedRouteAccess.UNAVAILABLE
    assert classify_hosted_route("GET", "/api/v1/admin/accounts") is HostedRouteAccess.ADMIN
    assert classify_hosted_route("PATCH", "/api/v1/account/model-connections/mc-1") is HostedRouteAccess.ACCOUNT
    assert classify_hosted_route("PUT", "/api/v1/account/model-connections/assignments") is HostedRouteAccess.ACCOUNT
    assert classify_hosted_route("GET", "/api/v1/research//history") is HostedRouteAccess.UNAVAILABLE


def test_research_requires_a_session_and_ignores_forged_identity_headers(monkeypatch, tmp_path) -> None:
    client, _runtime = _client(monkeypatch, tmp_path)

    for headers in (
        {},
        {"X-News-Actor-Id": "admin", "X-News-Roles": "admin"},
        {"Authorization": "Bearer shared-operator-token"},
        {"X-Newsroom-Session": "not-a-session"},
    ):
        for path in ("/api/v1/research/desktop/identity", "/api/v1/research/runs/run-1/trace"):
            response = client.get(path, headers=headers)
            assert response.status_code == 401, (path, headers)
            assert response.json()["error"]["code"] == "auth_required"


def test_mutations_from_another_origin_are_refused(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    headers = sign_in(runtime, "alice", USER_PASSWORD)

    for origin in (None, "https://evil.example", "http://127.0.0.1:3001"):
        extra = {} if origin is None else {"Origin": origin}
        response = client.post(
            "/api/v1/account/model-connections",
            headers={**headers, **extra},
            json={"providerId": "deepseek", "model": "deepseek-flash", "apiKey": KEY_A, "expectedRevision": 0},
        )
        assert response.status_code == 403, origin
        assert response.json()["error"]["code"] == "origin_forbidden"


def test_administration_requires_the_admin_role(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    user = sign_in(runtime, "alice", USER_PASSWORD)
    admin = sign_in(runtime, "root", ADMIN_PASSWORD, role="admin")

    refused = client.get("/api/v1/admin/accounts", headers=user)
    allowed = client.get("/api/v1/admin/accounts", headers=admin)

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "admin_required"
    assert allowed.status_code == 200
    assert sorted(item["username"] for item in allowed.json()["data"]["accounts"]) == ["alice", "root"]


def test_each_request_is_bound_to_the_signed_in_accounts_connections(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    alice = sign_in(runtime, "alice", USER_PASSWORD)
    bob = sign_in(runtime, "bob", USER_PASSWORD)

    created = client.post(
        "/api/v1/account/model-connections",
        headers=mutation_headers(alice),
        json={"providerId": "deepseek", "model": "deepseek-v4-pro", "apiKey": KEY_A, "expectedRevision": 0},
    )
    assert created.status_code == 200
    view = created.json()["data"]
    connection = view["connections"][0]
    assert connection["status"] == "unverified"
    assert connection["keyHint"] == KEY_A[-4:]
    assert KEY_A not in created.text

    unassigned = client.get("/api/v1/research/desktop/identity", headers=alice).json()["data"]
    assert unassigned["endpoint"] == {"error": "model_connection_required"}

    assigned = client.put(
        "/api/v1/account/model-connections/assignments",
        headers=mutation_headers(alice),
        json={"assignments": {"answer": connection["id"]}, "expectedRevision": view["revision"]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["data"]["assignments"]["answer"] == connection["id"]

    alice_identity = client.get("/api/v1/research/desktop/identity", headers=alice).json()["data"]
    bob_identity = client.get("/api/v1/research/desktop/identity", headers=bob).json()["data"]

    assert alice_identity["endpoint"] == {"keyTail": KEY_A[-4:], "model": "deepseek-v4-pro"}
    assert bob_identity["endpoint"] == {"error": "model_connection_required"}
    assert alice_identity["owner"]["userId"] != bob_identity["owner"]["userId"]


def test_model_connection_routes_report_errors_with_their_details(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    alice = sign_in(runtime, "alice", USER_PASSWORD)
    created = client.post(
        "/api/v1/account/model-connections",
        headers=mutation_headers(alice),
        json={"providerId": "deepseek", "model": "deepseek-flash", "apiKey": KEY_A, "expectedRevision": 0},
    ).json()["data"]
    connection_id = created["connections"][0]["id"]

    stale = client.patch(
        f"/api/v1/account/model-connections/{connection_id}",
        headers=mutation_headers(alice),
        json={"providerId": "deepseek", "model": "deepseek-v4-pro", "expectedRevision": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "model_connection_conflict"

    custom_too_small = client.post(
        "/api/v1/account/model-connections",
        headers=mutation_headers(alice),
        json={
            "providerId": "custom-openai-compatible",
            "model": "gateway-model",
            "apiKey": KEY_A,
            "baseUrl": "https://gateway.example.com/v1",
            "contextWindowTokens": 128_000,
            "expectedRevision": created["revision"],
        },
    ).json()["data"]
    small_id = next(item["id"] for item in custom_too_small["connections"] if item["custom"])
    refused = client.put(
        "/api/v1/account/model-connections/assignments",
        headers=mutation_headers(alice),
        json={"assignments": {"answer": small_id}, "expectedRevision": custom_too_small["revision"]},
    )
    assert refused.status_code == 400
    error = refused.json()["error"]
    assert error["code"] == "model_connection_window_too_small"
    assert error["details"]["purpose"] == "answer"
    assert error["details"]["contextWindow"] == 128_000

    deleted = client.delete(
        f"/api/v1/account/model-connections/{connection_id}",
        headers=mutation_headers(alice),
        params={"expectedRevision": custom_too_small["revision"]},
    )
    assert deleted.status_code == 200
    assert [item["id"] for item in deleted.json()["data"]["connections"]] == [small_id]
    assert KEY_A not in deleted.text


def test_sign_in_is_rate_limited_per_client_address(monkeypatch, tmp_path) -> None:
    client, runtime = _client(
        monkeypatch, tmp_path, NEWSROOM_HOSTED_LOGIN_RATE_LIMIT_PER_MINUTE="2"
    )
    runtime.accounts.create_account(username="alice", role="user", password=USER_PASSWORD)
    attempt = {"username": "alice", "password": "wrong-password"}
    headers = {"Origin": HOSTED_ORIGIN}

    statuses = [client.post("/api/v1/auth/login", headers=headers, json=attempt).status_code for _ in range(3)]

    assert statuses[:2] == [401, 401]
    assert statuses[2] == 429


def test_research_writes_are_rate_limited_per_account(monkeypatch, tmp_path) -> None:
    client, runtime = _client(
        monkeypatch, tmp_path, NEWSROOM_HOSTED_RESEARCH_RATE_LIMIT_PER_MINUTE="2"
    )
    alice = mutation_headers(sign_in(runtime, "alice", USER_PASSWORD))
    bob = mutation_headers(sign_in(runtime, "bob", USER_PASSWORD))
    path = "/api/v1/research/desktop/operations/analyze"

    alice_statuses = [client.post(path, headers=alice, json={}).status_code for _ in range(3)]
    bob_status = client.post(path, headers=bob, json={}).status_code
    # Reads never spend the write budget.
    alice_read = client.get("/api/v1/research/desktop/identity", headers=alice)

    assert all(status != 429 for status in alice_statuses[:2])
    assert alice_statuses[2] == 429
    assert bob_status != 429
    assert alice_read.status_code == 200


def test_model_probes_are_rate_limited_per_account(monkeypatch, tmp_path) -> None:
    client, runtime = _client(
        monkeypatch, tmp_path, NEWSROOM_HOSTED_MODEL_PROBE_RATE_LIMIT_PER_MINUTE="2"
    )
    alice = mutation_headers(sign_in(runtime, "alice", USER_PASSWORD))
    bob = mutation_headers(sign_in(runtime, "bob", USER_PASSWORD))
    # Discovery for a catalog provider answers from the catalog, so the
    # limiter is exercised without reaching any provider.
    path = "/api/v1/account/model-connections/discover"
    body = {"providerId": "deepseek"}

    alice_statuses = [client.post(path, headers=alice, json=body).status_code for _ in range(3)]
    bob_status = client.post(path, headers=bob, json=body).status_code
    # Saving settings is not a probe and spends no probe budget.
    saved = client.post(
        "/api/v1/account/model-connections",
        headers=alice,
        json={"providerId": "deepseek", "model": "deepseek-flash", "apiKey": KEY_A, "expectedRevision": 0},
    )

    assert alice_statuses == [200, 200, 429]
    assert bob_status == 200
    assert saved.status_code == 200


def test_preview_never_takes_a_key_and_spends_no_probe_budget(monkeypatch, tmp_path) -> None:
    client, runtime = _client(
        monkeypatch, tmp_path, NEWSROOM_HOSTED_MODEL_PROBE_RATE_LIMIT_PER_MINUTE="1"
    )
    alice = mutation_headers(sign_in(runtime, "alice", USER_PASSWORD))
    path = "/api/v1/account/model-connections/preview"
    draft = {"providerId": "deepseek", "model": "deepseek-flash"}

    previews = [client.post(path, headers=alice, json=draft) for _ in range(3)]
    with_key = client.post(path, headers=alice, json={**draft, "apiKey": KEY_A})

    assert [response.status_code for response in previews] == [200, 200, 200]
    data = previews[0].json()["data"]
    assert data["effectiveReasoningEffort"] == "high"
    assert data["fits"]["answer"]["fits"] is True
    assert set(data["fits"]) == {purpose.value for purpose in ModelPurpose}
    # The form sends settings while typing; a key in the body is refused, not ignored.
    assert with_key.status_code == 422
    assert KEY_A not in with_key.text
    assert not is_model_probe_route("POST", path)


def test_probe_routes_are_classified_exactly() -> None:
    assert is_model_probe_route("POST", "/api/v1/account/model-connections/discover")
    assert is_model_probe_route("POST", "/api/v1/account/model-connections/mc-0123456789abcdef/verify")
    assert not is_model_probe_route("GET", "/api/v1/account/model-connections/discover")
    assert not is_model_probe_route("POST", "/api/v1/account/model-connections")
    assert not is_model_probe_route("PUT", "/api/v1/account/model-connections/assignments")


def test_forwarded_address_is_trusted_only_from_configured_proxies(monkeypatch, tmp_path) -> None:
    client, runtime = _client(
        monkeypatch,
        tmp_path,
        NEWSROOM_HOSTED_LOGIN_RATE_LIMIT_PER_MINUTE="1",
        NEWSROOM_TRUSTED_PROXY_CIDRS="10.0.0.0/8",
    )
    runtime.accounts.create_account(username="alice", role="user", password=USER_PASSWORD)
    attempt = {"username": "alice", "password": "wrong-password"}

    # The test client's peer is not a trusted proxy, so rotating the header
    # cannot buy a fresh budget.
    first = client.post(
        "/api/v1/auth/login",
        headers={"Origin": HOSTED_ORIGIN, "X-Forwarded-For": "203.0.113.1"},
        json=attempt,
    )
    second = client.post(
        "/api/v1/auth/login",
        headers={"Origin": HOSTED_ORIGIN, "X-Forwarded-For": "203.0.113.2"},
        json=attempt,
    )

    assert first.status_code == 401
    assert second.status_code == 429


def test_session_status_has_no_bootstrap_or_desktop_identity(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    # Until the server CLI creates an administrator the service is not
    # initialized, and the browser still cannot bootstrap it.
    assert client.get("/api/v1/auth/session").json()["data"] == {
        "initialized": False,
        "bootstrapAvailable": False,
        "session": None,
    }
    sign_in(runtime, "root", ADMIN_PASSWORD, role="admin")
    alice = sign_in(runtime, "alice", USER_PASSWORD)

    anonymous = client.get("/api/v1/auth/session").json()["data"]
    signed = client.get("/api/v1/auth/session", headers=alice).json()["data"]
    methods = client.get("/api/v1/auth/methods").json()["data"]

    assert anonymous == {"initialized": True, "bootstrapAvailable": False, "session": None}
    assert signed["session"]["user"]["username"] == "alice"
    assert "sessionToken" not in signed["session"]
    assert "local_desktop" not in client.get("/health").json()["data"]
    assert methods["methods"] == {}


def test_hosted_startup_refuses_unsafe_configuration(tmp_path) -> None:
    with pytest.raises(DeploymentConfigurationError) as missing_key:
        build_hosted_runtime(hosted_env(tmp_path, NEWSROOM_CREDENTIAL_ENCRYPTION_KEY=""))
    assert missing_key.value.variable == "NEWSROOM_CREDENTIAL_ENCRYPTION_KEY"

    for activity_key in ("", "not-a-fernet-key"):
        with pytest.raises(DeploymentConfigurationError) as activity:
            build_hosted_runtime(hosted_env(tmp_path, NEWS_ACTIVITY_ENCRYPTION_KEY=activity_key))
        assert activity.value.variable == "NEWS_ACTIVITY_ENCRYPTION_KEY"

    with pytest.raises(DeploymentConfigurationError) as environment_mode:
        build_hosted_runtime(hosted_env(tmp_path, NEWSROOM_LLM_CREDENTIAL_MODE="environment"))
    assert environment_mode.value.variable == "NEWSROOM_LLM_CREDENTIAL_MODE"

    with pytest.raises(DeploymentConfigurationError) as insecure:
        build_hosted_runtime(hosted_env(tmp_path, NEWSROOM_ALLOW_INSECURE_ORIGIN="0"))
    assert insecure.value.variable == "NEWSROOM_PUBLIC_ORIGIN"

    with pytest.raises(DeploymentConfigurationError) as process_key_feature:
        build_hosted_runtime(hosted_env(tmp_path, NEWS_VISUAL_DESCRIPTION_ENABLED="1"))
    assert process_key_feature.value.variable == "NEWS_VISUAL_DESCRIPTION_ENABLED"

    for variable in ("NEWS_RESEARCH_PARSER_BACKENDS", "NEWSROOM_PDF_PARSER_CASCADE"):
        for backends in ("", "marker", "pymupdf,marker"):
            with pytest.raises(DeploymentConfigurationError) as docker_parser:
                build_hosted_runtime(hosted_env(tmp_path, **{variable: backends}))
            assert docker_parser.value.variable == variable

    with pytest.raises(DeploymentConfigurationError) as allowlist:
        build_hosted_runtime(
            hosted_env(tmp_path, NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS="http://gateway:8080")
        )
    assert allowlist.value.variable == "NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS"


_SMALL_ONLY_CATALOG = """\
schema_version: hosted-llm-providers.v2
providers:
  - id: tiny
    label: Tiny
    protocol: openai_chat
    base_url: https://tiny.example.com/v1
    default_model: tiny-8k
    models:
      - id: tiny-8k
        label: Tiny 8K
        context_window_tokens: 8000
        reasoning:
          parameter: none
        structured_output:
          mode: json_object
          revision: tiny-json-object
          source: https://tiny.example.com
"""


def test_hosted_startup_refuses_a_catalog_that_cannot_serve_every_purpose(tmp_path) -> None:
    catalog = tmp_path / "small-catalog.yaml"
    catalog.write_text(_SMALL_ONLY_CATALOG, encoding="utf-8")

    with pytest.raises(DeploymentConfigurationError) as too_small:
        build_hosted_runtime(hosted_env(tmp_path, NEWSROOM_HOSTED_LLM_PROVIDERS_PATH=str(catalog)))

    assert too_small.value.variable == "NEWSROOM_HOSTED_LLM_PROVIDERS_PATH"
    assert "answer" in str(too_small.value)


def test_hosted_startup_moves_the_single_key_store_into_a_connection(tmp_path) -> None:
    # A deployment upgraded from the one-key-per-account release still has
    # model-credentials.json; the first start turns it into a connection.
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    cipher = CredentialCipher(base64.b64decode(TEST_ENCRYPTION_KEY))
    sealed = cipher.seal(KEY_A, single_key_credential_binding("user-a", "deepseek"))
    (accounts / "model-credentials.json").write_text(json.dumps({
        "schemaVersion": "agora_model_credentials.v1",
        "credentials": [{
            "userId": "user-a", "providerId": "deepseek", "model": "deepseek-flash",
            "keyId": sealed.key_id, "nonce": sealed.nonce, "ciphertext": sealed.ciphertext,
            "keyHint": KEY_A[-4:], "revision": 1, "updatedAt": "2026-10-20T08:00:00Z",
        }],
    }), encoding="utf-8")

    runtime = build_hosted_runtime(hosted_env(tmp_path))

    assert (accounts / "model-credentials.json.migrated").exists()
    assert not (accounts / "model-credentials.json").exists()
    endpoint = runtime.model_connections.endpoint_for("user-a", ModelPurpose.ANSWER)
    assert (endpoint.provider, endpoint.model, endpoint.api_key) == ("deepseek", "deepseek-flash", KEY_A)


def test_hosted_startup_requires_the_configured_input_window(tmp_path) -> None:
    # Accounts bring their own models, so the budget cannot come from an
    # operator deployment: an unset input window is a configuration error.
    with pytest.raises(DeploymentConfigurationError) as unset:
        build_hosted_runtime(hosted_env(tmp_path, NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS=""))
    assert unset.value.variable == "NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS"

    runtime = build_hosted_runtime(hosted_env(tmp_path))
    candidate = runtime.model_connections.requirements[ModelPurpose.CANDIDATE_GENERATION]
    assert (candidate.input_tokens, candidate.output_tokens, candidate.structured_output) == (64000, 2048, True)


def test_hosted_app_refuses_static_api_keys(monkeypatch, tmp_path) -> None:
    configure_hosted_env(monkeypatch, tmp_path)
    runtime = build_test_hosted_runtime(tmp_path)

    with pytest.raises(DeploymentConfigurationError):
        create_app(hosted_runtime=runtime, api_token="operator-token", audit_emitter_factory=None)
    with pytest.raises(DeploymentConfigurationError):
        create_app(hosted_runtime=runtime, api_rate_limit_per_minute=60, audit_emitter_factory=None)
