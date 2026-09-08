from dataclasses import replace

import pytest

from backend.research.rag.adapters import ResearchRAGMemoryPort
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.memory.fake import FakeMemoryPort
from framework.harness.rag.fake import FakeRAGPlanner, fake_rag_session_spec, fake_research_evidence_packs
from framework.harness.rag.models import RAGSessionStatus
from framework.harness.rag.replay import replay_rag_session
from framework.harness.rag.session import BoundedRAGSessionController
from framework.harness.retrieval.fake import FakeRetrievalPort
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore
from tests.fixtures.admitted_memory import admitted_memory


def _spec(fixture):
    base = fake_rag_session_spec()
    return replace(
        base, graph_identity=fixture.graph,
        goal=replace(base.goal, question="How does the ablation evidence support the method?"),
        allowed_memory_namespaces=(fixture.metadata.namespace,),
        metadata={**base.metadata, "user_id": fixture.root.policy.owner_id},
        source_policy={**base.source_policy, "tenant_id": fixture.root.policy.tenant_id},
    )


@pytest.mark.parametrize("failure", ["unbound", "wrong_activity", "wrong_attempt", "logical", "uncommitted"])
def test_controller_rejects_memory_before_planner_or_retrieval(tmp_path, monkeypatch, failure):
    fixture = admitted_memory(tmp_path, commit=failure != "uncommitted")
    spec = _spec(fixture)
    if failure == "wrong_activity":
        spec = replace(spec, graph_identity=replace(spec.graph_identity, activity_id="another-activity"))
    elif failure == "wrong_attempt":
        spec = replace(spec, graph_identity=replace(spec.graph_identity, activity_attempt=2))
    elif failure == "logical":
        spec = replace(spec, graph_identity=replace(
            spec.graph_identity, node_id=None, node_instance_id=None, activity_id=None, activity_attempt=None,
        ))
    planner = FakeRAGPlanner()
    retrieval = FakeRetrievalPort(fake_research_evidence_packs())
    monkeypatch.setattr(planner, "plan", lambda *args, **kwargs: pytest.fail("planner called before admission"))
    monkeypatch.setattr(retrieval, "retrieve", lambda *args, **kwargs: pytest.fail("retrieval before admission"))
    monkeypatch.setattr(fixture.namespaces, "read", lambda ref: pytest.fail("unauthorized memory read"))
    controller = BoundedRAGSessionController(
        retrieval=retrieval, planner=planner,
        memory=FakeMemoryPort() if failure == "unbound" else ResearchRAGMemoryPort(fixture.recall),
    )
    with pytest.raises(HarnessValidationError):
        controller.run(spec)


def test_real_rag_controller_persists_and_replays_pinned_memory_without_live_reads(tmp_path, monkeypatch):
    fixture = admitted_memory(tmp_path / "grant")
    controller = BoundedRAGSessionController(
        retrieval=FakeRetrievalPort(fake_research_evidence_packs()), planner=FakeRAGPlanner(),
        memory=ResearchRAGMemoryPort(fixture.recall),
    )
    result = controller.run(_spec(fixture))
    assert result.status is RAGSessionStatus.SUCCEEDED
    assert result.context_pack is not None
    hit = result.context_pack.memory_context[0]
    assert hit["memory_ref"] == f"{fixture.metadata.exact_ref}#record=mem%2F1"
    assert hit["namespace_checksum"] == fixture.metadata.source_checksum
    assert hit["input_snapshot_ref"] == fixture.root.snapshot_ref
    assert hit["execution_identity"] == fixture.root.execution_identity.to_dict()
    assert "Prior paper ask" in hit["summary"]
    store = PaperRagTranscriptFileStore(tmp_path / "transcripts")
    artifact = store.persist(result.transcript)
    fixture.publisher.publish((replace(fixture.records[0], content="new current ablation evidence"),))
    high_watermark = fixture.events.get_stream_high_watermark("run:rag-grant-run", tenant_id="control")
    for target, names in (
        (fixture.namespaces, ("describe", "read")),
        (fixture.snapshots, ("get", "commit")),
        (fixture.recall, ("recall",)),
        (controller.planner, ("plan",)),
        (controller.retrieval, ("retrieve",)),
    ):
        for name in names:
            monkeypatch.setattr(target, name, lambda *args, **kwargs: pytest.fail("offline replay performed live IO"))
    replay = replay_rag_session(store.transcript_payload(artifact.path))
    assert replay.replayable
    assert replay.context_pack["memory_context"] == [hit]
    assert "new current ablation evidence" not in str(replay.to_dict())
    assert fixture.events.get_stream_high_watermark("run:rag-grant-run", tenant_id="control") == high_watermark
