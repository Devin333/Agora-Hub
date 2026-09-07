from __future__ import annotations

import copy
import dis
import inspect
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import CodeType, FunctionType, ModuleType
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from framework.events.errors import (
    EventContextConflictError,
    EventSchemaError,
    EventSchemaValidationError,
    EventUnknownSchemaError,
    EventQuarantineError,
    EventUpcastError,
)
from framework.events.schema.policy import (
    FieldDisposition,
    SensitivityPolicy,
    WholeDocumentReferenceDisposition,
)
from framework.shared.time import ensure_utc

if TYPE_CHECKING:
    from framework.events.canonical import BusinessContext
    from framework.events.telemetry import EventTelemetry


PayloadValidator = Callable[[Mapping[str, Any]], None]
EventUpcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]


_BUSINESS_CONTEXT_FIELDS = (
    "run_id",
    "graph_id",
    "graph_version",
    "graph_ref",
    "graph_checksum",
    "execution_identity",
    "stage_id",
    "task_id",
    "agent_id",
    "tool_call_id",
    "request_id",
)

# TaskPlan uses the same durable event stream as the rest of Harness.  The
# payload schema is intentionally reference-based; worker prompts and private
# result bodies stay in their dedicated stores.
TASK_PLAN_EVENT_SCHEMA_V2 = "newsroom.harness-task-plan-event/v2"
TASK_PLAN_EVENT_SCHEMA = TASK_PLAN_EVENT_SCHEMA_V2
TASK_PLAN_PARALLEL_EVENT_TYPES = (
    "TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED", "TASK_ATTEMPT_SPAWN_INTENT",
    "TASK_ATTEMPT_SPAWN_CONFIRMED", "TASK_ATTEMPT_SPAWN_UNKNOWN",
    "TASK_WAVE_DISPATCHED",
    "TASK_WAVE_COMPLETED", "TASK_GROUP_JOIN_WAITING", "TASK_GROUP_JOINED",
    "TASK_GROUP_FAILED", "TASK_GROUP_REPLAN_PENDING", "TASK_GROUP_CANCEL_REQUESTED",
    "TASK_GROUP_CANCELLED", "TASK_GROUP_INDETERMINATE", "TASK_GROUP_HALTED",
    "TASK_GROUP_SUPERSEDED", "TASK_GROUP_RECLAIMED", "TASK_GROUP_RECOVERY", "DEGRADED_SERIAL",
    "RECOVERY_STATUS_READ", "RECOVERY_RECONCILED", "RECOVERY_HALTED",
)
TASK_PLAN_EVENT_TYPES = (
    "PLAN_CANDIDATE_BUILT",
    "PLAN_CANDIDATE_REJECTED",
    "PLAN_VALIDATION_FAILED",
    "PLAN_ACCEPTED",
    "TASK_READY",
    "TASK_DISPATCHED",
    "TASK_STARTED",
    "TASK_RETRY_SCHEDULED",
    "TASK_RESULT_ACCEPTED",
    "TASK_RESULT_REJECTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_BLOCKED",
    "TASK_BLOCKED_UPSTREAM_FAILURE",
    "TASK_SKIPPED",
    "TASK_REPLACED",
    "PLAN_PATCH_PROPOSED",
    "PLAN_PATCH_REJECTED",
    "PLAN_PATCH_ACCEPTED",
    "STAGE_OUTPUT_AGGREGATED",
    "TASK_PLAN_VERIFIED",
    "TASK_PLAN_HALTED",
    *TASK_PLAN_PARALLEL_EVENT_TYPES,
)

# Runtime execution facts share one versioned payload schema.  The canonical
# event envelope still owns identity, ordering, security classification and
# durable checksums; this registry only admits the redacted fact body.
RUNTIME_EVENT_DATA_SCHEMA = "newsroom.runtime-event/v1"
RUNTIME_EVENT_TYPES = (
    "turn_started",
    "turn_stopped",
    "turn_aborted",
    "tool_requested",
    "approval_requested",
    "approval_decided",
    "execution_started",
    "execution_terminal",
    "child_spawned",
    "child_status",
    "child_heartbeat",
    "child_terminal",
    "context_compaction_planned",
    "context_compaction_committed",
    "context_compaction_rejected",
    "worker_heartbeat",
    "worker_status",
    "timeout",
    "cancel_requested",
    "cancellation_confirmed",
    "indeterminate",
    "runtime_error",
)

BUDGET_EVENT_DATA_SCHEMA = "newsroom.budget-event/v1"
BUDGET_EVENT_TYPES = (
    "budget_reservation_created",
    "budget_reservation_denied",
    "budget_reservation_settled",
    "budget_reservation_released",
    "budget_reservation_expired",
    "budget_reservation_indeterminate",
)


SUPPORTED_HISTORICAL_ENVELOPE_SCHEMAS = frozenset(
    {
        "newsroom.event-envelope/v2",
        "newsroom.event.v1",
        "newsroom.event_envelope.v1",
        "newsroom.event_record.v1",
    }
)


@dataclass(frozen=True)
class HistoricalSchemaResolution:
    event_type: str
    source_data_schema: str
    data_schema: str
    occurred_at: datetime
    payload: Mapping[str, Any]
    applied_upcasters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _required_text(self.event_type, "event_type"))
        object.__setattr__(
            self,
            "source_data_schema",
            _required_text(self.source_data_schema, "source_data_schema"),
        )
        object.__setattr__(self, "data_schema", _required_text(self.data_schema, "data_schema"))
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))
        if isinstance(self.applied_upcasters, (str, bytes)):
            raise TypeError("applied_upcasters must be a sequence of strings")
        object.__setattr__(
            self,
            "applied_upcasters",
            tuple(_required_text(item, "applied_upcaster") for item in self.applied_upcasters),
        )

    def payload_copy(self) -> dict[str, Any]:
        return _thaw_mapping(self.payload)


@dataclass(frozen=True)
class EventSchemaRegistration:
    event_type: str
    data_schema: str
    json_schema: Mapping[str, Any]
    sensitivity_policy: SensitivityPolicy = field(default_factory=SensitivityPolicy)
    upcast_to: str | None = None
    upcaster: EventUpcaster | None = None
    custom_validator: PayloadValidator | None = None
    current: bool = False
    authoritative_context_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        event_type = _required_text(self.event_type, "event_type")
        data_schema = _required_text(self.data_schema, "data_schema")
        upcast_to = _optional_text(self.upcast_to)
        if (upcast_to is None) != (self.upcaster is None):
            raise ValueError("upcast_to and upcaster must be configured together")
        if not isinstance(self.current, bool):
            raise TypeError("current must be a bool")
        if isinstance(self.authoritative_context_fields, (str, bytes)):
            raise TypeError("authoritative_context_fields must be a sequence")
        context_fields = tuple(self.authoritative_context_fields)
        if len(context_fields) != len(set(context_fields)):
            raise EventSchemaError("authoritative context fields must be unique")
        unknown_context_fields = sorted(
            set(context_fields) - set(_BUSINESS_CONTEXT_FIELDS)
        )
        if unknown_context_fields:
            raise EventSchemaError(
                "unknown authoritative context field: " + unknown_context_fields[0]
            )
        if not isinstance(self.sensitivity_policy, SensitivityPolicy):
            raise TypeError("sensitivity_policy must be SensitivityPolicy")
        if upcast_to is not None:
            if self.current:
                raise EventSchemaError("a current event schema cannot declare an upcaster")
            _validate_adjacent_schema_versions(data_schema, upcast_to)
            _validate_upcaster_purity(self.upcaster, event_type, data_schema)
        if self.custom_validator is not None:
            _validate_custom_validator_purity(
                self.custom_validator,
                event_type,
                data_schema,
            )
        if not isinstance(self.json_schema, Mapping):
            raise TypeError("json_schema must be a mapping")
        schema = copy.deepcopy(dict(self.json_schema))
        try:
            validator_cls = validator_for(schema)
            validator_cls.check_schema(schema)
        except SchemaError as exc:
            raise EventSchemaError(
                f"invalid JSON schema for {event_type} ({data_schema})"
            ) from exc
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "data_schema", data_schema)
        object.__setattr__(self, "upcast_to", upcast_to)
        object.__setattr__(self, "json_schema", _freeze_mapping(schema))
        object.__setattr__(
            self,
            "authoritative_context_fields",
            context_fields,
        )

    def schema_copy(self) -> dict[str, Any]:
        return _thaw_mapping(self.json_schema)


