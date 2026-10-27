from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from interfaces.api import create_app
from interfaces.composition.research import (
    default_research_runtime_provider,
    reset_default_research_runtime,
)
from tests.interfaces.api.hosted_research_support import (
    configure_research_without_llm,
    content_fingerprint,
    forbid_network,
    import_pdf,
    selectable_text_pdf,
)
from tests.interfaces.api.hosted_support import HOSTED_ORIGIN, build_test_hosted_runtime, sign_in


_ORIGIN = HOSTED_ORIGIN
_OWNER = "research-owner"
_PASSWORD = "correct-horse-battery-staple"
_MEMORY_PATH = "/api/v1/research/desktop/memory"


def test_real_account_session_reaches_disabled_research_and_account_stores(
    monkeypatch,
    short_desktop_data_root: tuple[Path, Path],
) -> None:
    data_root, import_root = short_desktop_data_root
    configure_research_without_llm(monkeypatch, data_root)
    monkeypatch.setenv("NEWSROOM_RESEARCH_IMPORT_DIR", str(import_root))

    preload_calls: list[str] = []

    def preload_reranker() -> None:
        preload_calls.append("preload")

    monkeypatch.setattr(
        "interfaces.services.paper_rag_factory.preload_reranker",
        preload_reranker,
    )

    hosted_runtime = build_test_hosted_runtime(data_root)
    # Accounts exist only through server-side administration; there is no
    # browser bootstrap in the hosted profile.
    headers = sign_in(hosted_runtime, _OWNER, _PASSWORD)
    reset_default_research_runtime()
    app = create_app(
        hosted_runtime=hosted_runtime,
        audit_emitter_factory=None,
    )

    try:
        with TestClient(app) as client, monkeypatch.context() as network_guard:
            forbid_network(network_guard)
            session = client.get("/api/v1/auth/session", headers=headers)
            assert session.status_code == 200
            user_id = session.json()["data"]["session"]["user"]["userId"]

            history = client.get("/api/v1/research/history", headers=headers)
            assert history.status_code == 200
            assert history.json()["data"] == {
                "revision": 0,
                "visits": [],
                "groups": [],
            }

            identity = client.get(
                "/api/v1/research/desktop/identity",
                headers=headers,
            )
            assert identity.status_code == 200
            identity_data = identity.json()["data"]
            assert identity_data["owner"] == {
                "userId": user_id,
                "tenantId": None,
                "memoryNamespace": f"research:user:{user_id}",
            }
            assert identity_data["generation"].startswith("gen_")

            capabilities = client.get(
                "/api/v1/research/desktop/capabilities",
                headers=headers,
            )
            assert capabilities.status_code == 200
            assert (
                capabilities.json()["data"]["generation"]
                == identity_data["generation"]
            )

            policy = client.get(f"{_MEMORY_PATH}/policy", headers=headers)
            assert policy.status_code == 200
            policy_data = policy.json()["data"]["policy"]
            assert policy_data["allow_remote_processing"] is False
            assert policy_data["revision"] == 1
            assert policy_data["created_at"] == policy_data["updated_at"]

            created = client.post(
                _MEMORY_PATH,
                headers={**headers, "Origin": _ORIGIN},
                json={
                    "creation": "confirmed",
                    "request_id": "real-auth-memory-create",
                    "content": "Prefer concise, evidence-grounded summaries.",
                    "category": "reading_preference",
                    "source_refs": [
                        {
                            "source_type": "user_input",
                            "source_ref": "user://desktop/settings",
                        }
                    ],
                },
            )
            assert created.status_code == 200
            created_record = created.json()["data"]["record"]
            assert "owner" not in created_record
            assert created_record["status"] == "active"

            pdf = selectable_text_pdf("offline-paper", repeat=12)
            operation = import_pdf(
                client,
                headers=headers,
                generation=identity_data["generation"],
                request_id="real-auth-pdf-import",
                filename="offline-paper.pdf",
                source_revision=content_fingerprint(pdf),
                content=pdf,
            )
            paper_id = operation["result"]["paperId"]

            document = client.get(
                f"/api/v1/research/papers/{paper_id}/document",
                headers=headers,
            )
            assert document.status_code == 200
            document_data = document.json()["data"]["document"]
            assert document_data["status"] == "parsed"
            assert any("Introduction" in section["text"] for section in document_data["sections"])

            memories = client.get(_MEMORY_PATH, headers=headers)
            assert memories.status_code == 200
            assert memories.headers["cache-control"] == "private, no-store"
            assert [record["memory_id"] for record in memories.json()["data"]["records"]] == [
                created_record["memory_id"]
            ]

        composition = default_research_runtime_provider().get()
        assert composition.available is False
        assert composition.service.capability_projection()["parse"] is True
        assert composition.availability_error is not None
        assert composition.availability_error.to_public_dict()["capabilities"] == [
            "research.llm.route"
        ]
        # The warm-up runs beside the server so it never delays the listening socket.
        assert app.state.reranker_warmup.wait(timeout=10)
        assert preload_calls == ["preload"]
        assert storage_io_path(data_root / "desktop" / "control" / "control.json").is_file()
        assert storage_io_path(data_root / "research" / "personal-memory.sqlite3").is_file()
    finally:
        reset_default_research_runtime()


@pytest.fixture(params=("short", "candidate", "long-import"))
def short_desktop_data_root(request):
    if request.param == "short":
        root = resolve_storage_path(tempfile.mkdtemp(prefix="pa-api-"))
    else:
        root = resolve_storage_path(Path.cwd() / ".tmp" / "research-local" / uuid4().hex)
        storage_io_path(root).mkdir(parents=True, exist_ok=False)
    import_root = root / "research" / "imports"
    if request.param == "long-import":
        # Exercise this service's real long locator without making unrelated
        # auth/memory composition responsible for the import regression.
        import_root = import_root / ("desktop-data-" + "x" * 65) / ("owner-data-" + "y" * 65)
        assert len(str(import_root)) > 260
    print(f"IMPORT_PATH_CASE {request.param} data_root={root} import_root={import_root}")
    try:
        yield root, import_root
    finally:
        shutil.rmtree(storage_io_path(root))
