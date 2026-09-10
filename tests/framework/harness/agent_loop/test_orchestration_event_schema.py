from __future__ import annotations

from copy import deepcopy

import pytest

from framework.events.canonical import thaw_canonical_json
from framework.events.errors import EventSchemaError
from framework.events.schema import default_event_schema_catalog
from framework.events.schema.catalog import (
    TASK_PLAN_EVENT_SCHEMA_V3,
    TASK_PLAN_EVENT_TYPES as CATALOG_EVENT_TYPES,
)
from framework.harness.task_plan.store import TASK_PLAN_EVENT_TYPES
from tests.framework.harness.agent_loop.test_orchestration_runtime import _request, _runtime
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore,
    _EventStore,
    _store,
)


@pytest.fixture(scope="module")
def canonical_events():
    events = _EventStore()
    runtime, identity = _runtime(store=_store(events, _ArtifactStore()))
    assert runtime.dispatch(_request(identity)).status == "succeeded"
    return tuple(events._events)


@pytest.fixture(scope="module")
def catalog():
    return default_event_schema_catalog()


def test_every_task_plan_event_has_a_canonical_schema(catalog):
    assert set(TASK_PLAN_EVENT_TYPES) == set(CATALOG_EVENT_TYPES)
    for event_type in TASK_PLAN_EVENT_TYPES:
        assert catalog.current_schema(event_type) == TASK_PLAN_EVENT_SCHEMA_V3


def test_real_submission_and_parallel_lifecycle_payloads_validate(catalog, canonical_events):
    event_types = {event.event_type for event in canonical_events}
    assert {
        "PLAN_CANDIDATE_BUILT", "TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED",
        "TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_CONFIRMED",
        "TASK_WAVE_DISPATCHED", "TASK_WAVE_COMPLETED", "TASK_GROUP_JOINED",
        "TASK_PLAN_VERIFIED",
    }.issubset(event_types)
    event_type_sequence = [event.event_type for event in canonical_events]
    first_dispatch = event_type_sequence.index("TASK_WAVE_DISPATCHED")
    assert all(
        event_type_sequence.index(event_type) < first_dispatch
        for event_type in ("TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_CONFIRMED")
    )
    for event in canonical_events:
        catalog.validate(event.event_type, event.data_schema, event.payload)


@pytest.mark.parametrize("event_type,path", [
    ("PLAN_CANDIDATE_BUILT", ("submission",)),
    ("PLAN_CANDIDATE_BUILT", ("submission", "identity")),
    ("TASK_GROUP_ADMITTED", ("group",)),
    ("TASK_WAVE_ADMITTED", ("wave",)),
    ("TASK_WAVE_ADMITTED", ("wave", "reservations", 0)),
    ("TASK_GROUP_JOINED", ("observation",)),
    ("TASK_PLAN_VERIFIED", ("terminal_result",)),
    ("TASK_PLAN_VERIFIED", ("terminal_result", "output")),
])
def test_schema_rejects_unknown_nested_control_fields(catalog, canonical_events, event_type, path):
    event = next(event for event in canonical_events if event.event_type == event_type)
    payload = deepcopy(thaw_canonical_json(event.payload))
    target = payload["details"]
    for segment in path:
        target = target[segment]
    target["publication"] = True
    with pytest.raises(EventSchemaError):
        catalog.validate(event.event_type, event.data_schema, payload)


@pytest.mark.parametrize("field", ["submission_key", "terminal_result", "terminal_result_checksum"])
def test_terminal_outcome_schema_requires_complete_checksum_bound_record(catalog, canonical_events, field):
    event = next(event for event in canonical_events if event.event_type == "TASK_PLAN_VERIFIED")
    payload = thaw_canonical_json(event.payload)
    del payload["details"][field]
    with pytest.raises(EventSchemaError):
        catalog.validate(event.event_type, event.data_schema, payload)


def test_group_admission_requires_complete_pinned_group(catalog, canonical_events):
    event = next(event for event in canonical_events if event.event_type == "TASK_GROUP_ADMITTED")
    payload = thaw_canonical_json(event.payload)
    del payload["details"]["group"]["group_checksum"]
    with pytest.raises(EventSchemaError):
        catalog.validate(event.event_type, event.data_schema, payload)


@pytest.mark.parametrize("event_type", ["RECOVERY_STATUS_READ", "RECOVERY_RECONCILED", "RECOVERY_HALTED"])
@pytest.mark.parametrize("missing", [None, "group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "recovery_id"])
def test_recovery_schema_requires_complete_attempt_correlation(catalog, canonical_events, event_type, missing):
    source = next(event for event in canonical_events if event.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED")
    payload = thaw_canonical_json(source.payload)
    source_details = payload["details"]
    payload["details"] = {
        key: source_details[key] for key in (
            "group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key",
        )
    }
    details = payload["details"]
    details.update(event_type=event_type, recovery_id="recovery-test",
                   idempotency_key="recovery-test:event", parallel_event_idempotency_key="audit-test")
    if event_type == "RECOVERY_HALTED":
        details["reason_code"] = "SPAWN_UNKNOWN"
    else:
        details["recovery_outcome"] = "status_read" if event_type == "RECOVERY_STATUS_READ" else "SPAWN_CONFIRMED"
        if event_type == "RECOVERY_RECONCILED":
            details["child_id"] = source_details["child_id"]
    if missing is None:
        catalog.validate(event_type, source.data_schema, payload)
    else:
        del details[missing]
        with pytest.raises(EventSchemaError):
            catalog.validate(event_type, source.data_schema, payload)