class EventSchemaCatalog:
    """Deterministic registry for event payload schemas and adjacent upcasters."""

    def __init__(self, *, telemetry: EventTelemetry | None = None) -> None:
        self._registrations: dict[tuple[str, str], EventSchemaRegistration] = {}
        self._current: dict[str, str] = {}
        self._telemetry = telemetry or _default_schema_telemetry()

    def register(self, registration: EventSchemaRegistration) -> None:
        key = (registration.event_type, registration.data_schema)
        if key in self._registrations:
            raise EventSchemaError(
                f"duplicate event schema registration: {registration.event_type} "
                f"({registration.data_schema})"
            )
        if registration.current and registration.event_type in self._current:
            raise EventSchemaError(
                f"multiple current schemas for event type: {registration.event_type}"
            )
        if registration.upcast_to == registration.data_schema:
            raise EventSchemaError("event schema cannot upcast to itself")
        self._registrations[key] = registration
        if registration.current:
            self._current[registration.event_type] = registration.data_schema

    def get(self, event_type: str, data_schema: str) -> EventSchemaRegistration:
        key = (
            _required_text(event_type, "event_type"),
            _required_text(data_schema, "data_schema"),
        )
        registration = self._registrations.get(key)
        if registration is None:
            raise EventUnknownSchemaError(*key)
        return registration

    def current_schema(self, event_type: str) -> str:
        event_type = _required_text(event_type, "event_type")
        current = self._current.get(event_type)
        if current is None:
            raise EventUnknownSchemaError(event_type, "<current>")
        return current

    def registrations(self) -> tuple[EventSchemaRegistration, ...]:
        return tuple(
            self._registrations[key]
            for key in sorted(self._registrations, key=lambda item: (item[0], item[1]))
        )

    def validate(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        registered = (
            isinstance(event_type, str)
            and isinstance(data_schema, str)
            and (event_type, data_schema) in self._registrations
        )
        try:
            validated = self._validate_payload(event_type, data_schema, payload)
        except Exception:
            self._record_schema_validation(registered=registered, result="invalid")
            raise
        self._record_schema_validation(registered=True, result="success")
        return validated

    def _validate_payload(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        registration = self.get(event_type, data_schema)
        canonicalization_failed = False
        snapshot: dict[str, Any] | None = None
        try:
            snapshot = _thaw_mapping(_freeze_mapping(payload))
        except (TypeError, ValueError):
            canonicalization_failed = True
        if canonicalization_failed:
            raise EventSchemaValidationError(
                event_type=registration.event_type,
                data_schema=registration.data_schema,
                path="$",
                rule="canonical_json",
            ) from None
        if snapshot is None:  # pragma: no cover - freeze returns or raises
            raise AssertionError("schema payload canonicalization returned no value")
        schema = registration.schema_copy()
        validator_cls = validator_for(schema)
        validator = validator_cls(schema)
        first_error = next(
            iter(
                sorted(
                    validator.iter_errors(snapshot),
                    key=lambda item: (_json_path(item), str(item.validator)),
                )
            ),
            None,
        )
        if first_error is not None:
            raise _validation_error(registration, first_error)
        if registration.custom_validator is not None:
            custom_failure: EventSchemaValidationError | None = None
            try:
                _run_pure_validator(registration.custom_validator, snapshot)
            except EventSchemaValidationError as error:
                custom_failure = EventSchemaValidationError(
                    event_type=error.event_type,
                    data_schema=error.data_schema,
                    path=error.path,
                    rule=error.rule,
                )
            except Exception:
                custom_failure = EventSchemaValidationError(
                    event_type=registration.event_type,
                    data_schema=registration.data_schema,
                    path="$",
                    rule="custom",
                )
            if custom_failure is not None:
                raise custom_failure from None
        return snapshot

    def prepare_publish_payload(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
        *,
        business_context: BusinessContext,
    ) -> dict[str, Any]:
        """Return the detached canonical payload accepted for publication.

        Graph event inputs may repeat fields that are authoritative in
        ``BusinessContext``. Equal duplicates are removed before schema
        validation; a conflicting or payload-only value fails with the typed
        context error instead of silently choosing one authority.
        """

        from framework.events.canonical import BusinessContext

        registration = self.get(event_type, data_schema)
        if not isinstance(business_context, BusinessContext):
            raise TypeError("business_context must be BusinessContext")
        try:
            snapshot = _thaw_mapping(_freeze_mapping(payload))
        except (TypeError, ValueError):
            raise EventSchemaValidationError(
                event_type=registration.event_type,
                data_schema=registration.data_schema,
                path="$",
                rule="canonical_json",
            ) from None

        for field_name in registration.authoritative_context_fields:
            if field_name not in snapshot:
                continue
            if snapshot[field_name] != getattr(business_context, field_name):
                raise EventContextConflictError(field_name)
            del snapshot[field_name]

        return self.validate(event_type, data_schema, snapshot)

    def upcast(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
        *,
        target_schema: str | None = None,
    ) -> tuple[str, dict[str, Any], tuple[str, ...]]:
        source_schema = data_schema if isinstance(data_schema, str) else "unknown"
        desired_schema = target_schema if isinstance(target_schema, str) else None
        if desired_schema is None:
            try:
                desired_schema = self.current_schema(event_type)
            except Exception:
                desired_schema = "unknown"
        registered = (
            isinstance(event_type, str)
            and (event_type, source_schema) in self._registrations
        )
        try:
            result = self._upcast(
                event_type,
                data_schema,
                payload,
                target_schema=target_schema,
            )
        except Exception:
            self._record_upcast(
                registered=registered,
                source_schema=source_schema,
                target_schema=desired_schema,
                result="failed",
            )
            raise
        for transition in result[2]:
            source, separator, target = transition.partition("->")
            self._record_upcast(
                registered=True,
                source_schema=source,
                target_schema=target if separator else result[0],
                result="success",
            )
        return result

    def _upcast(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
        *,
        target_schema: str | None = None,
    ) -> tuple[str, dict[str, Any], tuple[str, ...]]:
        desired = target_schema or self.current_schema(event_type)
        current_schema = str(data_schema)
        current_payload = self.validate(event_type, current_schema, payload)
        applied: list[str] = []
        visited: set[str] = set()

        while current_schema != desired:
            if current_schema in visited:
                raise EventUpcastError(
                    f"event upcast cycle: {event_type} ({current_schema})"
                )
            visited.add(current_schema)
            registration = self.get(event_type, current_schema)
            if registration.upcast_to is None or registration.upcaster is None:
                raise EventUpcastError(
                    f"no event upcast path: {event_type} ({current_schema} -> {desired})"
                )
            upcaster_failed = False
            next_payload: dict[str, Any] | None = None
            try:
                next_payload = _run_pure_upcaster(registration.upcaster, current_payload)
            except Exception:
                upcaster_failed = True
            if upcaster_failed or next_payload is None:
                raise EventUpcastError(
                    f"event upcaster failed: {event_type} ({current_schema})"
                ) from None
            next_schema = registration.upcast_to
            output_validation_failed = False
            try:
                self.get(event_type, next_schema)
                current_payload = self.validate(event_type, next_schema, next_payload)
            except (EventUnknownSchemaError, EventSchemaValidationError):
                output_validation_failed = True
            if output_validation_failed:
                raise EventUpcastError(
                    f"event upcaster output failed validation: "
                    f"{event_type} ({current_schema} -> {next_schema})"
                ) from None
            applied.append(f"{current_schema}->{next_schema}")
            current_schema = next_schema

        return current_schema, current_payload, tuple(applied)

    def _record_schema_validation(self, *, registered: bool, result: str) -> None:
        self._telemetry.add_counter(
            "event_schema_validation_total",
            labels={
                "event_type": "registered" if registered else "unknown",
                "result": result,
            },
        )

    def _record_upcast(
        self,
        *,
        registered: bool,
        source_schema: str,
        target_schema: str,
        result: str,
    ) -> None:
        self._telemetry.add_counter(
            "event_upcast_total",
            labels={
                "event_type": "registered" if registered else "unknown",
                "from": _schema_version_metric_bucket(source_schema),
                "to": _schema_version_metric_bucket(target_schema),
                "result": result,
            },
        )

    def resolve_historical(
        self,
        event_type: str,
        data_schema: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: str | datetime | None,
        envelope_schema: str | None = None,
        source: str | None = None,
        target_schema: str | None = None,
    ) -> HistoricalSchemaResolution:
        """Resolve a historical payload without inventing time or schema identity.

        The migration layer persists the quarantine record.  This method owns the
        deterministic classification used by import, replay, and verification.
        It intentionally returns no raw diagnostic or source payload on failure.
        """

        if envelope_schema is not None:
            normalized_envelope = _required_text(envelope_schema, "envelope_schema")
            if normalized_envelope not in SUPPORTED_HISTORICAL_ENVELOPE_SCHEMAS:
                raise EventQuarantineError("unknown_envelope_schema", source=source)

        if occurred_at is None or (isinstance(occurred_at, str) and not occurred_at.strip()):
            raise EventQuarantineError("missing_occurred_at", source=source)
        if not isinstance(occurred_at, (str, datetime)):
            raise EventQuarantineError("invalid_occurred_at", source=source)
        historical_time_invalid = False
        parsed_time: datetime | None = None
        try:
            parsed_time = _parse_historical_time(occurred_at)
        except (TypeError, ValueError, OverflowError):
            historical_time_invalid = True
        if historical_time_invalid or parsed_time is None:
            raise EventQuarantineError("invalid_occurred_at", source=source) from None

        schema_is_unknown = False
        try:
            self.get(event_type, data_schema)
        except EventUnknownSchemaError:
            schema_is_unknown = True
        if schema_is_unknown:
            raise EventQuarantineError("unknown_data_schema", source=source) from None
        historical_failure: str | None = None
        resolution: tuple[str, dict[str, Any], tuple[str, ...]] | None = None
        try:
            resolution = self.upcast(
                event_type,
                data_schema,
                payload,
                target_schema=target_schema,
            )
        except EventSchemaValidationError:
            historical_failure = "schema_validation_failed"
        except EventUpcastError:
            historical_failure = "upcast_failed"
        if historical_failure is not None:
            raise EventQuarantineError(historical_failure, source=source) from None
        if resolution is None:  # pragma: no cover - upcast returns or raises
            raise AssertionError("historical schema resolution returned no value")
        resolved_schema, resolved_payload, applied = resolution

        return HistoricalSchemaResolution(
            event_type=_required_text(event_type, "event_type"),
            source_data_schema=_required_text(data_schema, "data_schema"),
            data_schema=resolved_schema,
            occurred_at=parsed_time,
            payload=resolved_payload,
            applied_upcasters=applied,
        )


ATTEMPT_EVENT_DATA_SCHEMA = "newsroom.attempt-event/v1"
ATTEMPT_EVENT_TYPES = (
    "attempt_admission_rejected",
    "attempt_started",
    "attempt_terminal",
)

HARNESS_GRAPH_EVENT_DATA_SCHEMA = "newsroom.harness-graph-event/v1"
HARNESS_GRAPH_EVENT_ALIASES = (
    "run_created",
    "run_state_changed",
    "step_state_changed",
    "phase_recorded",
    "graph_phase_transition_recorded",
    "decision_recorded",
    "graph_worker_called",
    "graph_worker_result_recorded",
    "budget_fact_recorded",
    "gate_evaluated",
    "checkpoint_created",
    "context_compaction_planned",
    "context_compaction_action_applied",
    "context_summary_candidate_created",
    "context_compaction_verified",
    "context_compaction_rejected",
)

HARNESS_GRAPH_COMMIT_DATA_SCHEMA = "newsroom.harness-graph-control-commit/v1"
HARNESS_GRAPH_PROJECTION_RECORD_DATA_SCHEMA = (
    "newsroom.harness-graph-projection-record/v2"
)
HARNESS_GRAPH_COMMIT_EVENT_TYPES = (
    "harness_graph_initialized",
    "harness_graph_decision_committed",
    "harness_graph_projection_committed",
    "harness_graph_activity_result_committed",
    "harness_graph_observation_committed",
)
HARNESS_GRAPH_TRANSITION_EVENT_SCHEMAS: Mapping[str, str] = MappingProxyType(
    {
        "harness_graph_created": "newsroom.harness-graph-created/v1",
        "harness_graph_decision_committed": "newsroom.harness-graph-decision/v1",
        "harness_graph_node_activated": "newsroom.harness-graph-node-activated/v1",
        "harness_graph_node_terminal": "newsroom.harness-graph-node-terminal/v1",
        "harness_graph_choice_selected": "newsroom.harness-graph-choice-selected/v1",
        "harness_graph_fork_opened": "newsroom.harness-graph-fork-opened/v1",
        "harness_graph_join_satisfied": "newsroom.harness-graph-join-satisfied/v1",
        "harness_graph_loop_transitioned": "newsroom.harness-graph-loop-transition/v1",
        "harness_graph_wait_transitioned": "newsroom.harness-graph-wait-transition/v1",
        "harness_graph_winner_selected": "newsroom.harness-graph-winner-selected/v1",
        "harness_graph_cancellation_transitioned": "newsroom.harness-graph-cancellation-transition/v1",
        "harness_graph_compensation_transitioned": "newsroom.harness-graph-compensation-transition/v1",
        "harness_graph_budget_transitioned": "newsroom.harness-graph-budget-transition/v1",
        "harness_graph_run_lifecycle_transitioned": "newsroom.harness-graph-run-lifecycle-transition/v1",
    }
)


def default_event_schema_catalog(
    *,
    telemetry: EventTelemetry | None = None,
) -> EventSchemaCatalog:
    catalog = EventSchemaCatalog(telemetry=telemetry)
    for event_type in BUDGET_EVENT_TYPES:
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=BUDGET_EVENT_DATA_SCHEMA,
                json_schema=_budget_event_payload_schema(),
                sensitivity_policy=SensitivityPolicy(),
                current=True,
                authoritative_context_fields=("run_id",),
            )
        )
    for event_type in ATTEMPT_EVENT_TYPES:
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=ATTEMPT_EVENT_DATA_SCHEMA,
                json_schema=_attempt_payload_schema(event_type),
                sensitivity_policy=SensitivityPolicy(),
                current=True,
                authoritative_context_fields=_BUSINESS_CONTEXT_FIELDS,
            )
        )
    for event_type in HARNESS_GRAPH_EVENT_ALIASES:
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=HARNESS_GRAPH_EVENT_DATA_SCHEMA,
                json_schema=_harness_payload_schema(event_type),
                sensitivity_policy=_harness_sensitivity_policy(event_type),
                current=True,
            )
        )
    for event_type in HARNESS_GRAPH_COMMIT_EVENT_TYPES:
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=HARNESS_GRAPH_COMMIT_DATA_SCHEMA,
                json_schema=_harness_graph_commit_payload_schema(event_type),
                sensitivity_policy=SensitivityPolicy(),
                current=event_type != "harness_graph_projection_committed",
            )
        )
    catalog.register(
        EventSchemaRegistration(
            event_type="harness_graph_projection_committed",
            data_schema=HARNESS_GRAPH_PROJECTION_RECORD_DATA_SCHEMA,
            json_schema=_harness_graph_projection_record_payload_schema(),
            sensitivity_policy=SensitivityPolicy(),
            current=True,
        )
    )
    for event_type, data_schema in HARNESS_GRAPH_TRANSITION_EVENT_SCHEMAS.items():
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=data_schema,
                json_schema=_harness_graph_transition_payload_schema(data_schema),
                sensitivity_policy=SensitivityPolicy(),
                # Decision commits keep the canonical control-commit schema as
                # the writer.  Its logical decision schema remains explicitly
                # readable for bounded history migration.
                current=event_type not in HARNESS_GRAPH_COMMIT_EVENT_TYPES,
            )
        )
    for event_type in TASK_PLAN_EVENT_TYPES:
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=TASK_PLAN_EVENT_SCHEMA_V2,
                json_schema=_task_plan_event_payload_schema(
                    event_type,
                    data_schema=TASK_PLAN_EVENT_SCHEMA_V2,
                ),
                sensitivity_policy=SensitivityPolicy(),
                current=True,
            )
        )
    for event_type in RUNTIME_EVENT_TYPES:
        # These two logical facts already have a canonical Harness graph event
        # schema.  Reusing that registration avoids two current schemas for a
        # single event type.
        if event_type in {"context_compaction_planned", "context_compaction_rejected"}:
            continue
        catalog.register(
            EventSchemaRegistration(
                event_type=event_type,
                data_schema=RUNTIME_EVENT_DATA_SCHEMA,
                json_schema=_runtime_event_payload_schema(),
                sensitivity_policy=SensitivityPolicy(),
                current=True,
            )
        )
    return catalog


