from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from framework.events.canonical import checksum_for
from framework.events.application import DurableGraphEventProjectionAdapter
from framework.events.ports import EventStorePort
from framework.events.runtime.authorization import (
    RetirementCancellationAuthorizationDecision,
    RetirementCancellationAuthorizationRequest,
)
from framework.events.runtime.delivery import DurableDeliveryRuntime
from framework.events.schema import EventSchemaCatalog
from framework.shared.time import utc_now
from interfaces.models.actor import ActorContext
from interfaces.services.event_delivery_operations_service import (
    EventDeliveryOperationsService,
    EventDeliveryRuntimePort,
    RetirementCancellationRuntimePort,
)
from interfaces.services.event_operator_service import EventOperatorApplicationService
from interfaces.services.event_projection_service import EventProjectionService
from interfaces.services.event_quarantine_service import EventQuarantineService
from interfaces.services.event_reader_service import (
    EventAuthorizationContext,
    EventAuthorizationDecision,
    EventAuthorizationRequest,
    EventPermission,
)
from interfaces.services.event_replay_service import EventReplayService
from infrastructure.storage.artifacts.graph_terminal import (
    FilesystemGraphTerminalArtifactStore,
)


EVENTS_READ_PERMISSION = "events:read"
EVENTS_OPERATE_PERMISSION = "events:operate"

_READ_OPERATIONS = frozenset(
    {
        EventPermission.READ,
        EventPermission.PROJECTION_READ,
        EventPermission.REPLAY_READ,
        EventPermission.DEAD_LETTER_READ,
        EventPermission.DELIVERY_STATUS_READ,
        EventPermission.QUARANTINE_READ,
    }
)
_OPERATE_OPERATIONS = frozenset(
    {
        EventPermission.PROJECTION_REBUILD,
        EventPermission.REPLAY_START,
        EventPermission.DEAD_LETTER_REQUEUE,
        EventPermission.DEAD_LETTER_RESOLVE,
        EventPermission.REDELIVER,
        EventPermission.SUBSCRIPTION_RETIREMENT_CANCEL,
        EventPermission.QUARANTINE_RESOLVE,
    }
)


class DurableEventOperatorStorage(Protocol):
    event_store: EventStorePort
    schema_catalog: EventSchemaCatalog

    def create_replay_engine(self): ...


class EventOperatorEventAuthorizer:
    """Bind event permissions to one verified actor and deployment tenant."""

    def __init__(
        self,
        *,
        actor: ActorContext,
        authorization: EventAuthorizationContext,
        evidence_scope_digest: str,
    ) -> None:
        self._actor_type = actor.actor_type
        self._permissions = frozenset(actor.effective_permissions)
        self._authorization = authorization
        self._evidence_scope_digest = evidence_scope_digest

    def authorize(
        self,
        request: EventAuthorizationRequest,
    ) -> EventAuthorizationDecision:
        if not isinstance(request, EventAuthorizationRequest):
            raise TypeError("request must be EventAuthorizationRequest")
        exact_scope = (
            request.principal_id == self._authorization.principal_id
            and request.tenant_id == self._authorization.tenant_id
            and request.authentication_evidence_ref
            == self._authorization.authentication_evidence_ref
        )
        required_permission = _required_actor_permission(request.operation)
        authorized = bool(
            exact_scope
            and self._actor_type != "anonymous"
            and required_permission is not None
            and required_permission in self._permissions
        )
        return EventAuthorizationDecision(
            request=request,
            authorized=authorized,
            authorization_evidence_ref=(
                _event_operator_evidence_ref(
                    self._evidence_scope_digest,
                    request.operation,
                    request.request_checksum,
                )
                if authorized
                else None
            ),
            denial_reason_class=None if authorized else "operation_not_allowed",
        )


