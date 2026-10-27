from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    verified_atomic_write,
    verified_exclusive_file_lock,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_wait_approval import (
    authoritative_wait_approval_id,
)
from framework.harness.control_plane.state import HarnessRunSpec, run_spec_checksum
from framework.harness.graph.canonical import canonical_checksum, required_text
from framework.harness.waits.models import HarnessWaitScope, approval_event_ref_for
from framework.shared.time import parse_datetime
from interfaces.models.actor import ActorContext
from interfaces.services.harness_wait_service import (
    HarnessWaitActorScope,
    HarnessWaitApplicationError,
    HarnessWaitActorScopeResolverPort,
    HarnessWaitApprovalDecision,
    HarnessWaitApprovalResolverPort,
    HarnessWaitAuthorizationError,
    HarnessWaitControlPlanePort,
    HarnessWaitRequestError,
    HarnessWaitRuntimeBinding,
    HarnessWaitRuntimeResolverPort,
)


HARNESS_WAIT_RUNTIME_REGISTRY_SCHEMA = "newsroom.harness.wait-runtime/v1"
HARNESS_WAIT_APPROVAL_STORE_SCHEMA = "newsroom.harness.wait-approval/v1"


class HarnessWaitRuntimeStoreError(RuntimeError):
    """The durable runtime binding registry cannot be trusted or written."""


class HarnessWaitRuntimeUnavailableError(RuntimeError):
    """A persisted run exists but its executable binding is not available."""


@dataclass(frozen=True, slots=True)
class HarnessWaitRuntimeRegistration:
    """Immutable durable identity for one registered Graph runtime."""

    run_spec: HarnessRunSpec
    tenant_scope_ref: str
    identity_scope_ref: str
    registered_at: str
    registration_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        for field_name in ("tenant_scope_ref", "identity_scope_ref"):
            value = getattr(self, field_name)
            if not _is_checksum(value):
                raise ValueError(f"{field_name} must be a sha256 reference")
            declared = self.run_spec.metadata.get(field_name)
            if declared is not None and declared != value:
                raise HarnessWaitRuntimeStoreError(
                    f"runtime registration {field_name} does not match the run specification"
                )
        if not isinstance(self.registered_at, str) or not self.registered_at.strip():
            raise ValueError("registered_at is required")
        if parse_datetime(self.registered_at) is None:
            raise ValueError("registered_at must be a valid UTC timestamp")
        expected = _registration_ref(
            self.run_spec,
            tenant_scope_ref=self.tenant_scope_ref,
            identity_scope_ref=self.identity_scope_ref,
        )
        if self.registration_ref != expected:
            raise HarnessWaitRuntimeStoreError(
                "runtime registration checksum does not match its immutable content"
            )

    @property
    def graph_checksum(self) -> str:
        return self.run_spec.graph.definition_checksum

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_spec": self.run_spec.to_dict(),
            "run_spec_checksum": run_spec_checksum(self.run_spec),
            "graph_id": self.run_spec.graph.graph_id,
            "graph_version": self.run_spec.graph.graph_version,
            "graph_ref": f"{self.run_spec.graph.graph_id}@{self.run_spec.graph.graph_version}",
            "graph_checksum": self.graph_checksum,
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "registered_at": self.registered_at,
            "registration_ref": self.registration_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessWaitRuntimeRegistration":
        _exact_keys(
            value,
            {
                "run_spec",
                "run_spec_checksum",
                "graph_id",
                "graph_version",
                "graph_ref",
                "graph_checksum",
                "tenant_scope_ref",
                "identity_scope_ref",
                "registered_at",
                "registration_ref",
            },
            "runtime registration",
        )
        run_spec = HarnessRunSpec.from_dict(_mapping(value["run_spec"], "run_spec"))
        if value["run_spec_checksum"] != run_spec_checksum(run_spec):
            raise HarnessWaitRuntimeStoreError(
                "runtime registration run specification checksum is invalid"
            )
        graph_ref = f"{run_spec.graph.graph_id}@{run_spec.graph.graph_version}"
        if (
            value["graph_id"] != run_spec.graph.graph_id
            or value["graph_version"] != run_spec.graph.graph_version
            or value["graph_ref"] != graph_ref
            or value["graph_checksum"] != run_spec.graph.definition_checksum
        ):
            raise HarnessWaitRuntimeStoreError(
                "runtime registration Graph identity is invalid"
            )
        return cls(
            run_spec=run_spec,
            tenant_scope_ref=value["tenant_scope_ref"],
            identity_scope_ref=value["identity_scope_ref"],
            registered_at=value["registered_at"],
            registration_ref=value["registration_ref"],
        )