def _runtime_event_payload_schema() -> dict[str, Any]:
    # Runtime metadata is a bounded, recursively scalar/reference-shaped
    # object.  The negative key guard keeps complete prompts, tool payloads,
    # and secret-bearing fields out even when a caller bypasses the framework
    # RuntimeEventEnvelope redactor and publishes through EventRuntime.
    safe_key = (
        r"^(?!(?i:.*(?:secret|token|password|credential|private[_-]?key|"
        r"api[_-]?key|authorization|cookie|prompt|raw_payload|file_content).*))"
        r"(?!(?i:arguments?|payload|output|observation|task|feedback|verdict|"
        r"stream_event|text_delta|content|raw)$)"
        r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}$"
    )
    safe_value = {
        "anyOf": [
            {"type": ["string", "number", "boolean", "null"], "maxLength": 4096},
            {"type": "array", "maxItems": 128, "items": {"$ref": "#/$defs/safeValue"}},
            {"$ref": "#/$defs/safeObject"},
        ]
    }
    safe_object = {
        "type": "object",
        "maxProperties": 64,
        "patternProperties": {safe_key: {"$ref": "#/$defs/safeValue"}},
        "additionalProperties": False,
    }
    graph_identity = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "run_id",
                    "graph_id",
                    "graph_version",
                    "graph_ref",
                    "graph_checksum",
                    "node_id",
                    "node_instance_id",
                    "activity_id",
                    "attempt",
                ],
                "properties": {
                    "run_id": {"type": "string", "minLength": 1, "maxLength": 512},
                    "graph_id": {"type": "string", "minLength": 1, "maxLength": 512},
                    "graph_version": {"type": "string", "minLength": 1, "maxLength": 128},
                    "graph_ref": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "graph_checksum": _CHECKSUM_TEXT,
                    "node_id": {"type": "string", "minLength": 1, "maxLength": 512},
                    "node_instance_id": {"type": "string", "minLength": 1, "maxLength": 512},
                    "activity_id": {"type": "string", "minLength": 1, "maxLength": 512},
                    "attempt": {"type": "integer", "minimum": 1},
                },
            },
        ]
    }
    return {
        "type": "object",
        "$defs": {
            "safeValue": safe_value,
            "safeObject": safe_object,
        },
        "additionalProperties": False,
        "required": ["schema_version", "identity", "refs", "checksums", "metadata", "source"],
        "properties": {
            "schema_version": {"const": RUNTIME_EVENT_DATA_SCHEMA},
            "event_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "event_type": {"type": "string", "minLength": 1, "maxLength": 128},
            "occurred_at": {"type": "string", "minLength": 1, "maxLength": 128},
            "identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["graph_identity", "activity_id", "attempt_id", "node_id", "node_instance_id"],
                "properties": {
                    "graph_identity": graph_identity,
                    "activity_id": {"type": ["string", "null"], "maxLength": 512},
                    "attempt_id": {"type": ["string", "null"], "maxLength": 512},
                    "node_id": {"type": ["string", "null"], "maxLength": 512},
                    "node_instance_id": {"type": ["string", "null"], "maxLength": 512},
                },
            },
            "status": {"type": ["string", "null"], "maxLength": 128},
            "reason_code": {"type": ["string", "null"], "maxLength": 256},
            "sequence": {"type": ["integer", "null"], "minimum": 1},
            "stream_id": {"type": ["string", "null"], "maxLength": 512},
            "refs": {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 2048}},
            "checksums": {
                "type": "object",
                "maxProperties": 64,
                "additionalProperties": _CHECKSUM_TEXT,
            },
            "metadata": {"$ref": "#/$defs/safeObject"},
            "source": {"type": "string", "minLength": 1, "maxLength": 512},
        },
    }


def _budget_event_payload_schema() -> dict[str, Any]:
    amount_properties = {
        "llm_calls": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
        "input_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
        "output_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
        "reasoning_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
        "cached_input_tokens": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
        "estimated_cost_usd": {
            "type": "string",
            "pattern": r"^(0|[1-9][0-9]*)(\.[0-9]{12})$",
        },
    }
    amount_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(amount_properties),
        "properties": amount_properties,
    }
    scope_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "run_id",
            "scope_id",
            "scope_type",
            "parent_scope_id",
            "policy_revision",
        ],
        "properties": {
            "run_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "scope_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "scope_type": {
                "enum": ["run", "graph", "agent_loop", "subagent", "operation"]
            },
            "parent_scope_id": {
                "type": ["string", "null"],
                "maxLength": 512,
            },
            "policy_revision": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "execution_identity": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "run_id",
                            "graph_id",
                            "graph_version",
                            "graph_ref",
                            "graph_checksum",
                        ],
                        "properties": {
                            "run_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_version": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_ref": {"type": "string", "minLength": 1, "maxLength": 1025},
                            "graph_checksum": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "run_id",
                            "graph_id",
                            "graph_version",
                            "graph_ref",
                            "graph_checksum",
                            "node_id",
                            "node_instance_id",
                            "activity_id",
                            "attempt",
                        ],
                        "properties": {
                            "run_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_version": {"type": "string", "minLength": 1, "maxLength": 512},
                            "graph_ref": {"type": "string", "minLength": 1, "maxLength": 1025},
                            "graph_checksum": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                            "node_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "node_instance_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "activity_id": {"type": "string", "minLength": 1, "maxLength": 512},
                            "attempt": {"type": "integer", "minimum": 1},
                        },
                    },
                ]
            },
        },
    }
    checksum_schema = {
        "type": "string",
        "pattern": r"^sha256:[0-9a-f]{64}$",
    }
    reservation_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "reservation_id",
            "operation_id",
            "idempotency_key",
            "scope",
            "policy_digest",
            "requested",
            "status",
            "created_event_id",
            "created_at_epoch_ms",
        ],
        "properties": {
            "reservation_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 512},
            "scope": scope_schema,
            "policy_digest": checksum_schema,
            "requested": amount_schema,
            "status": {
                "enum": ["reserved", "settled", "released", "expired", "indeterminate"]
            },
            "created_event_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "created_at_epoch_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2**63 - 1,
            },
        },
    }
    settlement_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "reservation_id",
            "operation_id",
            "scope",
            "policy_digest",
            "actual",
            "request_dispatched",
            "cache_hit",
            "outcome",
            "settled_event_id",
            "reason_code",
        ],
        "properties": {
            "reservation_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "scope": scope_schema,
            "policy_digest": checksum_schema,
            "actual": amount_schema,
            "request_dispatched": {"type": "boolean"},
            "cache_hit": {"type": "boolean"},
            "outcome": {
                "enum": ["succeeded", "failed", "cancelled", "indeterminate"]
            },
            "settled_event_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "reason_code": {
                "type": ["string", "null"],
                "maxLength": 512,
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "scope",
            "policy_digest",
            "ledger_revision",
            "operation_id",
            "idempotency_key",
            "reservation_id",
            "amounts",
            "reason_codes",
            "outcome",
            "reservation",
            "settlement",
        ],
        "properties": {
            "scope": scope_schema,
            "policy_digest": checksum_schema,
            "ledger_revision": {"type": "integer", "minimum": 1},
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 512},
            "reservation_id": {"type": ["string", "null"], "maxLength": 512},
            "amounts": amount_schema,
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "maxItems": 16,
                "uniqueItems": True,
            },
            "outcome": {
                "anyOf": [
                    {
                        "enum": [
                            "reserved",
                            "denied",
                            "succeeded",
                            "failed",
                            "cancelled",
                            "released",
                            "expired",
                            "indeterminate",
                        ]
                    },
                    {"type": "null"},
                ]
            },
            "reservation": {"anyOf": [reservation_schema, {"type": "null"}]},
            "settlement": {"anyOf": [settlement_schema, {"type": "null"}]},
        },
    }


_TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
_NULLABLE_TEXT = {"type": ["string", "null"], "maxLength": 4096}
_OBJECT = {"type": "object", "maxProperties": 128}
_ARRAY_OF_TEXT = {"type": "array", "items": _TEXT, "maxItems": 4096}
_NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}
_POSITIVE_INTEGER = {"type": "integer", "minimum": 1}
_NUMBER = {"type": "number"}
_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NONNEGATIVE_NUMBER = {"type": "number", "minimum": 0}
_POSITIVE_NUMBER = {"type": "number", "exclusiveMinimum": 0}
_NULLABLE_POSITIVE_NUMBER = {
    "anyOf": [_POSITIVE_NUMBER, {"type": "null"}],
}
_BOOLEAN = {"type": "boolean"}
_CHECKSUM_TEXT = {
    "type": "string",
    "pattern": "^sha256:[0-9a-f]{64}$",
}
_ARRAY_OF_CHECKSUMS = {
    "type": "array",
    "items": _CHECKSUM_TEXT,
    "maxItems": 4096,
}


