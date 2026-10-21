from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest

import framework.events.schema.catalog as catalog_module

from framework.events.canonical import BusinessContext
from framework.events.errors import EventCanonicalizationError, EventSchemaError, EventUnknownSchemaError
from framework.events.schema.catalog import (
    EventSchemaCatalog,
    EventSchemaRegistration,
    default_event_schema_catalog,
)
from framework.events.telemetry import default_event_telemetry


def test_default_catalog_has_no_workflow_event_or_operation_reader() -> None:
    catalog = default_event_schema_catalog()

    for event_type, data_schema in (
        ("workflow_started", "newsroom.workflow-event/v1"),
        ("run_operation_requested", "newsroom.workflow-operation/v1"),
    ):
        with pytest.raises(EventUnknownSchemaError):
            catalog.get(event_type, data_schema)
        with pytest.raises(EventUnknownSchemaError):
            catalog.current_schema(event_type)

    assert all(
        "workflow" not in registration.data_schema
        for registration in catalog.registrations()
    )


def test_business_context_accepts_only_graph_orchestration_identity() -> None:
    context = BusinessContext.from_dict(
        {
            "run_id": "run-1",
            "graph_id": "research.paper-analysis",
            "graph_version": "1",
            "graph_ref": "research.paper-analysis@1",
            "graph_checksum": "sha256:" + "a" * 64,
            "stage_id": "run_research_rag",
        }
    )

    assert context.graph_ref == "research.paper-analysis@1"
    assert context.stage_id == "run_research_rag"
    with pytest.raises(EventCanonicalizationError, match="workflow_id"):
        BusinessContext.from_dict({"workflow_id": "legacy"})
    with pytest.raises(EventCanonicalizationError, match="step_id"):
        BusinessContext.from_dict({"step_id": "legacy"})