class EventOperatorRetirementCancellationAuthorizer:
    """Re-authorize the runtime command against the verified actor snapshot."""

    def __init__(
        self,
        *,
        actor: ActorContext,
        authorization: EventAuthorizationContext,
        evidence_scope_digest: str,
    ) -> None:
        self._actor_type = actor.actor_type
        self._permissions = frozenset(actor.effective_permissions)
        self._principal_id = actor.actor_id
        self._authorization = authorization
        self._evidence_scope_digest = evidence_scope_digest

    def authorize(
        self,
        request: RetirementCancellationAuthorizationRequest,
    ) -> RetirementCancellationAuthorizationDecision:
        if not isinstance(request, RetirementCancellationAuthorizationRequest):
            raise TypeError(
                "request must be RetirementCancellationAuthorizationRequest"
            )
        request.verify_integrity()
        application_request = EventAuthorizationRequest(
            principal_id=self._principal_id,
            tenant_id=self._authorization.tenant_id,
            authentication_evidence_ref=(
                self._authorization.authentication_evidence_ref
            ),
            operation=EventPermission.SUBSCRIPTION_RETIREMENT_CANCEL,
            target={
                "cancellation_id": request.cancellation_id,
                "subscription_id": request.subscription.subscription_id,
                "subscription_version": request.subscription.subscription_version,
                "operator_reason": request.operator_reason,
                "limit": request.limit,
            },
        )
        expected_application_evidence = _event_operator_evidence_ref(
            self._evidence_scope_digest,
            EventPermission.SUBSCRIPTION_RETIREMENT_CANCEL,
            application_request.request_checksum,
        )
        authorized = bool(
            self._actor_type != "anonymous"
            and EVENTS_OPERATE_PERMISSION in self._permissions
            and request.operator_id == self._principal_id
            and request.tenant_id == self._authorization.tenant_id
            and request.authorization_evidence_ref == expected_application_evidence
        )
        return RetirementCancellationAuthorizationDecision(
            request=request,
            authorized=authorized,
            decided_at=request.requested_at,
            authorization_evidence_ref=(
                request.authorization_evidence_ref if authorized else None
            ),
            denial_reason_class=None if authorized else "operation_not_allowed",
        )