def _harness_graph_transition_payload_schema(data_schema: str) -> dict[str, Any]:
    """Reference-only schema for one logical graph transition projection."""

    return {
        "type": "object",
        "maxProperties": 10,
        "properties": {
            "schema_version": {"const": data_schema},
            "transition_type": _TEXT,
            "graph_checksum": _CHECKSUM_TEXT,
            "projection_checksum": _CHECKSUM_TEXT,
            "cause_checksum": {
                "anyOf": [_CHECKSUM_TEXT, {"type": "null"}],
            },
            "node_instance_id": {
                "anyOf": [_TEXT, {"type": "null"}],
            },
            "attempt": {
                "anyOf": [_NONNEGATIVE_INTEGER, {"type": "null"}],
            },
            "evidence_refs": _ARRAY_OF_CHECKSUMS,
            "payload_refs": {
                "type": "object",
                "maxProperties": 64,
                "additionalProperties": _CHECKSUM_TEXT,
            },
            "diagnostic_refs": _ARRAY_OF_CHECKSUMS,
        },
        "required": [
            "schema_version",
            "transition_type",
            "graph_checksum",
            "projection_checksum",
            "cause_checksum",
            "node_instance_id",
            "attempt",
            "evidence_refs",
            "payload_refs",
            "diagnostic_refs",
        ],
        "additionalProperties": False,
    }


def _candidate_submission_schema() -> dict[str, Any]:
    identity_fields = {
        "schema_version": {"const": "newsroom.harness-candidate-dedup-identity/v1"},
        **{name: _TEXT for name in ("run_id", "stage_id", "parent_turn_id", "action_correlation_id")},
        "dedup_key": _CHECKSUM_TEXT,
    }
    fields = {
        "schema_version": {"const": "newsroom.harness-candidate-submission/v2"},
        "identity": {
            "type": "object", "additionalProperties": False,
            "required": list(identity_fields), "properties": identity_fields,
        },
        "candidate_checksum": _CHECKSUM_TEXT,
        "candidate_ref": _CHECKSUM_TEXT,
        "accepted_at": _TEXT,
        "admission_id": _TEXT,
        "submission_id": _TEXT,
        "plan_id": _TEXT,
        "record_checksum": _CHECKSUM_TEXT,
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": list(fields), "properties": fields,
    }


def _task_plan_terminal_result_schema() -> dict[str, Any]:
    fields = {
        "status": {"enum": ["succeeded", "failed", "blocked"]},
        "output": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "aggregate_ref": _TEXT,
                "aggregate_checksum": _CHECKSUM_TEXT,
                "output_refs_by_role": {
                    "type": "object", "additionalProperties": _TEXT, "maxProperties": 128,
                },
                "analysis_branch_refs": {
                    "type": "array", "items": {"type": "object", "maxProperties": 16}, "maxItems": 128,
                },
            },
        },
        "artifacts": {"type": "array", "maxItems": 0},
        "metrics": {"type": "object", "maxProperties": 0},
        "diagnostics": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "plan_id": _TEXT, "plan_version": _POSITIVE_INTEGER,
                "projection_checksum": _CHECKSUM_TEXT, "reason_code": _TEXT,
            },
        },
        "error": {"anyOf": [_TEXT, {"type": "null"}]},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": list(fields), "properties": fields,
    }


def _parallel_task_plan_details_schema(event_type: str) -> dict[str, Any]:
    def object_schema(fields: dict[str, Any], required=None) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": fields, "required": list(fields) if required is None else required,
        }

    non_negative = {"type": "integer", "minimum": 0}
    budget = {"type": "object", "additionalProperties": non_negative, "maxProperties": 16}
    capacity_allocations = {
        "type": "object", "additionalProperties": {"type": "integer", "minimum": 1},
        "minProperties": 1, "maxProperties": 16,
    }
    nullable_text = {"anyOf": [_TEXT, {"type": "null"}]}
    nullable_checksum = {"anyOf": [_CHECKSUM_TEXT, {"type": "null"}]}
    group_state = {"enum": [
        "PLANNED", "ADMITTED", "DISPATCHING", "RUNNING", "JOINING", "REPLAN_PENDING",
        "SUCCEEDED", "FAILED", "CANCELLED", "INDETERMINATE", "HALTED", "SUPERSEDED",
    ]}
    reservation_state = {"enum": ["RESERVED", "CONSUMED", "RELEASED"]}
    wave_terminal_outcome = {"enum": [
        "SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED", "INDETERMINATE",
        "RECLAIMED", "DEADLINE_EXCEEDED",
    ]}
    reservation = object_schema({
        "schema_version": {"const": "agora.harness-task-reservation/v1"},
        "task_id": _TEXT, "idempotency_key": _TEXT, "budget": budget,
        "capacity_allocations": capacity_allocations,
        "capacity_policy_checksums": {
            "type": "object", "minProperties": 1,
            "additionalProperties": _CHECKSUM_TEXT, "maxProperties": 16,
        },
        "state": reservation_state, "reservation_checksum": _CHECKSUM_TEXT,
    }, required=["schema_version", "task_id", "idempotency_key", "budget", "state", "reservation_checksum"])
    reservation["dependentRequired"] = {
        "capacity_allocations": ["capacity_policy_checksums"],
        "capacity_policy_checksums": ["capacity_allocations"],
    }
    group = object_schema({
        "schema_version": {"const": "agora.harness-dispatch-group/v2"},
        "group_id": _TEXT, "group_checksum": _CHECKSUM_TEXT,
        "run_id": _TEXT, "stage_id": _TEXT, "plan_id": _TEXT,
        "plan_checksum": _CHECKSUM_TEXT,
        "policy_ref": _TEXT, "policy_checksum": _CHECKSUM_TEXT,
        "admission_policy_checksum": _CHECKSUM_TEXT,
        "parent_graph_identity": object_schema({
            "run_id": _TEXT, "graph_id": _TEXT, "graph_version": _TEXT,
            "graph_ref": _TEXT, "graph_checksum": _CHECKSUM_TEXT,
            "node_id": _TEXT, "node_instance_id": _TEXT,
            "activity_id": _TEXT, "attempt": _POSITIVE_INTEGER,
        }),
        "plan_version": _POSITIVE_INTEGER, "task_ids": _ARRAY_OF_TEXT,
        "required_output_roles": _ARRAY_OF_TEXT, "join_policy": {"enum": ["wait_all", "fail_fast"]},
        "max_waves": _POSITIVE_INTEGER, "max_parallelism": _POSITIVE_INTEGER,
        "budget_envelope": budget, "correlation_id": _TEXT, "state": group_state,
    })
    wave = object_schema({
        "schema_version": {"const": "agora.harness-dispatch-wave/v3"},
        "wave_id": _TEXT, "group_id": _TEXT, "ordinal": _POSITIVE_INTEGER,
        "task_ids": _ARRAY_OF_TEXT, "effective_parallelism": _POSITIVE_INTEGER,
        "execution_mode": {"enum": ["SUPERVISED", "SERIAL", "INLINE_TEST"]},
        "reservations": {"type": "array", "items": reservation, "maxItems": 128},
        "state": {"enum": ["PLANNED", "ADMITTED", "DISPATCHING", "RUNNING", "TERMINAL"]},
        "terminal_outcome": {"anyOf": [wave_terminal_outcome, {"type": "null"}]},
    })
    wave["allOf"] = [
        {
            "if": {"properties": {"state": {"const": "TERMINAL"}}},
            "then": {"properties": {"terminal_outcome": wave_terminal_outcome}},
            "else": {"properties": {"terminal_outcome": {"type": "null"}}},
        }
    ]
    task_summary = object_schema({
        "task_id": _TEXT, "status": _TEXT, "attempt": _NONNEGATIVE_INTEGER,
        "result_ref": nullable_text, "checksum": _CHECKSUM_TEXT,
        "output_roles": _ARRAY_OF_TEXT,
        "summary": {"type": "string", "maxLength": 65536},
        "summary_truncated": {"type": "boolean"}, "summary_checksum": _CHECKSUM_TEXT,
    }, required=["task_id", "status", "attempt", "result_ref", "checksum", "output_roles"])
    wave_summary = object_schema({
        "wave_id": _TEXT, "ordinal": _POSITIVE_INTEGER, "task_ids": _ARRAY_OF_TEXT,
        "status": {"enum": ["PLANNED", "ADMITTED", "DISPATCHING", "RUNNING", "TERMINAL"]},
    })
    observation = object_schema({
        "group_id": _TEXT, "group_status": group_state, "plan_version": _POSITIVE_INTEGER,
        "waves": {"type": "array", "items": wave_summary, "maxItems": 128},
        "tasks": {"type": "array", "items": task_summary, "maxItems": 128},
        "aggregate_ref": nullable_text, "aggregate_checksum": nullable_checksum,
        "diagnostics": _ARRAY_OF_TEXT, "result_refs": _ARRAY_OF_TEXT,
        "truncated": {"type": "boolean"},
    })
    recovered_result = object_schema({
        "task_id": _TEXT, "task_instance_id": _TEXT, "attempt": _POSITIVE_INTEGER,
        "status": {"enum": ["succeeded", "failed"]}, "result_checksum": _CHECKSUM_TEXT,
    })
    fields = {
        "event_type": {"const": event_type}, "parallel_event_idempotency_key": _TEXT,
        "idempotency_key": _TEXT, "group": group, "wave": wave,
        "group_id": _TEXT, "wave_id": _TEXT, "task_ids": _ARRAY_OF_TEXT,
        "task_id": _TEXT,
        "task_instance_id": _TEXT, "attempt": _POSITIVE_INTEGER, "child_id": _TEXT,
        "requested_parallelism": non_negative, "effective_parallelism": non_negative,
        "queue_wait_ms": non_negative, "run_duration_ms": non_negative,
        "join_duration_ms": non_negative, "group_duration_ms": non_negative,
        "reason_code": _TEXT, "diagnostics": _ARRAY_OF_TEXT,
        "quarantined_task_ids": _ARRAY_OF_TEXT, "retry_eligible": {"type": "boolean"},
        "reservation_state": reservation_state,
        "reservation_states": {"type": "object", "additionalProperties": reservation_state, "maxProperties": 128},
        "child_states": {"type": "object", "additionalProperties": _TEXT, "maxProperties": 128},
        "recovery_outcome": _TEXT,
        "recovery_id": _TEXT,
        "operation_key": _TEXT,
        "budget_before_checksum": _CHECKSUM_TEXT,
        "budget_after_checksum": _CHECKSUM_TEXT,
        "budget_reservation": {
            "type": "object", "additionalProperties": False,
            "required": [
                "schema_version", "ledger_version", "owner_scope", "reservation_key",
                "parent_allocation", "attempt_allocation", "reservation_checksum",
            ],
            "properties": {
                "schema_version": {"const": "agora.harness-budget-reservation/v1"},
                "ledger_version": _POSITIVE_INTEGER,
                "owner_scope": _TEXT,
                "reservation_key": _TEXT,
                "parent_allocation": budget,
                "attempt_allocation": budget,
                "reservation_checksum": _CHECKSUM_TEXT,
                "turns": non_negative, "remaining_turns": non_negative,
                "tool_calls": non_negative, "remaining_tool_calls": non_negative,
                "memory_ops": non_negative, "remaining_memory_ops": non_negative,
                "output_tokens": non_negative, "remaining_output_tokens": non_negative,
            },
        },
        "spawn_status": {"enum": ["SPAWN_CONFIRMED", "SPAWN_UNKNOWN"]},
        "terminal_outcome": wave_terminal_outcome,
        "recovered_results": {"type": "array", "items": recovered_result, "maxItems": 128},
        "observation": observation,
    }
    required_by_type = {
        "TASK_GROUP_ADMITTED": ["group", "requested_parallelism", "effective_parallelism", "idempotency_key"],
        "TASK_WAVE_ADMITTED": ["group", "wave", "idempotency_key", "budget_before_checksum", "budget_after_checksum"],
        "TASK_ATTEMPT_SPAWN_INTENT": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "idempotency_key", "budget_reservation"],
        "TASK_ATTEMPT_SPAWN_CONFIRMED": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "spawn_status", "child_id", "idempotency_key"],
        "TASK_ATTEMPT_SPAWN_UNKNOWN": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "spawn_status", "idempotency_key"],
        "TASK_WAVE_DISPATCHED": ["group_id", "wave_id", "task_ids", "idempotency_key"],
        "TASK_WAVE_COMPLETED": ["group_id", "wave_id", "task_ids", "terminal_outcome"],
        "TASK_GROUP_JOIN_WAITING": ["group", "observation", "idempotency_key"],
        "TASK_GROUP_JOINED": ["group", "observation", "idempotency_key"],
        "TASK_GROUP_RECOVERY": ["group", "group_id", "recovered_results", "recovery_outcome", "idempotency_key"],
        "RECOVERY_STATUS_READ": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "recovery_outcome", "idempotency_key"],
        "RECOVERY_RECONCILED": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "recovery_outcome", "idempotency_key"],
        "RECOVERY_HALTED": ["group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "reason_code", "idempotency_key"],
        "TASK_GROUP_RECLAIMED": ["group_id", "wave_id", "task_ids", "task_instance_id", "attempt", "child_id", "retry_eligible"],
        "DEGRADED_SERIAL": ["group_id", "reason_code"],
    }
    required = ["event_type", "parallel_event_idempotency_key", *required_by_type.get(event_type, ["reason_code"])]
    if event_type.startswith("RECOVERY_"):
        required.append("recovery_id")
    if event_type == "RECOVERY_RECONCILED":
        required.append("child_id")
    result = object_schema(fields, required=required)
    result["anyOf"] = [{"required": ["group"]}, {"required": ["group_id"]}]
    if event_type in {"TASK_ATTEMPT_SPAWN_CONFIRMED", "TASK_ATTEMPT_SPAWN_UNKNOWN"}:
        result["properties"]["spawn_status"] = {
            "const": "SPAWN_CONFIRMED" if event_type.endswith("CONFIRMED") else "SPAWN_UNKNOWN"
        }
    if event_type == "TASK_ATTEMPT_SPAWN_UNKNOWN":
        result["properties"]["child_id"] = False
    if event_type in {"RECOVERY_STATUS_READ", "RECOVERY_RECONCILED"}:
        result["properties"]["recovery_outcome"] = {
            "const": "status_read" if event_type == "RECOVERY_STATUS_READ" else "SPAWN_CONFIRMED"
        }
    if event_type == "RECOVERY_HALTED":
        result["properties"]["reason_code"] = {"enum": ["SPAWN_UNKNOWN", "SPAWN_IDENTITY_CONFLICT", "CHILD_NOT_TRACKABLE"]}
    return result