def test_catalog_keeps_one_current_schema_per_graph_event_type() -> None:
    catalog = EventSchemaCatalog()
    registration = EventSchemaRegistration(
        event_type="graph_node_verified",
        data_schema="newsroom.graph-node-verified/v1",
        json_schema={
            "type": "object",
            "required": ["graph_ref", "graph_checksum", "stage_id"],
            "properties": {
                "graph_ref": {"type": "string"},
                "graph_checksum": {"type": "string"},
                "stage_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        current=True,
        authoritative_context_fields=(
            "run_id",
            "graph_id",
            "graph_version",
            "graph_ref",
            "graph_checksum",
            "stage_id",
        ),
    )
    catalog.register(registration)

    assert catalog.current_schema("graph_node_verified") == (
        "newsroom.graph-node-verified/v1"
    )
    assert catalog.validate(
        "graph_node_verified",
        "newsroom.graph-node-verified/v1",
        {
            "graph_ref": "research.paper-analysis@1",
            "graph_checksum": "sha256:" + "a" * 64,
            "stage_id": "run_research_rag",
        },
    )["stage_id"] == "run_research_rag"
    with pytest.raises(EventSchemaError):
        catalog.register(registration)



def _pure_custom_validator(payload: dict[str, object]) -> None:
    return None


class _FalseyTelemetry:
    def __init__(self) -> None:
        self.counters: list[tuple[str, dict[str, object]]] = []

    def __bool__(self) -> bool:
        return False

    def add_counter(self, name: str, *, labels: dict[str, object]) -> None:
        self.counters.append((name, dict(labels)))


def test_default_catalog_reuses_only_immutable_registrations_and_not_catalog_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_module, "_DEFAULT_SCHEMA_REGISTRATIONS", None)

    telemetry_one = default_event_telemetry()
    telemetry_two = default_event_telemetry()
    first = default_event_schema_catalog(telemetry=telemetry_one)
    second = default_event_schema_catalog(telemetry=telemetry_two)

    assert first is not second
    assert first._telemetry is telemetry_one
    assert second._telemetry is telemetry_two
    registration_one = first.get(
        "runtime_error", catalog_module.RUNTIME_EVENT_DATA_SCHEMA
    )
    registration_two = second.get(
        "runtime_error", catalog_module.RUNTIME_EVENT_DATA_SCHEMA
    )
    assert registration_one is registration_two
    assert isinstance(registration_one.json_schema, MappingProxyType)
    assert isinstance(registration_one.sensitivity_policy.field_rules, MappingProxyType)

    schema_copy = registration_one.schema_copy()
    schema_copy["properties"]["error"] = {"type": "string"}
    assert "error" not in registration_two.schema_copy()["properties"]

    first._registrations.clear()
    assert (
        second.get("runtime_error", catalog_module.RUNTIME_EVENT_DATA_SCHEMA)
        is registration_two
    )


def test_default_registration_cache_build_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_module, "_DEFAULT_SCHEMA_REGISTRATIONS", None)
    original_builder = catalog_module._build_default_event_schema_registrations
    build_calls = 0

    def tracked_builder() -> tuple[EventSchemaRegistration, ...]:
        nonlocal build_calls
        build_calls += 1
        return original_builder()

    monkeypatch.setattr(
        catalog_module,
        "_build_default_event_schema_registrations",
        tracked_builder,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        catalogs = list(
            executor.map(lambda _: default_event_schema_catalog(), range(8))
        )

    assert build_calls == 1
    assert all(
        catalog.get("runtime_error", catalog_module.RUNTIME_EVENT_DATA_SCHEMA)
        is catalogs[0].get("runtime_error", catalog_module.RUNTIME_EVENT_DATA_SCHEMA)
        for catalog in catalogs
    )


def test_custom_validator_is_not_admitted_to_default_registration_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = EventSchemaRegistration(
        event_type="custom_event",
        data_schema="newsroom.custom-event/v1",
        json_schema={"type": "object", "additionalProperties": False},
        custom_validator=_pure_custom_validator,
        current=True,
    )
    build_calls = 0

    def custom_builder() -> tuple[EventSchemaRegistration, ...]:
        nonlocal build_calls
        build_calls += 1
        return (registration,)

    monkeypatch.setattr(catalog_module, "_DEFAULT_SCHEMA_REGISTRATIONS", None)
    monkeypatch.setattr(
        catalog_module,
        "_build_default_event_schema_registrations",
        custom_builder,
    )

    first = default_event_schema_catalog()
    second = default_event_schema_catalog()

    assert build_calls == 2
    assert first.get("custom_event", "newsroom.custom-event/v1") is registration
    assert second.get("custom_event", "newsroom.custom-event/v1") is registration


def test_new_or_same_id_different_schema_is_strictly_validated_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_validator_for = catalog_module.validator_for
    seen_schemas: list[dict[str, object]] = []

    def tracked_validator_for(schema: object):
        assert isinstance(schema, dict)
        seen_schemas.append(schema)
        return original_validator_for(schema)

    monkeypatch.setattr(catalog_module, "validator_for", tracked_validator_for)
    EventSchemaRegistration(
        event_type="same_event",
        data_schema="newsroom.same-event/v1",
        json_schema={"type": "object"},
    )
    EventSchemaRegistration(
        event_type="same_event",
        data_schema="newsroom.same-event/v1",
        json_schema={"type": "array"},
    )

    with pytest.raises(EventSchemaError, match="invalid JSON schema"):
        EventSchemaRegistration(
            event_type="invalid_event",
            data_schema="newsroom.invalid-event/v1",
            json_schema={"type": "not-a-json-schema-type"},
        )

    assert [schema["type"] for schema in seen_schemas] == [
        "object",
        "array",
        "not-a-json-schema-type",
    ]



def test_catalog_preserves_a_valid_falsey_telemetry_instance() -> None:
    telemetry = _FalseyTelemetry()
    catalog = EventSchemaCatalog(telemetry=telemetry)  # type: ignore[arg-type]
    catalog.register(
        EventSchemaRegistration(
            event_type="falsey_telemetry_event",
            data_schema="newsroom.falsey-telemetry/v1",
            json_schema={"type": "object", "additionalProperties": False},
            current=True,
        )
    )

    assert catalog._telemetry is telemetry
    assert catalog.validate(
        "falsey_telemetry_event",
        "newsroom.falsey-telemetry/v1",
        {},
    ) == {}
    assert telemetry.counters == [
        (
            "event_schema_validation_total",
            {"event_type": "registered", "result": "success"},
        )
    ]