def event_operator_service_from_actor(
    actor: ActorContext,
    *,
    env: Mapping[str, str] | None = None,
    delivery_runtime: EventDeliveryRuntimePort | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> EventOperatorApplicationService:
    """Compose one tenant-scoped operator service from one storage bundle."""

    if not isinstance(actor, ActorContext):
        raise TypeError("actor must be ActorContext")
    if not callable(clock):
        raise TypeError("clock must be callable")
    values = env if env is not None else os.environ
    tenant_id = _required_config(values, "NEWS_TENANT_ID")
    configured_root = str(values.get("NEWS_ARTIFACT_ROOT") or "").strip()
    artifact_root = Path(configured_root or ".newsroom/runs")

    from infrastructure.storage.events import factory as storage_factory

    storage = storage_factory.durable_event_storage_from_env(
        artifact_root=artifact_root,
        env=values,
    )
    return build_event_operator_service(
        actor,
        tenant_id=tenant_id,
        artifact_root=artifact_root,
        event_storage=storage,
        delivery_runtime=delivery_runtime,
        clock=clock,
    )


def build_event_operator_service(
    actor: ActorContext,
    *,
    tenant_id: str,
    artifact_root: str | Path,
    event_storage: DurableEventOperatorStorage,
    delivery_runtime: EventDeliveryRuntimePort | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> EventOperatorApplicationService:
    """Bind an actor to an already composed long-lived storage/runtime pair."""

    if not isinstance(actor, ActorContext):
        raise TypeError("actor must be ActorContext")
    if event_storage is None:
        raise ValueError("event_storage is required")
    if not isinstance(event_storage.schema_catalog, EventSchemaCatalog):
        raise TypeError("event storage schema_catalog is invalid")
    if not callable(clock):
        raise TypeError("clock must be callable")
    normalized_tenant_id = str(tenant_id or "").strip()
    if not normalized_tenant_id:
        raise ValueError("tenant_id is required for event operator composition")
    store = event_storage.event_store
    if store is None:
        raise ValueError("event storage event_store is required")
    _validate_delivery_runtime_store(delivery_runtime, store=store)

    replay_engine = None
    create_replay_engine = getattr(event_storage, "create_replay_engine", None)
    if callable(create_replay_engine):
        replay_engine = create_replay_engine()

    digest = _actor_scope_digest(actor, tenant_id=normalized_tenant_id)
    authorization = EventAuthorizationContext(
        principal_id=actor.actor_id,
        tenant_id=normalized_tenant_id,
        authentication_evidence_ref=f"authn://actor-context/{digest}",
    )
    authorizer = EventOperatorEventAuthorizer(
        actor=actor,
        authorization=authorization,
        evidence_scope_digest=digest,
    )
    retirement_cancellation_runtime: RetirementCancellationRuntimePort = (
        DurableDeliveryRuntime(
            store,
            retirement_cancellation_authorizer=(
                EventOperatorRetirementCancellationAuthorizer(
                    actor=actor,
                    authorization=authorization,
                    evidence_scope_digest=digest,
                )
            ),
        )
    )
    return EventOperatorApplicationService(
        authorization=authorization,
        quarantine=EventQuarantineService(
            store,
            authorizer=authorizer,
            clock=clock,
        ),
        replay=EventReplayService(
            engine=replay_engine,
            checkpoint_reader=getattr(event_storage, "replay_checkpoint_store", None),
            report_store=store,
            authorizer=authorizer,
            clock=clock,
        ),
        delivery=EventDeliveryOperationsService(
            store=store,
            runtime=delivery_runtime,
            retirement_cancellation_runtime=retirement_cancellation_runtime,
            authorizer=authorizer,
            clock=clock,
        ),
        projection=EventProjectionService(
            reader=store,
            authorizer=authorizer,
            artifact_root=Path(artifact_root),
            schema_catalog=event_storage.schema_catalog,
            projection=DurableGraphEventProjectionAdapter(
                reader=store,
                schema_catalog=event_storage.schema_catalog,
            ),
            terminal_manifest_reader=FilesystemGraphTerminalArtifactStore(
                artifact_root
            ),
        ),
    )


def event_operator_service_from_env(
    *,
    env: Mapping[str, str] | None = None,
    delivery_runtime: EventDeliveryRuntimePort | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> EventOperatorApplicationService:
    """Compose a trusted CLI/stdio operator identity from deployment config."""

    values = env if env is not None else os.environ
    principal_id = _required_config(values, "NEWS_EVENT_OPERATOR_PRINCIPAL_ID")
    _required_config(values, "NEWS_TENANT_ID")
    actor = ActorContext(
        actor_id=principal_id,
        actor_type="service",
        roles=["service"],
        permissions=[EVENTS_READ_PERMISSION, EVENTS_OPERATE_PERMISSION],
        request_id="event-operator-deployment",
    )
    return event_operator_service_from_actor(
        actor,
        env=values,
        delivery_runtime=delivery_runtime,
        clock=clock,
    )


def _required_actor_permission(operation: EventPermission) -> str | None:
    if operation in _READ_OPERATIONS:
        return EVENTS_READ_PERMISSION
    if operation in _OPERATE_OPERATIONS:
        return EVENTS_OPERATE_PERMISSION
    return None


def _event_operator_evidence_ref(
    scope_digest: str,
    operation: EventPermission,
    request_checksum: str,
) -> str:
    checksum = checksum_for(
        {
            "scope_digest": scope_digest,
            "operation": operation.value,
            "request_checksum": request_checksum,
        }
    ).removeprefix("sha256:")
    return f"authz://event-operator/{checksum}"


def _actor_scope_digest(actor: ActorContext, *, tenant_id: str) -> str:
    checksum = checksum_for(
        {
            "actor_id": actor.actor_id,
            "actor_type": actor.actor_type,
            "roles": sorted(set(actor.roles)),
            "permissions": sorted(actor.effective_permissions),
            "request_id": actor.request_id,
            "tenant_id": tenant_id,
        }
    )
    return checksum.removeprefix("sha256:")


def _validate_delivery_runtime_store(
    runtime: EventDeliveryRuntimePort | None,
    *,
    store: EventStorePort,
) -> None:
    if runtime is None:
        return
    registry = getattr(runtime, "consumers", None)
    if registry is None or getattr(registry, "store", None) is not store:
        raise ValueError(
            "delivery runtime consumer registry must share event storage"
        )


def _required_config(values: Mapping[str, str], key: str) -> str:
    value = str(values.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required for event operator composition")
    return value


__all__ = [
    "DurableEventOperatorStorage",
    "EVENTS_OPERATE_PERMISSION",
    "EVENTS_READ_PERMISSION",
    "EventOperatorEventAuthorizer",
    "EventOperatorRetirementCancellationAuthorizer",
    "build_event_operator_service",
    "event_operator_service_from_actor",
    "event_operator_service_from_env",
]