RuntimeRehydrator = Callable[
    [HarnessWaitRuntimeRegistration, ActorContext],
    HarnessWaitRuntimeBinding,
]


class HarnessWaitRuntimeRegistry(HarnessWaitRuntimeResolverPort):
    """Durable Graph run registry plus a process-owned live runtime cache.

    The JSON record is the authority for immutable run identity.  A control
    plane is never reconstructed from an inspection projection; after a
    process restart a caller must supply an explicit composition rehydrator or
    resolution fails closed.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        rehydrator: RuntimeRehydrator | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / "runtime-bindings.json"
        self._lock = RLock()
        self._rehydrator = rehydrator
        self._records = self._read_records()
        self._bindings: dict[str, HarnessWaitRuntimeBinding] = {}

    def register(
        self,
        run_spec: HarnessRunSpec,
        control_plane: HarnessWaitControlPlanePort,
        *,
        tenant_scope_ref: str,
        identity_scope_ref: str,
    ) -> None:
        if not isinstance(run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        if not isinstance(control_plane, HarnessWaitControlPlanePort):
            raise TypeError("control_plane must implement HarnessWaitControlPort")
        registration = HarnessWaitRuntimeRegistration(
            run_spec=run_spec,
            tenant_scope_ref=tenant_scope_ref,
            identity_scope_ref=identity_scope_ref,
            registered_at=run_spec.created_at.isoformat().replace("+00:00", "Z"),
            registration_ref=_registration_ref(
                run_spec,
                tenant_scope_ref=tenant_scope_ref,
                identity_scope_ref=identity_scope_ref,
            ),
        )
        binding = HarnessWaitRuntimeBinding(run_spec, control_plane)
        with self._lock:
            lock_path = self.root / "runtime-bindings.lock"
            try:
                with verified_exclusive_file_lock(
                    lock_path,
                    root=self.root,
                    identity="harness-wait-runtime-registry",
                ):
                    # Reload while holding the inter-process lock so two
                    # composition instances cannot overwrite each other's
                    # immutable registrations with stale in-memory snapshots.
                    records = self._read_records()
                    existing = records.get(run_spec.run_id)
                    if existing is not None and existing != registration:
                        raise HarnessWaitRuntimeStoreError(
                            "run id is already bound to different immutable Graph content"
                        )
                    records[run_spec.run_id] = registration
                    self._write_records_locked(records)
                    self._records = records
            except (OSError, ArtifactStoreMetadataError) as exc:
                raise HarnessWaitRuntimeStoreError(
                    "runtime registry could not be written"
                ) from exc
            self._bindings[run_spec.run_id] = binding

    def resolve(
        self,
        run_id: str,
        *,
        actor: ActorContext,
    ) -> HarnessWaitRuntimeBinding:
        run_id = required_text(run_id, "run_id")
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        with self._lock:
            registration = self._records.get(run_id)
            binding = self._bindings.get(run_id)
        if registration is None:
            raise HarnessWaitRuntimeUnavailableError(
                "no durable Graph runtime registration exists for this run"
            )
        if binding is None:
            if self._rehydrator is None:
                raise HarnessWaitRuntimeUnavailableError(
                    "durable Graph run exists but its executable binding is unavailable"
                )
            binding = self._rehydrator(registration, actor)
            if not isinstance(binding, HarnessWaitRuntimeBinding):
                raise HarnessWaitRuntimeStoreError(
                    "runtime rehydrator returned an invalid binding"
                )
            self._validate_binding(registration, binding)
            with self._lock:
                self._bindings[run_id] = binding
        self._validate_binding(registration, binding)
        return binding

    def registration(self, run_id: str) -> HarnessWaitRuntimeRegistration | None:
        with self._lock:
            return self._records.get(run_id)

    def close(self) -> None:
        with self._lock:
            self._bindings.clear()

    def _validate_binding(
        self,
        registration: HarnessWaitRuntimeRegistration,
        binding: HarnessWaitRuntimeBinding,
    ) -> None:
        if binding.run_spec.run_id != registration.run_spec.run_id:
            raise HarnessWaitRuntimeStoreError("runtime binding run id mismatch")
        if run_spec_checksum(binding.run_spec) != run_spec_checksum(registration.run_spec):
            raise HarnessWaitRuntimeStoreError("runtime binding specification checksum mismatch")
        if binding.run_spec.graph.definition_checksum != registration.graph_checksum:
            raise HarnessWaitRuntimeStoreError("runtime binding Graph checksum mismatch")

    def _read_records(self) -> dict[str, HarnessWaitRuntimeRegistration]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("registry payload must be an object")
            _exact_keys(payload, {"schema_version", "registrations"}, "runtime registry")
            if payload["schema_version"] != HARNESS_WAIT_RUNTIME_REGISTRY_SCHEMA:
                raise ValueError("unsupported runtime registry schema")
            entries = payload["registrations"]
            if not isinstance(entries, list):
                raise ValueError("runtime registry registrations must be an array")
            records = [
                HarnessWaitRuntimeRegistration.from_dict(_mapping(item, "registration"))
                for item in entries
            ]
            return {record.run_spec.run_id: record for record in records}
        except (OSError, ValueError, TypeError, KeyError, HarnessValidationError) as exc:
            raise HarnessWaitRuntimeStoreError("runtime registry is corrupt") from exc

    def _write_records_locked(
        self,
        records: Mapping[str, HarnessWaitRuntimeRegistration] | None = None,
    ) -> None:
        current_records = self._records if records is None else records
        payload = {
            "schema_version": HARNESS_WAIT_RUNTIME_REGISTRY_SCHEMA,
            "registrations": [
                item.to_dict()
                for item in sorted(
                    current_records.values(),
                    key=lambda record: record.run_spec.run_id,
                )
            ],
        }
        content = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        verified_atomic_write(
            self.path,
            content,
            root=self.root,
            identity="harness-wait-runtime-registry",
        )


class DurableHarnessWaitApprovalResolver(HarnessWaitApprovalResolverPort):
    """Resolve approval decisions from the current durable Wait projection."""

    def __init__(
        self,
        *,
        runtime_resolver: HarnessWaitRuntimeResolverPort,
        actor_scope_resolver: HarnessWaitActorScopeResolverPort,
        root: str | Path,
    ) -> None:
        if not isinstance(runtime_resolver, HarnessWaitRuntimeResolverPort):
            raise TypeError("runtime_resolver must implement HarnessWaitRuntimeResolverPort")
        if not isinstance(actor_scope_resolver, HarnessWaitActorScopeResolverPort):
            raise TypeError(
                "actor_scope_resolver must implement HarnessWaitActorScopeResolverPort"
            )
        self._runtime_resolver = runtime_resolver
        self._actor_scope_resolver = actor_scope_resolver
        self._store = _ApprovalDecisionStore(Path(root) / "approval-decisions.json")

    def resolve(
        self,
        approval_id: str,
        *,
        run_id: str,
        node_instance_id: str,
        actor: ActorContext,
        requested_approved: bool,
    ) -> HarnessWaitApprovalDecision:
        approval_id = required_text(approval_id, "approval_id")
        run_id = required_text(run_id, "run_id")
        node_instance_id = required_text(node_instance_id, "node_instance_id")
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        if not isinstance(requested_approved, bool):
            raise TypeError("requested_approved must be boolean")
        try:
            existing = self._store.get(approval_id)
            binding = self._runtime_resolver.resolve(run_id, actor=actor)
        except (HarnessWaitRuntimeStoreError, HarnessWaitRuntimeUnavailableError) as exc:
            raise HarnessWaitApplicationError(
                "approval runtime binding or durable decision store is unavailable",
                code="wait_runtime_resolver_unavailable",
            ) from exc
        state = binding.control_plane.recover_graph(binding.run_spec)
        registration = _approval_registration(
            state,
            node_instance_id,
            require_unresolved=existing is None,
        )
        if registration is None:
            raise HarnessWaitRequestError(
                "approval Wait is stale or no longer registered",
                code="wait_approval_stale",
            )
        scope = HarnessWaitScope(
            wait_id=registration.wait_id,
            run_id=state.run_id,
            node_instance_id=registration.node_instance_id,
            tenant_scope_ref=registration.tenant_scope_ref,
            identity_scope_ref=registration.identity_scope_ref,
            signal_schema_ref=registration.signal_schema_ref,
            correlation_ref=registration.correlation_ref,
        )
        actor_scope = self._actor_scope_resolver.resolve(actor)
        if not isinstance(actor_scope, HarnessWaitActorScope):
            raise HarnessWaitAuthorizationError(
                "actor scope resolver returned an invalid scope",
                code="wait_actor_scope_resolver_invalid",
            )
        if (
            scope.tenant_scope_ref != actor_scope.tenant_scope_ref
            or scope.identity_scope_ref != actor_scope.identity_scope_ref
        ):
            raise HarnessWaitAuthorizationError(
                "approval Wait is outside the authorized actor scope",
                code="wait_approval_evidence_unauthorized",
            )
        authoritative_id = authoritative_wait_approval_id(
            state,
            node_instance_id,
            graph_inputs=binding.run_spec.inputs,
        )
        # Once the Harness has durably resumed a Wait, the resolved state no
        # longer carries the original correlation payload. A stored decision
        # is the only trusted approval-id witness for an identical retry; the
        # current Graph identity/scope/event reference checks below still
        # prevent cross-Graph or forged replay.
        if authoritative_id is None and existing is not None:
            authoritative_id = existing.approval_id
        if authoritative_id is None or authoritative_id != approval_id:
            raise HarnessWaitAuthorizationError(
                "approval id does not match the authoritative Graph Wait",
                code="wait_approval_id_mismatch",
            )
        graph_ref = state.graph_ref
        event_ref = approval_event_ref_for(
            approval_id=approval_id,
            scope=scope,
            actor_identity_scope_ref=actor_scope.actor_identity_scope_ref,
            approved=requested_approved,
            graph_id=graph_ref.graph_id,
            graph_version=graph_ref.identity_version,
            graph_ref=graph_ref.identity_ref.exact_ref,
            graph_checksum=graph_ref.checksum,
        )
        decision = HarnessWaitApprovalDecision(
            approval_id=approval_id,
            run_id=state.run_id,
            node_instance_id=node_instance_id,
            approval_event_ref=event_ref,
            actor_identity_scope_ref=actor_scope.actor_identity_scope_ref,
            approved=requested_approved,
            graph_id=graph_ref.graph_id,
            graph_version=graph_ref.identity_version,
            graph_ref=graph_ref.identity_ref.exact_ref,
            graph_checksum=graph_ref.checksum,
        )
        if existing is not None:
            if _decision_projection(existing) == _decision_projection(decision):
                return existing
            raise HarnessWaitRequestError(
                "approval id already has another durable decision",
                code="wait_approval_decision_conflict",
            )
        try:
            self._store.put(decision)
        except HarnessWaitRuntimeStoreError as exc:
            raise HarnessWaitApplicationError(
                "approval decision store is unavailable",
                code="wait_approval_store_unavailable",
            ) from exc
        return decision


class _ApprovalDecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def get(self, approval_id: str) -> HarnessWaitApprovalDecision | None:
        with self._lock:
            records = self._read()
            return records.get(approval_id)

    def put(self, decision: HarnessWaitApprovalDecision) -> None:
        with self._lock:
            try:
                with verified_exclusive_file_lock(
                    self.path.with_name("approval-decisions.lock"),
                    root=self.path.parent,
                    identity="harness-wait-approval-store",
                ):
                    # Read and compare under the same lock as the atomic
                    # write; retries from separate workers cannot lose a
                    # decision or silently replace a conflicting one.
                    records = self._read()
                    existing = records.get(decision.approval_id)
                    if existing is not None and _decision_projection(existing) != _decision_projection(decision):
                        raise HarnessWaitRequestError(
                            "approval id already has another durable decision",
                            code="wait_approval_decision_conflict",
                        )
                    records[decision.approval_id] = decision
                    payload = {
                        "schema_version": HARNESS_WAIT_APPROVAL_STORE_SCHEMA,
                        "decisions": [
                            _decision_to_dict(item)
                            for item in sorted(
                                records.values(), key=lambda value: value.approval_id
                            )
                        ],
                    }
                    content = (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                    ).encode("utf-8")
                    verified_atomic_write(
                        self.path,
                        content,
                        root=self.path.parent,
                        identity="harness-wait-approval-store",
                    )
            except (OSError, ArtifactStoreMetadataError) as exc:
                raise HarnessWaitRuntimeStoreError(
                    "approval decision store could not be written"
                ) from exc

    def _read(self) -> dict[str, HarnessWaitApprovalDecision]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            _exact_keys(payload, {"schema_version", "decisions"}, "approval decision store")
            if payload["schema_version"] != HARNESS_WAIT_APPROVAL_STORE_SCHEMA:
                raise ValueError("unsupported approval decision store schema")
            decisions = payload["decisions"]
            if not isinstance(decisions, list):
                raise ValueError("approval decisions must be an array")
            parsed = [_decision_from_dict(_mapping(item, "approval decision")) for item in decisions]
            return {item.approval_id: item for item in parsed}
        except (OSError, ValueError, TypeError, KeyError, HarnessValidationError) as exc:
            raise HarnessWaitRuntimeStoreError("approval decision store is corrupt") from exc


def _approval_registration(
    state: Any,
    node_instance_id: str,
    *,
    require_unresolved: bool = True,
) -> Any | None:
    for registration in state.wait_registrations:
        if (
            registration.node_instance_id == node_instance_id
            and registration.kind.value == "approval"
            and (not require_unresolved or registration.unresolved)
        ):
            return registration
    return None


def _registration_ref(
    run_spec: HarnessRunSpec,
    *,
    tenant_scope_ref: str,
    identity_scope_ref: str,
) -> str:
    return canonical_checksum(
        {
            "run_spec_checksum": run_spec_checksum(run_spec),
            "graph_checksum": run_spec.graph.definition_checksum,
            "tenant_scope_ref": _checksum(tenant_scope_ref),
            "identity_scope_ref": _checksum(identity_scope_ref),
        }
    )


def _decision_to_dict(decision: HarnessWaitApprovalDecision) -> dict[str, Any]:
    return {
        "approval_id": decision.approval_id,
        "run_id": decision.run_id,
        "node_instance_id": decision.node_instance_id,
        "approval_event_ref": decision.approval_event_ref,
        "actor_identity_scope_ref": decision.actor_identity_scope_ref,
        "approved": decision.approved,
        "graph_id": decision.graph_id,
        "graph_version": decision.graph_version,
        "graph_ref": decision.graph_ref,
        "graph_checksum": decision.graph_checksum,
    }


def _decision_from_dict(value: Mapping[str, Any]) -> HarnessWaitApprovalDecision:
    _exact_keys(
        value,
        {
            "approval_id",
            "run_id",
            "node_instance_id",
            "approval_event_ref",
            "actor_identity_scope_ref",
            "approved",
            "graph_id",
            "graph_version",
            "graph_ref",
            "graph_checksum",
        },
        "approval decision",
    )
    return HarnessWaitApprovalDecision(**dict(value))


def _decision_projection(
    decision: HarnessWaitApprovalDecision,
    *,
    run_id: str | None = None,
    node_instance_id: str | None = None,
    approved: bool | None = None,
) -> tuple[Any, ...]:
    return (
        decision.approval_id,
        decision.run_id if run_id is None else run_id,
        decision.node_instance_id if node_instance_id is None else node_instance_id,
        decision.approval_event_ref,
        decision.actor_identity_scope_ref,
        decision.approved if approved is None else approved,
        decision.graph_id,
        decision.graph_version,
        decision.graph_ref,
        decision.graph_checksum,
    )


def _checksum(value: Any) -> str:
    if not _is_checksum(value):
        raise ValueError("value must be a sha256 reference")
    return str(value)


def _is_checksum(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:") and all(
        char in "0123456789abcdef" for char in value[7:]
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{field_name} fields do not match the versioned contract")


__all__ = [
    "DurableHarnessWaitApprovalResolver",
    "HARNESS_WAIT_APPROVAL_STORE_SCHEMA",
    "HARNESS_WAIT_RUNTIME_REGISTRY_SCHEMA",
    "HarnessWaitRuntimeRegistration",
    "HarnessWaitRuntimeRegistry",
    "HarnessWaitRuntimeStoreError",
    "HarnessWaitRuntimeUnavailableError",
]
