from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import RAGSessionStatus, RAGTranscript
from framework.shared.json import stable_json_dumps, to_jsonable


@dataclass(frozen=True)
class RAGReplayCheck:
    name: str
    passed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RAGSessionReplayResult:
    transcript_id: str
    session_id: str
    status: RAGSessionStatus
    event_count: int
    phase_sequence: tuple[str, ...]
    gate_results: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    budget_snapshots: tuple[dict[str, Any], ...]
    context_pack: dict[str, Any] | None = None
    answer: dict[str, Any] | None = None
    replay_checks: tuple[RAGReplayCheck, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def replayable(self) -> bool:
        return not self.errors and all(check.passed for check in self.replay_checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_id": self.transcript_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "event_count": self.event_count,
            "phase_sequence": list(self.phase_sequence),
            "gate_results": [dict(item) for item in self.gate_results],
            "decisions": [dict(item) for item in self.decisions],
            "budget_snapshots": [dict(item) for item in self.budget_snapshots],
            "context_pack": to_jsonable(self.context_pack),
            "answer": to_jsonable(self.answer),
            "replay_checks": [check.to_dict() for check in self.replay_checks],
            "errors": list(self.errors),
            "replayable": self.replayable,
        }


class RAGSessionReplayRunner:
    def replay(
        self,
        transcript: RAGTranscript | Mapping[str, Any],
        *,
        snapshots: Mapping[str, Any] | None = None,
    ) -> RAGSessionReplayResult:
        resolved = _coerce_transcript(transcript)
        events = tuple(_normalize_event(event) for event in resolved.events)
        if not events:
            raise HarnessValidationError("RAG replay requires transcript events")

        phase_sequence = tuple(str(event["event_type"]) for event in events)
        context_pack = _last_context_pack(events)
        answer = _last_answer(events)
        checks = [
            _terminal_event_check(status=resolved.status, phase_sequence=phase_sequence),
            *_snapshot_checks(context_pack, snapshots),
        ]
        errors = tuple(check.reason for check in checks if not check.passed)
        return RAGSessionReplayResult(
            transcript_id=resolved.transcript_id,
            session_id=resolved.session_id,
            status=resolved.status,
            event_count=len(events),
            phase_sequence=phase_sequence,
            gate_results=tuple(_gate_timeline(events)),
            decisions=tuple(_decision_timeline(events)),
            budget_snapshots=tuple(_budget_snapshots(events)),
            context_pack=context_pack,
            answer=answer,
            replay_checks=tuple(checks),
            errors=errors,
        )


def replay_rag_session(
    transcript: RAGTranscript | Mapping[str, Any],
    *,
    snapshots: Mapping[str, Any] | None = None,
) -> RAGSessionReplayResult:
    return RAGSessionReplayRunner().replay(transcript, snapshots=snapshots)


def _coerce_transcript(value: RAGTranscript | Mapping[str, Any]) -> RAGTranscript:
    if isinstance(value, RAGTranscript):
        return value
    payload = dict(value)
    return RAGTranscript(
        transcript_id=str(payload.get("transcript_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        events=tuple(payload.get("events") or ()),
        status=payload.get("status") or "",
        created_at=payload.get("created_at"),
    )


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(event)
    event_type = str(item.get("event_type") or "").strip()
    if not event_type:
        raise HarnessValidationError("RAG replay event requires event_type")
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        raise HarnessValidationError("RAG replay event payload must be an object")
    return {"event_type": event_type, "payload": dict(payload)}


def _last_context_pack(events: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    packs = [
        event["payload"].get("pack")
        for event in events
        if event["event_type"] == "rag_context_pack_assembled"
        and isinstance(event["payload"].get("pack"), dict)
    ]
    return dict(packs[-1]) if packs else None


def _last_answer(events: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    answers = [
        event["payload"]
        for event in events
        if event["event_type"] == "rag_answer_candidate_created"
    ]
    return dict(answers[-1]) if answers else None


def _gate_timeline(events: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        payload = event["payload"]
        raw_results = payload.get("gate_results")
        if isinstance(raw_results, list):
            for result in raw_results:
                if isinstance(result, dict):
                    timeline.append({
                        "event_index": index,
                        "event_type": event["event_type"],
                        **dict(result),
                    })
        elif event["event_type"] == "rag_gate_failed":
            timeline.append({
                "event_index": index,
                "event_type": event["event_type"],
                **dict(payload),
            })
    return timeline


def _decision_timeline(events: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    decision_events = {
        "rag_evidence_type_gap_accepted",
        "rag_replanned",
        "rag_context_pack_returned",
        "rag_answer_returned",
        "rag_abstained",
        "rag_halted",
    }
    decisions: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if event["event_type"] not in decision_events:
            continue
        decisions.append({
            "event_index": index,
            "event_type": event["event_type"],
            "payload": dict(event["payload"]),
        })
    return decisions


def _budget_snapshots(events: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        budget = event["payload"].get("budget_snapshot")
        if isinstance(budget, dict):
            snapshots.append({
                "event_index": index,
                "event_type": event["event_type"],
                "budget_snapshot": dict(budget),
            })
    return snapshots


def _terminal_event_check(*, status: RAGSessionStatus, phase_sequence: tuple[str, ...]) -> RAGReplayCheck:
    terminal_events_by_status = {
        RAGSessionStatus.SUCCEEDED: {"rag_context_pack_returned"},
        RAGSessionStatus.ANSWERED: {"rag_answer_returned"},
        RAGSessionStatus.ABSTAINED: {"rag_abstained"},
        RAGSessionStatus.HALTED: {"rag_halted"},
        RAGSessionStatus.INSUFFICIENT_EVIDENCE: {"rag_halted", "rag_replanned", "rag_source_verified"},
        RAGSessionStatus.FAILED: {"rag_halted"},
    }
    expected = terminal_events_by_status.get(status, set())
    passed = any(event_type in expected for event_type in phase_sequence)
    return RAGReplayCheck(
        name="terminal_event",
        passed=passed,
        reason="terminal event matches transcript status" if passed else f"missing terminal event for status {status.value}",
        metadata={"status": status.value, "expected_event_types": sorted(expected)},
    )


def _snapshot_checks(
    context_pack: dict[str, Any] | None,
    snapshots: Mapping[str, Any] | None,
) -> tuple[RAGReplayCheck, ...]:
    if context_pack is None:
        return (RAGReplayCheck("snapshot_refs", True, "no context pack refs recorded"),)
    refs = _context_pack_refs(context_pack)
    if snapshots is None:
        return (
            RAGReplayCheck(
                "snapshot_refs",
                True,
                "snapshot validation skipped",
                {"required_refs": sorted(refs)},
            ),
        )
    checks: list[RAGReplayCheck] = []
    for ref in sorted(refs):
        if ref not in snapshots:
            checks.append(RAGReplayCheck("snapshot_ref_present", False, f"missing replay snapshot: {ref}", {"ref": ref}))
            continue
        snapshot = snapshots[ref]
        payload, checksum = _snapshot_payload_and_checksum(snapshot)
        if checksum is None:
            checks.append(RAGReplayCheck("snapshot_ref_present", True, "snapshot present", {"ref": ref}))
            continue
        actual = _checksum(payload)
        checks.append(
            RAGReplayCheck(
                "snapshot_checksum",
                actual == checksum,
                "snapshot checksum matches" if actual == checksum else f"snapshot checksum mismatch: {ref}",
                {"ref": ref, "expected": checksum, "actual": actual},
            )
        )
    if not refs:
        checks.append(RAGReplayCheck("snapshot_refs", True, "context pack recorded no snapshot refs"))
    return tuple(checks)


def _context_pack_refs(context_pack: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    metadata = context_pack.get("metadata")
    if isinstance(metadata, dict):
        snapshot_ref = str(metadata.get("context_snapshot_ref") or "").strip()
        if snapshot_ref:
            refs.add(snapshot_ref)
    for key in ("artifact_refs", "context_refs", "source_refs"):
        raw = context_pack.get(key) or ()
        if isinstance(raw, str):
            raw = (raw,)
        if isinstance(raw, (list, tuple)):
            refs.update(str(ref) for ref in raw if str(ref).strip())
    for evidence in context_pack.get("accepted_evidence") or ():
        if not isinstance(evidence, dict):
            continue
        raw = evidence.get("artifact_refs") or ()
        if isinstance(raw, str):
            raw = (raw,)
        if isinstance(raw, (list, tuple)):
            refs.update(str(ref) for ref in raw if str(ref).strip())
    return refs


def _snapshot_payload_and_checksum(snapshot: Any) -> tuple[Any, str | None]:
    if isinstance(snapshot, dict) and "payload" in snapshot:
        checksum = snapshot.get("checksum")
        return snapshot.get("payload"), str(checksum) if checksum else None
    if isinstance(snapshot, dict) and "checksum" in snapshot:
        payload = {key: value for key, value in snapshot.items() if key != "checksum"}
        checksum = snapshot.get("checksum")
        return payload, str(checksum) if checksum else None
    return snapshot, None


def _checksum(payload: Any) -> str:
    return f"sha256:{sha256(stable_json_dumps(to_jsonable(payload)).encode('utf-8')).hexdigest()}"


__all__ = [
    "RAGReplayCheck",
    "RAGSessionReplayResult",
    "RAGSessionReplayRunner",
    "replay_rag_session",
]