def _task_plan_event_payload_schema(
    event_type: str,
    *,
    data_schema: str,
) -> dict[str, Any]:
    nullable_text = {"anyOf": [_TEXT, {"type": "null"}]}
    nullable_checksum = {"anyOf": [_CHECKSUM_TEXT, {"type": "null"}]}
    nullable_positive_integer = {"anyOf": [_POSITIVE_INTEGER, {"type": "null"}]}
    safe_payload = {
        "type": "object",
        "additionalProperties": False,
        "maxProperties": 16,
        "properties": {
            "candidate_ref": _CHECKSUM_TEXT,
            "plan_ref": _CHECKSUM_TEXT,
            "policy_ref": _TEXT,
            "patch_ref": _CHECKSUM_TEXT,
            "result_ref": nullable_text,
            "result_checksum": _CHECKSUM_TEXT,
            "gate_refs": {
                "type": "array",
                "items": _TEXT,
                "maxItems": 64,
                "uniqueItems": True,
            },
            "gate_evidence_refs": {
                "type": "array",
                "items": _CHECKSUM_TEXT,
                "maxItems": 64,
                "uniqueItems": True,
            },
            "transcript_ref": nullable_text,
            "transcript_checksum": nullable_checksum,
            "subagent_output_ref": nullable_text,
            "subagent_output_checksum": nullable_checksum,
            "aggregate_ref": _TEXT,
            "aggregate_checksum": _CHECKSUM_TEXT,
            "projection_checksum": _CHECKSUM_TEXT,
            "decision_checksum": _CHECKSUM_TEXT,
            "diagnostic_ref": _TEXT,
            "output_refs_by_role": {
                "type": "object",
                "additionalProperties": _TEXT,
                "maxProperties": 128,
            },
            "result_refs": _ARRAY_OF_TEXT,
            "branch_refs": {
                "type": "array",
                "items": {"type": "object", "maxProperties": 16},
                "maxItems": 128,
            },
            "replaced_task_id": _TEXT,
            "replacement_task_id": _TEXT,
            "blocking_predecessor_ids": _ARRAY_OF_TEXT,
        },
    }
    if event_type == "PLAN_CANDIDATE_BUILT":
        safe_payload["properties"]["submission"] = _candidate_submission_schema()
    if event_type in {"TASK_PLAN_VERIFIED", "TASK_PLAN_HALTED"}:
        terminal_fields = ("submission_key", "terminal_result", "terminal_result_checksum")
        safe_payload["properties"].update({
            "submission_key": _CHECKSUM_TEXT,
            "terminal_result": _task_plan_terminal_result_schema(),
            "terminal_result_checksum": _CHECKSUM_TEXT,
        })
        safe_payload["dependentRequired"] = {
            name: [other for other in terminal_fields if other != name]
            for name in terminal_fields
        }
    graph_identity_required = [
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_schema_version",
        "compiler_version",
        "condition_policy_version",
        "stage_binding_checksum",
        "stage_identity_schema",
        "stage_identity_checksum",
    ]
    identity_required = graph_identity_required
    identity_properties = {
        "graph_id": _TEXT,
        "graph_version": _TEXT,
        "graph_ref": _TEXT,
        "graph_schema_version": _TEXT,
        "compiler_version": _TEXT,
        "condition_policy_version": _TEXT,
        "stage_binding_checksum": _CHECKSUM_TEXT,
        "stage_identity_schema": _TEXT,
        "stage_identity_checksum": _CHECKSUM_TEXT,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "run_id",
            *identity_required,
            "stage_id",
            "graph_checksum",
            "plan_id",
            "plan_version",
            "task_id",
            "task_instance_id",
            "attempt",
            "schema_version",
            "actor_type",
            "causal_event_ref",
            "input_checksum",
            "output_refs",
            "reason_code",
            "details",
            "sequence",
            "event_checksum",
        ],
        "properties": {
            "run_id": _TEXT,
            **identity_properties,
            "stage_id": _TEXT,
            "graph_checksum": _CHECKSUM_TEXT,
            "plan_id": nullable_text,
            "plan_version": nullable_positive_integer,
            "task_id": nullable_text,
            "task_instance_id": nullable_text,
            "attempt": nullable_positive_integer,
            "schema_version": {"const": data_schema},
            "actor_type": _TEXT,
            "causal_event_ref": nullable_text,
            "input_checksum": nullable_checksum,
            "output_refs": {
                "type": "array",
                "items": _TEXT,
                "maxItems": 128,
                "uniqueItems": True,
            },
            "reason_code": nullable_text,
            "details": (
                _parallel_task_plan_details_schema(event_type)
                if event_type in TASK_PLAN_PARALLEL_EVENT_TYPES else safe_payload
            ),
            "sequence": _POSITIVE_INTEGER,
            "event_checksum": _CHECKSUM_TEXT,
        },
    }


