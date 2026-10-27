from __future__ import annotations

import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace

from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.composition.personal_memory import build_personal_memory_runtime
from interfaces.services.auth_service import AuthSessionInvalidError
from interfaces.services.research_desktop_control import canonical_request_fingerprint
from interfaces.services.research_service import ResearchApplicationService
from tests.interfaces.api.hosted_support import (
    HOSTED_ORIGIN,
    build_test_hosted_runtime,
    configure_hosted_env,
)


ORIGIN = HOSTED_ORIGIN
BASE = "/api/v1/research/desktop/memory"


def _hosted_memory_env(monkeypatch, tmp_path, database_path) -> None:
    configure_hosted_env(monkeypatch, tmp_path)
    # These tests exercise the real SQLite memory runtime, not RAG startup.
    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "0")
    monkeypatch.setenv(
        "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH",
        str(database_path),
    )


def _hosted_runtime(tmp_path):
    return build_test_hosted_runtime(tmp_path, auth=_AuthService())


def _headers(token: str = "session-a", *, mutation: bool = False) -> dict[str, str]:
    headers = {"X-Newsroom-Session": token}
    if mutation:
        headers["Origin"] = ORIGIN
    return headers


def _create_body(
    *,
    request_id: str = "memory-request-1",
    creation: str = "confirmed",
    content: str = "I research deterministic agent evaluation.",
) -> dict[str, object]:
    return {
        "creation": creation,
        "request_id": request_id,
        "content": content,
        "category": "research_interest",
        "source_refs": [
            {
                "source_type": "user_input",
                "source_ref": "user://personal-memory/settings",
                "label": "Memory settings",
            }
        ],
    }


def _client(monkeypatch, tmp_path):
    database_path = (tmp_path / "memory" / "personal-memory.sqlite3").resolve()
    _hosted_memory_env(monkeypatch, tmp_path, database_path)
    runtime = build_personal_memory_runtime()
    app = create_app(
        hosted_runtime=_hosted_runtime(tmp_path),
        research_desktop_service_factory=lambda: _DesktopLeaseService(),
        personal_memory_service_factory=lambda: runtime.service,
        audit_emitter_factory=None,
    )
    return TestClient(app), runtime


def test_personal_memory_requires_account_session_origin_and_rejects_owner_fields(
    monkeypatch,
    tmp_path,
) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    try:
        anonymous = client.get(BASE)
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "auth_required"

        cross_origin = client.post(
            BASE,
            headers=_headers(mutation=False),
            json=_create_body(),
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["error"]["code"] == "origin_forbidden"

        duplicate_status = client.get(
            BASE,
            headers=_headers(),
            params=[("status", "active"), ("status", "active")],
        )
        assert duplicate_status.status_code == 400

        forged = client.post(
            BASE,
            headers=_headers(mutation=True),
            json={**_create_body(), "user_id": "other-user"},
        )
        assert forged.status_code == 422
        assert forged.headers["cache-control"] == "private, no-store"
        assert forged.json()["error"]["code"] == "invalid_request"
    finally:
        runtime.close()


def test_personal_memory_complete_management_recall_export_and_policy_flow(
    monkeypatch,
    tmp_path,
) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    try:
        created = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(creation="candidate"),
        )
        assert created.status_code == 200
        record = created.json()["data"]["record"]
        memory_id = record["memory_id"]
        assert record["status"] == "candidate"
        assert record["revision"] == 1
        assert "owner" not in record
        assert created.headers["cache-control"] == "private, no-store"

        repeated = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(creation="candidate"),
        )
        assert repeated.json()["data"]["record"]["memory_id"] == memory_id

        incompatible = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(
                creation="candidate",
                content="A different idempotent payload.",
            ),
        )
        assert incompatible.status_code == 409
        assert incompatible.json()["error"]["details"]["reason"] == "idempotency_conflict"

        confirmed = client.post(
            f"{BASE}/{memory_id}/confirm",
            headers=_headers(mutation=True),
            json={"revision": 1, "content": "I research deterministic agent evaluation and replay."},
        )
        assert confirmed.status_code == 200
        confirmed_record = confirmed.json()["data"]["record"]
        assert confirmed_record["status"] == "active"
        assert confirmed_record["revision"] == 2

        stale = client.patch(
            f"{BASE}/{memory_id}",
            headers=_headers(mutation=True),
            json={"revision": 1, "category": "background"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["reason"] == "revision_conflict"

        updated = client.patch(
            f"{BASE}/{memory_id}",
            headers=_headers(mutation=True),
            json={"revision": 2, "category": "background"},
        )
        assert updated.json()["data"]["record"]["revision"] == 3

        recalled = client.post(
            f"{BASE}/recall",
            headers=_headers(mutation=True),
            json={"query": "deterministic replay", "limit": 5, "use_memory": True},
        )
        recall_data = recalled.json()["data"]
        assert recall_data["used_memory_refs"] == [
            {"memory_id": memory_id, "revision": 3}
        ]
        assert recall_data["records"][0]["memory_id"] == memory_id

        disabled = client.post(
            f"{BASE}/{memory_id}/status",
            headers=_headers(mutation=True),
            json={"status": "disabled", "revision": 3},
        )
        assert disabled.json()["data"]["record"]["status"] == "disabled"

        enabled = client.post(
            f"{BASE}/{memory_id}/status",
            headers=_headers(mutation=True),
            json={"status": "active", "revision": 4},
        )
        assert enabled.json()["data"]["record"]["status"] == "active"

        policy = client.get(f"{BASE}/policy", headers=_headers())
        assert policy.json()["data"]["policy"]["allow_remote_processing"] is False
        assert policy.json()["data"]["policy"]["remote_processing_permissions"] == {
            "model": False,
            "tool": False,
            "embedding": False,
        }
        assert policy.json()["data"]["policy"]["revision"] == 1

        allowed = client.put(
            f"{BASE}/policy",
            headers=_headers(mutation=True),
            json={"allow_remote_processing": True, "revision": 1},
        )
        assert allowed.json()["data"]["policy"]["allow_remote_processing"] is True
        assert allowed.json()["data"]["policy"]["remote_processing_permissions"] == {
            "model": True,
            "tool": True,
            "embedding": True,
        }
        assert allowed.json()["data"]["policy"]["revision"] == 2

        exported = client.get(f"{BASE}/export", headers=_headers())
        export_data = exported.json()["data"]["export"]
        assert export_data["records"][0]["memory_id"] == memory_id
        assert "owner" not in export_data
        assert "owner" not in export_data["records"][0]
        assert export_data["policy"]["revision"] == 2
        assert not {"api_key", "token", "secret"} & set(export_data)
        assert not {"api_key", "token", "secret"} & set(export_data["policy"])

        info = client.get(f"{BASE}/info", headers=_headers()).json()["data"]["info"]
        assert info["storage"] == "local_sqlite"
        assert info["database_path"].endswith("personal-memory.sqlite3")
        assert info["remote_processing_default"] is False
        assert info["remote_processing_channel_defaults"] == {
            "model": False,
            "tool": False,
            "embedding": False,
        }
        assert info["remote_processing_channels"] == ["model", "tool", "embedding"]
        assert info["remote_processing_auto_enabled"] is False
        assert not any("key" in key.casefold() for key in info)

        rebuilt = client.post(
            f"{BASE}/rebuild-index",
            headers=_headers(mutation=True),
        )
        assert rebuilt.json()["data"] == {"indexed_records": 1}

        deleted = client.request(
            "DELETE",
            f"{BASE}/{memory_id}",
            headers=_headers(mutation=True),
            json={"revision": 5},
        )
        assert deleted.json()["data"]["record"]["status"] == "deleted"

        listing = client.get(BASE, headers=_headers()).json()["data"]
        assert listing["records"] == []
        assert listing["next_cursor"] is None
        tombstones = client.get(
            BASE,
            headers=_headers(),
            params=[("status", "deleted")],
        ).json()["data"]
        assert tombstones["records"][0]["memory_id"] == memory_id

        ordinary_export = client.get(f"{BASE}/export", headers=_headers()).json()["data"]["export"]
        assert ordinary_export["records"] == []
        explicit_history = client.get(
            f"{BASE}/export",
            headers=_headers(),
            params={"include_deleted": "true"},
        ).json()["data"]["export"]
        assert explicit_history["records"][0]["status"] == "deleted"
        assert any("historical conversations" in notice for notice in explicit_history["notices"])

        rejected_candidate = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(
                creation="candidate",
                request_id="memory-request-rejected-candidate",
                content="Reject this candidate without activating it.",
            ),
        ).json()["data"]["record"]
        rejected = client.request(
            "DELETE",
            f"{BASE}/{rejected_candidate['memory_id']}",
            headers=_headers(mutation=True),
            json={"revision": rejected_candidate["revision"]},
        )
        assert rejected.json()["data"]["record"]["status"] == "deleted"
    finally:
        runtime.close()


def test_personal_memory_owner_isolation_not_found_and_write_failure_mapping(
    monkeypatch,
    tmp_path,
) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    try:
        created = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(),
        ).json()["data"]["record"]

        hidden = client.get(
            f"{BASE}/{created['memory_id']}",
            headers=_headers("session-b"),
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "personal_memory_not_found"

        assert runtime.store._connection is not None  # type: ignore[attr-defined]
        runtime.store._connection.execute("PRAGMA query_only = ON")  # type: ignore[union-attr]
        failed = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(request_id="memory-request-write-failure"),
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "personal_memory_store_unavailable"
        assert failed.json()["error"]["retryable"] is True
        runtime.store._connection.execute("PRAGMA query_only = OFF")  # type: ignore[union-attr]
    finally:
        runtime.close()


def test_personal_memory_routes_are_hidden_outside_the_hosted_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEWSROOM_DEPLOYMENT_PROFILE", "default")
    runtime = build_personal_memory_runtime(
        env={"NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH": str((tmp_path / "memory.sqlite3").resolve())}
    )
    client = TestClient(
        create_app(
            personal_memory_service_factory=lambda: runtime.service,
            audit_emitter_factory=None,
        )
    )
    try:
        response = client.get(BASE)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.headers["cache-control"] == "private, no-store"
    finally:
        runtime.close()


def test_default_api_factory_composes_real_sqlite_runtime(monkeypatch, tmp_path) -> None:
    database_path = (tmp_path / "default-runtime" / "personal-memory.sqlite3").resolve()
    _hosted_memory_env(monkeypatch, tmp_path, database_path)
    app = create_app(
        hosted_runtime=_hosted_runtime(tmp_path),
        research_desktop_service_factory=lambda: _DesktopLeaseService(),
        audit_emitter_factory=None,
    )

    with TestClient(app) as client:
        response = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(request_id="default-runtime-create"),
        )
        assert response.status_code == 200
        assert response.json()["data"]["record"]["status"] == "active"

    assert database_path.is_file()