def _harness_graph_commit_payload_schema(event_type: str) -> dict[str, Any]:
    commit_kind: dict[str, Any]
    commit_field: str
    if event_type == "harness_graph_initialized":
        commit_kind = {"const": "initialize"}
        commit_field = "state"
    elif event_type == "harness_graph_decision_committed":
        commit_kind = {"const": "decision"}
        commit_field = "decision"
    elif event_type == "harness_graph_projection_committed":
        commit_kind = {
            "enum": [
                "decision_projection",
                "activity_result_projection",
                "observation_projection",
            ]
        }
        commit_field = "state"
    elif event_type == "harness_graph_activity_result_committed":
        commit_kind = {"const": "activity_result"}
        commit_field = "result"
    elif event_type == "harness_graph_observation_committed":
        commit_kind = {"const": "observation"}
        commit_field = "observation"
    else:  # pragma: no cover - default catalog owns the finite caller set
        raise AssertionError(f"unsupported Harness graph event type: {event_type}")
    commit_properties: dict[str, Any] = {
        "schema_version": {"const": HARNESS_GRAPH_COMMIT_DATA_SCHEMA},
        "commit_kind": commit_kind,
        "sequence": _POSITIVE_INTEGER,
        "occurred_at": _TEXT,
        "commit_checksum": _CHECKSUM_TEXT,
        commit_field: _OBJECT,
    }
    commit_required = [
        "schema_version",
        "commit_kind",
        "sequence",
        "occurred_at",
        "commit_checksum",
        commit_field,
    ]
    if event_type == "harness_graph_decision_committed":
        commit_properties.update(
            {
                "activity_input_ref": {
                    "anyOf": [_CHECKSUM_TEXT, {"type": "null"}],
                },
                "accepted_evidence_refs": _ARRAY_OF_CHECKSUMS,
                "side_effect_outcome_ref": {
                    "anyOf": [_CHECKSUM_TEXT, {"type": "null"}],
                },
            }
        )
        commit_required.extend(
            [
                "activity_input_ref",
                "accepted_evidence_refs",
                "side_effect_outcome_ref",
            ]
        )
    elif event_type in {
        "harness_graph_initialized",
        "harness_graph_projection_committed",
    }:
        previous_projection_checksum = (
            {"type": "null"}
            if event_type == "harness_graph_initialized"
            else _CHECKSUM_TEXT
        )
        commit_properties.update(
            {
                "cause_checksum": _CHECKSUM_TEXT,
                "previous_projection_checksum": previous_projection_checksum,
                "budget_reservations": _OBJECT,
                "budget_consumptions": _OBJECT,
                "activity": {"anyOf": [_OBJECT, {"type": "null"}]},
            }
        )
        commit_required.extend(
            [
                "cause_checksum",
                "previous_projection_checksum",
                "budget_reservations",
                "budget_consumptions",
                "activity",
            ]
        )
    commit_schema = {
        "type": "object",
        "maxProperties": 17,
        "properties": commit_properties,
        "required": commit_required,
        "additionalProperties": False,
    }
    if event_type == "harness_graph_initialized":
        return {
            "type": "object",
            "maxProperties": 3,
            "properties": {
                "commit": commit_schema,
                "graph": {"type": "object", "maxProperties": 32},
                "run_spec_checksum": _CHECKSUM_TEXT,
            },
            "required": ["commit", "graph", "run_spec_checksum"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "maxProperties": 1,
        "properties": {"commit": commit_schema},
        "required": ["commit"],
        "additionalProperties": False,
    }


def _harness_graph_projection_record_payload_schema() -> dict[str, Any]:
    checksum = _CHECKSUM_TEXT
    commit = {
        "type": "object",
        "maxProperties": 17,
        "properties": {
            "schema_version": {
                "const": HARNESS_GRAPH_PROJECTION_RECORD_DATA_SCHEMA
            },
            "state_schema_version": {"type": "string", "minLength": 1},
            "reducer_version": {"type": "string", "minLength": 1},
            "commit_kind": {
                "enum": [
                    "decision_projection",
                    "activity_result_projection",
                    "observation_projection",
                ]
            },
            "run_id": _TEXT,
            "cause_checksum": checksum,
            "previous_projection_checksum": checksum,
            "projection_checksum": checksum,
            "sequence": _POSITIVE_INTEGER,
            "occurred_at": _TEXT,
            "budget_reservations": _OBJECT,
            "budget_consumptions": _OBJECT,
            "activity": {"anyOf": [_OBJECT, {"type": "null"}]},
            "state_summary": _OBJECT,
            "activated_node_instance_id": {"anyOf": [_TEXT, {"type": "null"}]},
            "projection_commit_checksum": checksum,
            "record_checksum": checksum,
        },
        "required": [
            "schema_version",
            "state_schema_version",
            "reducer_version",
            "commit_kind",
            "run_id",
            "cause_checksum",
            "previous_projection_checksum",
            "projection_checksum",
            "sequence",
            "occurred_at",
            "budget_reservations",
            "budget_consumptions",
            "activity",
            "state_summary",
            "activated_node_instance_id",
            "projection_commit_checksum",
            "record_checksum",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "maxProperties": 1,
        "properties": {"commit": commit},
        "required": ["commit"],
        "additionalProperties": False,
    }


_HARNESS_GATE_RESULT = {
    "type": "object",
    "maxProperties": 6,
    "properties": {
        "gate": {"type": "string", "minLength": 1, "maxLength": 128},
        "passed": _BOOLEAN,
        "reason": _NULLABLE_TEXT,
        "details": _OBJECT,
        "diagnostics": _OBJECT,
        "result_ref": _CHECKSUM_TEXT,
    },
    "required": ["gate", "passed"],
    "additionalProperties": False,
}
_HARNESS_GATE_RESULTS = {
    "type": "array",
    "items": _HARNESS_GATE_RESULT,
    "maxItems": 256,
}
_HARNESS_PHASE_METADATA = {
    "type": "object",
    "maxProperties": 5,
    "properties": {
        "turn_count": _NONNEGATIVE_INTEGER,
        "worker_call_count": _NONNEGATIVE_INTEGER,
        "replan_count": _NONNEGATIVE_INTEGER,
        "node_instance_id": _TEXT,
        "attempt": _NONNEGATIVE_INTEGER,
    },
    "additionalProperties": False,
}
_HARNESS_DECISION_PAYLOAD = {
    "type": "object",
    "maxProperties": 11,
    "properties": {
        "approval_outcome": {"enum": ["approved", "cancelled"]},
        "backoff_seconds": {"type": "number", "minimum": 0},
        "max_turns": _NONNEGATIVE_INTEGER,
        "turn_count": _NONNEGATIVE_INTEGER,
        "gate_results": _HARNESS_GATE_RESULTS,
        "quality_verdict_ref": _CHECKSUM_TEXT,
        "worker_result_ref": _CHECKSUM_TEXT,
        "value_ref": _CHECKSUM_TEXT,
        "decision_payload_ref": _CHECKSUM_TEXT,
        "graph_decision_checksum": _CHECKSUM_TEXT,
        "side_effect_decision_ref": _CHECKSUM_TEXT,
    },
    "required": ["decision_payload_ref"],
    "additionalProperties": False,
}
_HARNESS_STEP_METADATA = {
    "type": "object",
    "maxProperties": 4,
    "properties": {
        "approval_granted": _BOOLEAN,
        "rerouted": _BOOLEAN,
        "worker_result_ref": _CHECKSUM_TEXT,
        "metadata_ref": _CHECKSUM_TEXT,
    },
    "additionalProperties": False,
}

def _payload_schema(
    *,
    properties: Mapping[str, Any] | None = None,
    required: tuple[str, ...] = (),
    additional_properties: bool = False,
    any_of: tuple[Mapping[str, Any], ...] = (),
    min_properties: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "maxProperties": 128,
        "properties": copy.deepcopy(dict(properties or {})),
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    if any_of:
        schema["anyOf"] = [copy.deepcopy(dict(item)) for item in any_of]
    if min_properties is not None:
        schema["minProperties"] = min_properties
    return schema


_ATTEMPT_BUDGET_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "max_attempts": _POSITIVE_INTEGER,
        "used_attempts": _NONNEGATIVE_INTEGER,
        "remaining_attempts": _NONNEGATIVE_INTEGER,
    },
    "required": ["max_attempts", "used_attempts", "remaining_attempts"],
    "additionalProperties": False,
}

_RETRY_CREDIT_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "max_total_retries": _NONNEGATIVE_INTEGER,
        "used_retries": _NONNEGATIVE_INTEGER,
        "remaining_retries": _NONNEGATIVE_INTEGER,
    },
    "required": [
        "max_total_retries",
        "used_retries",
        "remaining_retries",
    ],
    "additionalProperties": False,
}

_ATTEMPT_DEADLINE_CALCULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "now_monotonic": _NUMBER,
        "requested_until": _NULLABLE_NUMBER,
        "parent_available_until": _NULLABLE_NUMBER,
        "root_available_until": _NULLABLE_NUMBER,
        "completion_until": _NULLABLE_NUMBER,
        "effective_deadline": _NULLABLE_NUMBER,
        "execution_window_seconds": _NULLABLE_NUMBER,
        "min_start_window_seconds": _NONNEGATIVE_NUMBER,
        "cancellation_grace_seconds": _NONNEGATIVE_NUMBER,
        "completion_reserve_seconds": _NONNEGATIVE_NUMBER,
    },
    "required": [
        "now_monotonic",
        "requested_until",
        "parent_available_until",
        "root_available_until",
        "completion_until",
        "effective_deadline",
        "execution_window_seconds",
        "min_start_window_seconds",
        "cancellation_grace_seconds",
        "completion_reserve_seconds",
    ],
    "additionalProperties": False,
}


def _attempt_payload_schema(event_type: str) -> dict[str, Any]:
    common = {
        "execution_id": _TEXT,
        "operation_id": _TEXT,
        "operation_kind": _TEXT,
        "idempotency_key": _TEXT,
        "started": _BOOLEAN,
        "deadline_calculation": _ATTEMPT_DEADLINE_CALCULATION_SCHEMA,
        "local_budget": _ATTEMPT_BUDGET_SNAPSHOT_SCHEMA,
        "root_retry_credits": _RETRY_CREDIT_SNAPSHOT_SCHEMA,
    }
    common_required = (
        "execution_id",
        "operation_id",
        "operation_kind",
        "idempotency_key",
        "started",
        "deadline_calculation",
        "local_budget",
        "root_retry_credits",
    )
    if event_type == "attempt_admission_rejected":
        return _payload_schema(
            properties={
                **common,
                "started": {"const": False},
                "reason_code": _TEXT,
            },
            required=(*common_required, "reason_code"),
        )
    started_properties = {
        **common,
        "started": {"const": True},
        "attempt_id": _TEXT,
        "local_attempt_no": _POSITIVE_INTEGER,
        "retry_credit_id": _NULLABLE_TEXT,
        "parent_attempt_id": _NULLABLE_TEXT,
    }
    started_required = (
        *common_required,
        "attempt_id",
        "local_attempt_no",
        "retry_credit_id",
        "parent_attempt_id",
    )
    if event_type == "attempt_started":
        return _payload_schema(
            properties=started_properties,
            required=started_required,
        )
    if event_type == "attempt_terminal":
        return _payload_schema(
            properties={
                **started_properties,
                "state": {
                    "enum": [
                        "SUCCEEDED",
                        "FAILED",
                        "TIMED_OUT",
                        "INDETERMINATE",
                    ]
                },
                "reason_code": _NULLABLE_TEXT,
                "termination_confirmed": _BOOLEAN,
                "indeterminate": _BOOLEAN,
                "elapsed_seconds": _NONNEGATIVE_NUMBER,
            },
            required=(
                *started_required,
                "state",
                "reason_code",
                "termination_confirmed",
                "indeterminate",
                "elapsed_seconds",
            ),
        )
    raise EventSchemaError(f"Attempt event schema is not defined: {event_type}")