def test_default_factories_share_real_personal_memory_core_with_desktop_task_context(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = (tmp_path / "shared-runtime" / "personal-memory.sqlite3").resolve()
    _hosted_memory_env(monkeypatch, tmp_path, database_path)
    monkeypatch.setenv(
        "NEWSROOM_DESKTOP_CONTROL_PATH",
        str((tmp_path / "desktop-control.json").resolve()),
    )
    research = _CapturingResearchService()
    hosted_runtime = _hosted_runtime(tmp_path)
    # Hosted research is admitted only for an account with a model key.
    hosted_runtime.model_credentials.save(
        "user-a",
        provider_id="deepseek",
        model="deepseek-v4-flash",
        api_key="sk-personal-memory-test",
        expected_revision=None,
    )
    app = create_app(
        hosted_runtime=hosted_runtime,
        research_service_factory=lambda: research,
        audit_emitter_factory=None,
    )

    with TestClient(app) as client:
        identity = client.get(
            "/api/v1/research/desktop/identity",
            headers=_headers(),
        ).json()["data"]
        created = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(request_id="shared-runtime-create"),
        ).json()["data"]["record"]
        policy = client.get(f"{BASE}/policy", headers=_headers()).json()["data"]["policy"]
        assert policy["allow_remote_processing"] is False

        disabled = client.post(
            "/api/v1/research/desktop/operations/ask",
            headers=_headers(mutation=True),
            json=_ask_payload(
                request_id="shared-memory-off",
                generation=identity["generation"],
                memory_id=created["memory_id"],
                memory_revision=created["revision"],
                policy_revision=policy["revision"],
            ),
        )
        assert disabled.status_code == 403
        assert disabled.json()["error"]["code"] == (
            "personal_memory_remote_processing_disabled"
        )
        assert research.contexts == []

        enabled_policy = client.put(
            f"{BASE}/policy",
            headers=_headers(mutation=True),
            json={
                "allow_remote_processing": True,
                "revision": policy["revision"],
            },
        ).json()["data"]["policy"]
        accepted = client.post(
            "/api/v1/research/desktop/operations/ask",
            headers=_headers(mutation=True),
            json=_ask_payload(
                request_id="shared-memory-on",
                generation=identity["generation"],
                memory_id=created["memory_id"],
                memory_revision=created["revision"],
                policy_revision=enabled_policy["revision"],
            ),
        )
        assert accepted.status_code == 200
        operation = accepted.json()["data"]
        assert operation["status"] == "passed"
        answer = operation["result"]
        assert answer["answerStatus"] == "answered"
        assert answer["evidenceRefs"] == ["evidence-memory-methods-1"]
        assert answer["citations"][0]["evidenceId"] == "evidence-memory-methods-1"
        assert answer["sourceRevision"] == "source-revision-1"
        assert answer["transcriptId"] == "rag-transcript://memory-fixture/answer-1"
        assert answer["usedPersonalMemory"] == [
            {"memoryId": created["memory_id"], "revision": created["revision"]}
        ]

    assert database_path.is_file()
    assert research.owners == [("user-a", None, None)]
    assert len(research.contexts) == 1
    context = research.contexts[0]
    assert context.policy_revision == enabled_policy["revision"]
    assert context.used_refs == (
        {"memoryId": created["memory_id"], "revision": created["revision"]},
    )