def _harness_payload_schema(event_type: str) -> dict[str, Any]:
    if event_type == "run_created":
        return _payload_schema(
            properties={"projection_schema": {"const": "harness-safe-summary/v1"}},
        )
    if event_type == "run_state_changed":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "status": {
                    "enum": [
                        "created",
                        "running",
                        "planning",
                        "executing",
                        "verifying",
                        "replanning",
                        "waiting_approval",
                        "succeeded",
                        "failed",
                        "halted",
                        "cancelled",
                        "blocked",
                    ]
                },
            },
            required=("status",),
        )
    if event_type == "step_state_changed":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "node_id": _TEXT,
                "status": {
                    "enum": [
                        "pending",
                        "planning",
                        "plan_verified",
                        "running",
                        "verifying",
                        "retrying",
                        "replanning",
                        "succeeded",
                        "failed",
                        "skipped",
                        "waiting_approval",
                        "halted",
                    ]
                },
                "attempts": _NONNEGATIVE_INTEGER,
                "replans": _NONNEGATIVE_INTEGER,
                "output_ref": _NULLABLE_TEXT,
                "output_key_ref": _CHECKSUM_TEXT,
                "error": _NULLABLE_TEXT,
                "error_ref": _CHECKSUM_TEXT,
                "metadata": _HARNESS_STEP_METADATA,
                "updated_at": _TEXT,
            },
            required=("status", "attempts", "replans", "metadata"),
            any_of=(
                {"required": ["node_id", "updated_at"]},
                {"required": ["projection_schema"]},
            ),
        )
    if event_type == "graph_phase_transition_recorded":
        return _payload_schema(
            properties={
                "graph_phase_transition": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "schema",
                        "context",
                        "phase",
                        "boundary",
                        "attempt",
                        "event_sequence",
                        "gate_evidence_refs",
                        "occurred_at",
                        "record_checksum",
                    ],
                    "properties": {
                        "schema": {"const": "newsroom.harness-graph-phase-transition/v1"},
                        "context": {"type": "object"},
                        "phase": {"enum": ["plan", "execute", "verify", "replan", "halt"]},
                        "boundary": {"enum": ["entry", "exit"]},
                        "attempt": {"type": "integer", "minimum": 0},
                        "event_sequence": {"type": "integer", "minimum": 1},
                        "gate_evidence_refs": {
                            "type": "array",
                            "items": _CHECKSUM_TEXT,
                            "uniqueItems": True,
                        },
                        "occurred_at": _TEXT,
                        "record_checksum": _CHECKSUM_TEXT,
                    },
                }
            },
            required=("graph_phase_transition",),
        )
    if event_type == "phase_recorded":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "phase": {"enum": ["plan", "execute", "verify", "replan", "halt"]},
                "boundary": {"enum": ["entry", "exit"]},
                "node_id": _TEXT,
                "input_refs": _ARRAY_OF_TEXT,
                "output_refs": _ARRAY_OF_TEXT,
                "input_ref_checksums": _ARRAY_OF_CHECKSUMS,
                "output_ref_checksums": _ARRAY_OF_CHECKSUMS,
                "gate_results": _HARNESS_GATE_RESULTS,
                "metadata": _HARNESS_PHASE_METADATA,
                "occurred_at": _TEXT,
                # Historical phase-transition payload retained during migration.
                "from_phase": _TEXT,
                "to_phase": _TEXT,
            },
            any_of=(
                {"required": ["phase", "node_id", "gate_results", "occurred_at"]},
                {
                    "required": [
                        "projection_schema",
                        "phase",
                        "boundary",
                        "input_ref_checksums",
                        "output_ref_checksums",
                        "gate_results",
                        "metadata",
                    ]
                },
                {"required": ["from_phase", "to_phase"]},
            ),
        )
    if event_type == "decision_recorded":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "decision_type": {
                    "enum": [
                        "start_step",
                        "plan_step",
                        "execute_step",
                        "evaluate_result_persistence",
                        "verify_step",
                        "complete_step",
                        "retry_step",
                        "replan_step",
                        "route_to_step",
                        "route_to_repair",
                        "wait_for_approval",
                        "resume_after_approval",
                        "fail_run",
                        "complete_run",
                        "cancel_run",
                        "block_run",
                        "halt_run",
                    ]
                },
                "run_id": _TEXT,
                "node_id": _NULLABLE_TEXT,
                "target_node_id": _NULLABLE_TEXT,
                "reason": _NULLABLE_TEXT,
                "reason_ref": _CHECKSUM_TEXT,
                "payload": _OBJECT,
                "decision_payload": _HARNESS_DECISION_PAYLOAD,
                "decided_by": {"const": "harness"},
                "decided_at": _TEXT,
            },
            required=("decision_type", "decided_by"),
            any_of=(
                {"required": ["run_id", "payload", "decided_at"]},
                {"required": ["projection_schema", "decision_payload"]},
            ),
        )
    if event_type == "graph_worker_called":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "run_id": _TEXT,
                "node_id": _TEXT,
                "worker_type": {
                    "enum": [
                        "function",
                        "tool",
                        "agent_loop",
                        "task_plan",
                        "llm",
                        "skill",
                        "skill_evolution",
                        "subagent",
                        "retrieval",
                        "memory",
                        "mcp",
                        "quality_gate",
                        "script",
                    ]
                },
                "activity_id": _TEXT,
                "node_instance_id": _TEXT,
                "idempotency_key": _TEXT,
                "activity_attempt": _POSITIVE_INTEGER,
                "activity_contract_version": _TEXT,
                "activity_checksum": _CHECKSUM_TEXT,
                "graph_ref": _OBJECT,
                "step_ref": _OBJECT,
                "worker_ref": _OBJECT,
                "activity_ref": _OBJECT,
                "input_ref": _CHECKSUM_TEXT,
                "activity_input_ref": _CHECKSUM_TEXT,
                "inputs": _OBJECT,
                "metadata": _OBJECT,
                "input_count": _NONNEGATIVE_INTEGER,
                "metadata_ref": _CHECKSUM_TEXT,
            },
            required=("worker_type",),
            any_of=(
                {"required": ["run_id", "node_id", "inputs", "metadata"]},
                {
                    "required": [
                        "projection_schema",
                        "activity_id",
                        "idempotency_key",
                        "activity_attempt",
                        "activity_contract_version",
                        "input_ref",
                        "input_count",
                        "metadata_ref",
                    ]
                },
            ),
        )
    if event_type == "graph_worker_result_recorded":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "status": {
                    "enum": ["succeeded", "failed", "blocked", "waiting_approval"]
                },
                "output": _OBJECT,
                "artifacts": _ARRAY_OF_TEXT,
                "diagnostics": _OBJECT,
                "metrics": _OBJECT,
                "error": _NULLABLE_TEXT,
                "output_ref": _CHECKSUM_TEXT,
                "diagnostics_ref": _CHECKSUM_TEXT,
                "metric_count": _NONNEGATIVE_INTEGER,
                "artifact_count": _NONNEGATIVE_INTEGER,
                "artifact_ref_checksums": _ARRAY_OF_CHECKSUMS,
                "error_ref": _CHECKSUM_TEXT,
            },
            required=("status",),
            any_of=(
                {"required": ["output", "artifacts", "diagnostics", "metrics"]},
                {
                    "required": [
                        "projection_schema",
                        "output_ref",
                        "diagnostics_ref",
                        "metric_count",
                        "artifact_count",
                        "artifact_ref_checksums",
                    ]
                },
            ),
        )
    if event_type == "gate_evaluated":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "gate": {"type": "string", "minLength": 1, "maxLength": 128},
                "passed": _BOOLEAN,
                "reason": _NULLABLE_TEXT,
                "details": _OBJECT,
                "reason_ref": _CHECKSUM_TEXT,
                "details_ref": _CHECKSUM_TEXT,
                "reference": _TEXT,
                "input_ref": _CHECKSUM_TEXT,
                "result_ref": _CHECKSUM_TEXT,
                "reason_code": _TEXT,
                "score": {"type": "number", "minimum": 0, "maximum": 1},
            },
            required=("gate", "passed"),
            any_of=(
                {"required": ["details"]},
                {"required": ["projection_schema", "details_ref"]},
            ),
        )
    if event_type == "budget_fact_recorded":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "resolution_status": {"enum": ["verified", "invalid"]},
                "operation_id": _TEXT,
                "ledger_revision": _POSITIVE_INTEGER,
                "within_budget": _BOOLEAN,
                "violations": {
                    "type": "array",
                    "items": _TEXT,
                    "maxItems": 16,
                    "uniqueItems": True,
                },
                "fact_ref": {"anyOf": [_CHECKSUM_TEXT, {"type": "null"}]},
                "event_id": {"anyOf": [_TEXT, {"type": "null"}]},
                "event_type": {
                    "anyOf": [
                        {
                            "enum": [
                                "budget_reservation_denied",
                                "budget_reservation_settled",
                                "budget_reservation_released",
                                "budget_reservation_expired",
                                "budget_reservation_indeterminate",
                            ]
                        },
                        {"type": "null"},
                    ]
                },
                "reservation_id": {"anyOf": [_TEXT, {"type": "null"}]},
                "policy_digest": {"anyOf": [_CHECKSUM_TEXT, {"type": "null"}]},
                "scope_id": {"anyOf": [_TEXT, {"type": "null"}]},
                "stream_sequence": {
                    "anyOf": [_POSITIVE_INTEGER, {"type": "null"}]
                },
                "reason_code": {"anyOf": [_TEXT, {"type": "null"}]},
            },
            required=(
                "projection_schema",
                "resolution_status",
                "operation_id",
                "ledger_revision",
                "within_budget",
                "violations",
                "fact_ref",
                "event_id",
                "event_type",
                "reservation_id",
                "policy_digest",
                "scope_id",
                "stream_sequence",
                "reason_code",
            ),
        )
    if event_type == "checkpoint_created":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "checkpoint_id": _TEXT,
            },
            required=("checkpoint_id",),
        )
    if event_type.startswith("context_"):
        return _context_compaction_payload_schema(event_type)
    raise EventSchemaError(f"Harness event schema is not defined: {event_type}")


def _context_compaction_payload_schema(event_type: str) -> dict[str, Any]:
    snapshot_properties = {
        "projection_schema": {"const": "harness-safe-summary/v1"},
        "source_snapshot_id": _TEXT,
        "source_snapshot_checksum": _CHECKSUM_TEXT,
    }
    if event_type == "context_compaction_planned":
        return _payload_schema(
            properties={
                **snapshot_properties,
                "initial_admission_id": _TEXT,
                "initial_admission_ref": _NULLABLE_TEXT,
                "planning_result_ref": _NULLABLE_TEXT,
                "plan_ref": _NULLABLE_TEXT,
                "plan_id": _NULLABLE_TEXT,
                "status": {
                    "enum": [
                        "plan_ready",
                        "no_compaction_required",
                        "protected_context_exceeds_window",
                        "no_allowed_compaction",
                        "action_budget_exhausted",
                    ]
                },
                "reason_code": _TEXT,
                "protected_group_count": _NONNEGATIVE_INTEGER,
            },
            required=(
                "projection_schema",
                "source_snapshot_id",
                "source_snapshot_checksum",
                "initial_admission_id",
                "initial_admission_ref",
                "planning_result_ref",
                "plan_ref",
                "plan_id",
                "status",
                "reason_code",
                "protected_group_count",
            ),
        )
    if event_type == "context_compaction_action_applied":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "plan_id": _TEXT,
                "action_id": _TEXT,
                "action_type": {
                    "enum": [
                        "drop_reconstructable_group",
                        "replace_with_reference",
                        "reduce_authorized_tool_set",
                        "select_evidence_spans",
                        "compact_old_conversation",
                        "summarize_groups",
                    ]
                },
                "action_result_ref": _TEXT,
                "source_snapshot_id": _TEXT,
                "result_group_count": _NONNEGATIVE_INTEGER,
                "applied": _BOOLEAN,
                "reason_code": _TEXT,
            },
            required=(
                "projection_schema",
                "plan_id",
                "action_id",
                "action_type",
                "action_result_ref",
                "source_snapshot_id",
                "result_group_count",
                "applied",
                "reason_code",
            ),
        )
    if event_type == "context_summary_candidate_created":
        return _payload_schema(
            properties={
                "projection_schema": {"const": "harness-safe-summary/v1"},
                "plan_id": _TEXT,
                "action_id": _TEXT,
                "action_result_ref": _TEXT,
                "candidate_ref": _TEXT,
            },
            required=(
                "projection_schema",
                "plan_id",
                "action_id",
                "action_result_ref",
                "candidate_ref",
            ),
        )
    if event_type == "context_compaction_verified":
        return _payload_schema(
            properties={
                **snapshot_properties,
                "result_snapshot_id": _TEXT,
                "result_snapshot_checksum": _CHECKSUM_TEXT,
                "plan_id": _TEXT,
                "record_ref": _TEXT,
                "aggregate_ref": _TEXT,
                "initial_admission_ref": _TEXT,
                "final_admission_ref": _TEXT,
                "prepared_fingerprint": _CHECKSUM_TEXT,
                "before_input_tokens": _NONNEGATIVE_INTEGER,
                "after_input_tokens": _NONNEGATIVE_INTEGER,
                "policy_revision": _TEXT,
                "profile_revision": _TEXT,
                "gate_names": _ARRAY_OF_TEXT,
            },
            required=(
                "projection_schema",
                "source_snapshot_id",
                "source_snapshot_checksum",
                "result_snapshot_id",
                "result_snapshot_checksum",
                "plan_id",
                "record_ref",
                "aggregate_ref",
                "initial_admission_ref",
                "final_admission_ref",
                "prepared_fingerprint",
                "before_input_tokens",
                "after_input_tokens",
                "policy_revision",
                "profile_revision",
                "gate_names",
            ),
        )
    if event_type == "context_compaction_rejected":
        return _payload_schema(
            properties={
                **snapshot_properties,
                "plan_id": _NULLABLE_TEXT,
                "planning_result_ref": _NULLABLE_TEXT,
                "result_snapshot_id": _NULLABLE_TEXT,
                "record_ref": _NULLABLE_TEXT,
                "aggregate_ref": _NULLABLE_TEXT,
                "reason_code": _TEXT,
                "aggregate_outcome": _NULLABLE_TEXT,
                "planning_status": _TEXT,
            },
            required=(
                "projection_schema",
                "source_snapshot_id",
                "source_snapshot_checksum",
                "plan_id",
                "planning_result_ref",
                "result_snapshot_id",
                "record_ref",
                "aggregate_ref",
                "reason_code",
                "aggregate_outcome",
            ),
        )
    raise EventSchemaError(f"Context compaction event schema is not defined: {event_type}")