def test_default_personal_memory_runtime_is_constructed_once_under_concurrency_and_closed_once(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = (tmp_path / "concurrent-runtime" / "personal-memory.sqlite3").resolve()
    _hosted_memory_env(monkeypatch, tmp_path, database_path)
    monkeypatch.setenv(
        "NEWSROOM_DESKTOP_CONTROL_PATH",
        str((tmp_path / "concurrent-control.json").resolve()),
    )
    app_module = importlib.import_module("interfaces.api.app")
    actual_factory = build_personal_memory_runtime
    counts = {"constructed": 0, "closed": 0}
    count_lock = Lock()

    def counted_factory():
        with count_lock:
            counts["constructed"] += 1
        time.sleep(0.05)
        return _CountingRuntime(actual_factory(), counts, count_lock)

    monkeypatch.setattr(app_module, "build_personal_memory_runtime", counted_factory)
    app = create_app(
        hosted_runtime=_hosted_runtime(tmp_path),
        research_service_factory=_CapturingResearchService,
        audit_emitter_factory=None,
    )
    assert counts == {"constructed": 0, "closed": 0}

    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(
                executor.map(
                    lambda index: client.get(
                        f"{BASE}/info"
                        if index % 2 == 0
                        else "/api/v1/research/desktop/identity",
                        headers=_headers(),
                    ),
                    range(8),
                )
            )
        assert all(response.status_code == 200 for response in responses)
        assert counts == {"constructed": 1, "closed": 0}

    assert counts == {"constructed": 1, "closed": 1}


def test_custom_personal_memory_factories_do_not_construct_a_second_default_store(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = (tmp_path / "custom-runtime" / "personal-memory.sqlite3").resolve()
    _hosted_memory_env(monkeypatch, tmp_path, database_path)
    monkeypatch.setenv(
        "NEWSROOM_DESKTOP_CONTROL_PATH",
        str((tmp_path / "custom-control.json").resolve()),
    )
    runtime = build_personal_memory_runtime()
    app_module = importlib.import_module("interfaces.api.app")

    def reject_default_runtime():
        raise AssertionError("default personal-memory runtime must not be constructed")

    monkeypatch.setattr(
        app_module,
        "build_personal_memory_runtime",
        reject_default_runtime,
    )
    core_factory_calls = 0

    def personal_memory_core_factory():
        nonlocal core_factory_calls
        core_factory_calls += 1
        return runtime.application

    app = create_app(
        hosted_runtime=_hosted_runtime(tmp_path),
        research_service_factory=_CapturingResearchService,
        personal_memory_service_factory=lambda: runtime.service,
        personal_memory_core_factory=personal_memory_core_factory,
        audit_emitter_factory=None,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                BASE,
                headers=_headers(mutation=True),
                json=_create_body(request_id="custom-runtime-create"),
            )
            assert response.status_code == 200
            # The desktop service is built on its first research request and
            # must reuse the injected core instead of opening the default store.
            identity = client.get("/api/v1/research/desktop/identity", headers=_headers())
            assert identity.status_code == 200
            client.get("/api/v1/research/desktop/identity", headers=_headers())
            assert core_factory_calls == 1
    finally:
        runtime.close()


def _ask_payload(
    *,
    request_id: str,
    generation: str,
    memory_id: str,
    memory_revision: int,
    policy_revision: int,
) -> dict[str, object]:
    personal_memory = {
        "refs": [{"memoryId": memory_id, "revision": memory_revision}],
        "policyRevision": policy_revision,
    }
    semantic = {
        "kind": "ask",
        "runId": "run-memory-1",
        "paperId": "paper-memory-1",
        "sourceRevision": "source-revision-1",
        "sourceFingerprint": f"sha256:{'c' * 64}",
        "question": "How should this paper be evaluated?",
        "selection": {},
        "locale": None,
        "personalMemory": personal_memory,
    }
    return {
        "requestId": request_id,
        "generation": generation,
        "requestFingerprint": canonical_request_fingerprint(semantic),
        "runId": semantic["runId"],
        "paperId": semantic["paperId"],
        "sourceRevision": semantic["sourceRevision"],
        "sourceFingerprint": semantic["sourceFingerprint"],
        "question": semantic["question"],
        "selection": {},
        "personalMemory": personal_memory,
    }


class _CapturingResearchService(ResearchApplicationService):
    def __init__(self) -> None:
        super().__init__()
        self.contexts = []
        self.owners: list[tuple[str | None, str | None, str | None]] = []

    def get_run_projection(self, run_id: str, *, actor=None):
        del run_id, actor
        return {
            "runId": "run-memory-1",
            "paperId": "paper-memory-1",
            "sourceRevision": "source-revision-1",
            "sourceFingerprint": f"sha256:{'c' * 64}",
        }

    def ask_paper(self, paper_id: str, request, *, graph_identity=None):
        del graph_identity
        provider = request.personal_memory_context
        assert provider is not None
        context = provider.current()
        self.contexts.append(context)
        self.owners.append(
            (request.user_id, request.tenant_id, request.memory_namespace)
        )
        return {
            "runId": request.run_id,
            "paperId": paper_id,
            "answer": "The shared personal-memory context was applied.",
            "abstained": False,
            "status": "answered",
            "decision": {"decision_type": "return_answer"},
            "citations": [
                {
                    "id": "citation-memory-methods-1",
                    "label": "Methods",
                    "evidenceId": "evidence-memory-methods-1",
                    "paperId": paper_id,
                    "sourceRevision": "source-revision-1",
                    "sectionId": "methods",
                    "valid": True,
                }
            ],
            "evidenceRefs": ["evidence-memory-methods-1"],
            "confidence": 1.0,
            "transcriptId": "rag-transcript://memory-fixture/answer-1",
            "usedPersonalMemory": list(context.used_refs),
        }


class _CountingRuntime:
    def __init__(self, runtime, counts, count_lock: Lock) -> None:
        self.store = runtime.store
        self.application = runtime.application
        self.service = runtime.service
        self._runtime = runtime
        self._counts = counts
        self._count_lock = count_lock

    def close(self) -> None:
        with self._count_lock:
            self._counts["closed"] += 1
        self._runtime.close()


class _AuthService:
    def get_session(self, token: str):
        users = {
            "session-a": "user-a",
            "session-b": "user-b",
        }
        if token not in users:
            raise AuthSessionInvalidError("unknown test session")
        user_id = users[token]
        return SimpleNamespace(
            user=SimpleNamespace(userId=user_id, role="user"),
            sessionId=f"auth-{user_id}",
        )


class _DesktopLeaseService:
    def begin_business_operation(self, *, operation: str) -> str:
        return operation

    def end_business_operation(self, lease_id: str) -> None:
        del lease_id


def test_personal_memory_confirm_and_update_normalize_source_references(
    monkeypatch,
    tmp_path,
) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    source = {
        "source_type": "paper",
        "source_ref": "paper://deterministic-replay",
        "source_revision": "revision-1",
        "locator": "section:3",
        "label": "Explicitly reviewed paper",
    }
    try:
        created = client.post(
            BASE,
            headers=_headers(mutation=True),
            json=_create_body(creation="candidate"),
        )
        assert created.status_code == 200
        memory_id = created.json()["data"]["record"]["memory_id"]
        confirmed = client.post(
            f"{BASE}/{memory_id}/confirm",
            headers=_headers(mutation=True),
            json={"revision": 1, "source_refs": [source]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["data"]["record"]["source_refs"] == [source]

        updated_source = {**source, "source_revision": "revision-2", "locator": "section:4"}
        updated = client.patch(
            f"{BASE}/{memory_id}",
            headers=_headers(mutation=True),
            json={"revision": 2, "source_refs": [updated_source]},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["record"]["source_refs"] == [updated_source]
        assert updated.json()["data"]["record"]["revision"] == 3
    finally:
        runtime.close()