def _harness_sensitivity_policy(event_type: str) -> SensitivityPolicy:
    if event_type == "phase_recorded":
        return SensitivityPolicy(
            field_rules={
                "/input_refs/*": FieldDisposition.REFERENCE_ONLY,
                "/output_refs/*": FieldDisposition.REFERENCE_ONLY,
                "/gate_results/*/details": FieldDisposition.SENSITIVE,
                "/gate_results/*/diagnostics": FieldDisposition.REFERENCE_ONLY,
                "/gate_results/*/reason": FieldDisposition.SENSITIVE,
            },
            whole_document_reference=(
                WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ),
            redact_sensitive=True,
        )
    if event_type == "decision_recorded":
        return SensitivityPolicy(
            field_rules={
                "/payload": FieldDisposition.REFERENCE_ONLY,
                "/reason": FieldDisposition.SENSITIVE,
            },
            whole_document_reference=(
                WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ),
            redact_sensitive=True,
        )
    if event_type == "graph_worker_called":
        return SensitivityPolicy(
            field_rules={
                "/inputs": FieldDisposition.REFERENCE_ONLY,
                "/metadata": FieldDisposition.REFERENCE_ONLY,
            },
            whole_document_reference=(
                WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ),
        )
    if event_type == "graph_worker_result_recorded":
        return SensitivityPolicy(
            field_rules={
                "/output": FieldDisposition.REFERENCE_ONLY,
                "/artifacts/*": FieldDisposition.REFERENCE_ONLY,
                "/diagnostics": FieldDisposition.REFERENCE_ONLY,
                "/metrics": FieldDisposition.SENSITIVE,
                "/error": FieldDisposition.SENSITIVE,
            },
            whole_document_reference=(
                WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ),
            redact_sensitive=True,
        )
    if event_type == "gate_evaluated":
        return SensitivityPolicy(
            field_rules={
                "/details": FieldDisposition.SENSITIVE,
                "/reason": FieldDisposition.SENSITIVE,
            },
            redact_sensitive=True,
        )
    if event_type == "step_state_changed":
        return SensitivityPolicy(
            field_rules={
                "/error": FieldDisposition.SENSITIVE,
                "/output_ref": FieldDisposition.REFERENCE_ONLY,
                "/metadata/worker_result": FieldDisposition.REFERENCE_ONLY,
            },
            whole_document_reference=(
                WholeDocumentReferenceDisposition.SECURE_REQUIRED
            ),
            redact_sensitive=True,
        )
    return SensitivityPolicy()


def _validation_error(
    registration: EventSchemaRegistration,
    error: ValidationError,
) -> EventSchemaValidationError:
    return EventSchemaValidationError(
        event_type=registration.event_type,
        data_schema=registration.data_schema,
        path=_json_path(error),
        rule=str(error.validator or "schema"),
    )


def _json_path(error: ValidationError) -> str:
    if not error.absolute_path:
        return "$"
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("JSON value must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError("JSON object keys must be strings")
    frozen: dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return 0 if value == 0 else value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_value(item) for key, item in value.items()}


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return copy.deepcopy(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional schema text must be a string")
    text = value.strip()
    return text or None


def _parse_historical_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("occurred_at is empty")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        raise TypeError("occurred_at must be a string or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("occurred_at must contain an explicit timezone")
    return parsed.astimezone(UTC)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


_SCHEMA_VERSION_PATTERN = re.compile(r"^(?P<prefix>.*(?:/|^))v(?P<version>[1-9][0-9]*)$")


def _schema_version_metric_bucket(value: str) -> str:
    match = _SCHEMA_VERSION_PATTERN.fullmatch(str(value))
    if match is None:
        return "unknown" if value == "unknown" else "other"
    version = int(match.group("version"))
    return f"v{version}" if version <= 10 else "other"


def _default_schema_telemetry() -> EventTelemetry:
    from framework.events.telemetry import default_event_telemetry

    return default_event_telemetry()
_PURE_CALLABLE_FORBIDDEN_OPCODES = frozenset(
    {
        "BUILD_SET",
        "DELETE_DEREF",
        "DELETE_GLOBAL",
        "IMPORT_FROM",
        "IMPORT_NAME",
        "IMPORT_STAR",
        "LOAD_BUILD_CLASS",
        "MAKE_FUNCTION",
        "SET_ADD",
        "SET_UPDATE",
        "STORE_DEREF",
        "STORE_GLOBAL",
    }
)
_PURE_CALLABLE_ALLOWED_BUILTINS = frozenset(
    {
        "AssertionError",
        "TypeError",
        "ValueError",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "round",
        "sorted",
        "str",
        "sum",
        "tuple",
    }
)
_PURE_CALLABLE_ALLOWED_METHODS = frozenset(
    {
        "count",
        "endswith",
        "get",
        "index",
        "items",
        "join",
        "keys",
        "lower",
        "lstrip",
        "replace",
        "rsplit",
        "rstrip",
        "split",
        "startswith",
        "strip",
        "upper",
        "values",
    }
)
_PURE_CALLABLE_REFLECTION_CONSTANTS = frozenset(
    {
        "__builtins__",
        "__class__",
        "__closure__",
        "__code__",
        "__dict__",
        "__func__",
        "__getattribute__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "cr_frame",
        "f_builtins",
        "f_globals",
        "f_locals",
        "func_globals",
        "gi_frame",
        "mro",
    }
)


def _validate_adjacent_schema_versions(source: str, target: str) -> None:
    source_match = _SCHEMA_VERSION_PATTERN.fullmatch(source)
    target_match = _SCHEMA_VERSION_PATTERN.fullmatch(target)
    if source_match is None or target_match is None:
        raise EventSchemaError(
            "upcast schemas must end in an explicit adjacent /vN version"
        )
    if source_match.group("prefix") != target_match.group("prefix"):
        raise EventSchemaError("upcaster cannot change the data-schema namespace")
    source_version = int(source_match.group("version"))
    target_version = int(target_match.group("version"))
    if target_version != source_version + 1:
        raise EventSchemaError(
            f"event upcaster must target the adjacent version: {source} -> {target}"
        )


def _validate_upcaster_purity(
    upcaster: EventUpcaster | None,
    event_type: str,
    data_schema: str,
) -> None:
    _validate_pure_schema_callable(
        upcaster,
        role="upcaster",
        event_type=event_type,
        data_schema=data_schema,
    )


def _validate_custom_validator_purity(
    validator: PayloadValidator,
    event_type: str,
    data_schema: str,
) -> None:
    _validate_pure_schema_callable(
        validator,
        role="custom validator",
        event_type=event_type,
        data_schema=data_schema,
    )


def _validate_pure_schema_callable(
    function: Any,
    *,
    role: str,
    event_type: str,
    data_schema: str,
) -> None:
    if not isinstance(function, FunctionType):
        raise EventSchemaError(
            f"event {role} must be a plain function: {event_type} ({data_schema})"
        )
    if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function):
        raise EventSchemaError(
            f"event {role} must be synchronous: {event_type} ({data_schema})"
        )
    parameters = tuple(inspect.signature(function).parameters.values())
    if len(parameters) != 1 or any(
        parameter.kind
        not in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        for parameter in parameters
    ):
        raise EventSchemaError(f"event {role} must accept exactly one payload argument")
    try:
        _audit_pure_schema_function(function)
    except EventSchemaError:
        raise
    except Exception as exc:
        raise EventSchemaError(
            f"event {role} purity could not be verified: {event_type} ({data_schema})"
        ) from exc


def _audit_pure_schema_function(function: FunctionType) -> None:
    closure = inspect.getclosurevars(function)
    for name in closure.builtins:
        if name not in _PURE_CALLABLE_ALLOWED_BUILTINS:
            raise EventSchemaError(f"event schema callable uses forbidden builtin: {name}")
    for name, value in {**closure.globals, **closure.nonlocals}.items():
        if isinstance(value, ModuleType) or not _is_pure_schema_constant(value):
            raise EventSchemaError(
                f"event schema callable captures forbidden dependency: {name}"
            )
    for value in function.__defaults__ or ():
        if not _is_pure_schema_constant(value):
            raise EventSchemaError("event schema callable has a mutable default")
    for value in (function.__kwdefaults__ or {}).values():
        if not _is_pure_schema_constant(value):
            raise EventSchemaError("event schema callable has a mutable default")
    _audit_pure_schema_code(function.__code__, function=function, seen=set())


def _audit_pure_schema_code(
    code: CodeType,
    *,
    function: FunctionType,
    seen: set[int],
) -> None:
    identity = id(code)
    if identity in seen:
        return
    seen.add(identity)
    for instruction in dis.get_instructions(code):
        operation = instruction.opname
        name = str(instruction.argval) if instruction.argval is not None else ""
        if operation in _PURE_CALLABLE_FORBIDDEN_OPCODES:
            raise EventSchemaError(
                f"event schema callable uses forbidden operation: {operation}"
            )
        if operation == "LOAD_ATTR":
            raise EventSchemaError(
                f"event schema callable uses forbidden attribute: {name}"
            )
        if operation == "LOAD_METHOD" and name not in _PURE_CALLABLE_ALLOWED_METHODS:
            raise EventSchemaError(
                f"event schema callable uses forbidden method: {name}"
            )
        if operation == "LOAD_GLOBAL" and name not in _PURE_CALLABLE_ALLOWED_BUILTINS:
            raise EventSchemaError(
                f"event schema callable uses forbidden global: {name}"
            )
        if operation == "LOAD_CONST":
            _audit_pure_schema_constant(instruction.argval, function=function, seen=seen)


def _audit_pure_schema_constant(
    value: Any,
    *,
    function: FunctionType,
    seen: set[int],
) -> None:
    if isinstance(value, CodeType):
        _audit_pure_schema_code(value, function=function, seen=seen)
        return
    if isinstance(value, str):
        if (
            value in _PURE_CALLABLE_REFLECTION_CONSTANTS
            or (value.startswith("__") and value.endswith("__"))
        ):
            raise EventSchemaError(
                f"event schema callable contains forbidden reflection constant: {value}"
            )
        return
    if value is None or isinstance(value, (bool, int, float, bytes)):
        return
    if isinstance(value, tuple):
        for item in value:
            _audit_pure_schema_constant(item, function=function, seen=seen)
        return
    raise EventSchemaError(
        f"event schema callable contains unsupported constant: {type(value).__name__}"
    )


def _is_pure_schema_constant(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return True
    if isinstance(value, tuple):
        return all(_is_pure_schema_constant(item) for item in value)
    return False


def _run_pure_upcaster(
    upcaster: EventUpcaster,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    produced = upcaster(_freeze_mapping(_thaw_mapping(_freeze_mapping(payload))))
    if not isinstance(produced, Mapping):
        raise TypeError("upcaster must return a mapping")
    return _thaw_mapping(_freeze_mapping(produced))


def _run_pure_validator(
    validator: PayloadValidator,
    payload: Mapping[str, Any],
) -> None:
    result = validator(_freeze_mapping(_thaw_mapping(_freeze_mapping(payload))))
    if result is not None:
        raise TypeError("custom validator must return None")
