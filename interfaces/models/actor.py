from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


ActorType = Literal["user", "service", "mcp_client", "system", "anonymous"]

READ_REPORTS_PERMISSION = "read:reports"
WRITE_RUNS_PERMISSION = "write:runs"
MANAGE_SCHEDULES_PERMISSION = "manage:schedules"
MANAGE_APPROVALS_PERMISSION = "manage:approvals"
ADMIN_STORAGE_PERMISSION = "admin:storage"
READ_EVENTS_PERMISSION = "events:read"
OPERATE_EVENTS_PERMISSION = "events:operate"
RESEARCH_PAPER_PARSE_PERMISSION = "research.paper.parse"
RESEARCH_PAPER_INGEST_PERMISSION = "research.paper.ingest"
RESEARCH_PAPER_REFRESH_PERMISSION = "research.paper.refresh"
RESEARCH_CATALOG_READ_PERMISSION = "research.catalog.read"
RESEARCH_CATALOG_VERIFY_PERMISSION = "research.catalog.verify"
RESEARCH_CATALOG_EXPORT_PERMISSION = "research.catalog.export"
RESEARCH_DIAGNOSTICS_READ_PERMISSION = "research.diagnostics.read"
RESEARCH_ARTIFACT_READ_PERMISSION = "research.artifact.read"
RESEARCH_EVENT_REPLAY_PERMISSION = "research.event.replay"

PERMISSION_ALIASES: dict[str, set[str]] = {
    READ_REPORTS_PERMISSION: {
        "runs:read",
        "reports:read",
        "sources:read",
        "memory:search",
        "workers:read",
        "schedules:read",
        "approvals:read",
        "mcp:read",
        "storage:read",
        "entities:read",
        "subscriptions:read",
    },
    WRITE_RUNS_PERMISSION: {"runs:create", "runs:cancel"},
    MANAGE_SCHEDULES_PERMISSION: {"schedules:write"},
    MANAGE_APPROVALS_PERMISSION: {"reports:publish", "approvals:decide"},
    ADMIN_STORAGE_PERMISSION: {"storage:read"},
    RESEARCH_CATALOG_READ_PERMISSION: {READ_REPORTS_PERMISSION},
    RESEARCH_PAPER_PARSE_PERMISSION: {WRITE_RUNS_PERMISSION},
    RESEARCH_PAPER_INGEST_PERMISSION: {WRITE_RUNS_PERMISSION},
    RESEARCH_PAPER_REFRESH_PERMISSION: {WRITE_RUNS_PERMISSION},
    RESEARCH_CATALOG_VERIFY_PERMISSION: {MANAGE_APPROVALS_PERMISSION},
    RESEARCH_CATALOG_EXPORT_PERMISSION: {READ_REPORTS_PERMISSION},
    RESEARCH_DIAGNOSTICS_READ_PERMISSION: {READ_REPORTS_PERMISSION, "admin:diagnose"},
    RESEARCH_ARTIFACT_READ_PERMISSION: {READ_REPORTS_PERMISSION},
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {
        READ_REPORTS_PERMISSION,
    },
    "admin": {
        READ_REPORTS_PERMISSION,
        WRITE_RUNS_PERMISSION,
        MANAGE_SCHEDULES_PERMISSION,
        MANAGE_APPROVALS_PERMISSION,
        ADMIN_STORAGE_PERMISSION,
        "runs:create",
        "runs:read",
        "runs:cancel",
        "reports:read",
        "reports:publish",
        "sources:read",
        "sources:write",
        "memory:search",
        "workers:read",
        "schedules:read",
        "schedules:write",
        "approvals:read",
        "admin:diagnose",
        "mcp:read",
        "storage:read",
        "entities:read",
        "entities:write",
        "subscriptions:read",
        "subscriptions:write",
        READ_EVENTS_PERMISSION,
        OPERATE_EVENTS_PERMISSION,
    },
    "operator": {
        READ_REPORTS_PERMISSION,
        WRITE_RUNS_PERMISSION,
        MANAGE_SCHEDULES_PERMISSION,
        "runs:create",
        "runs:read",
        "runs:cancel",
        "reports:read",
        "sources:read",
        "sources:write",
        "memory:search",
        "workers:read",
        "schedules:read",
        "schedules:write",
        "approvals:read",
        "admin:diagnose",
        "mcp:read",
        "storage:read",
        "entities:read",
        "entities:write",
        "subscriptions:read",
        "subscriptions:write",
        READ_EVENTS_PERMISSION,
        OPERATE_EVENTS_PERMISSION,
    },
    "developer": {
        READ_REPORTS_PERMISSION,
        WRITE_RUNS_PERMISSION,
        "runs:create",
        "runs:read",
        "reports:read",
        "sources:read",
        "memory:search",
        "workers:read",
        "admin:diagnose",
        "mcp:read",
        "storage:read",
        "entities:read",
        "subscriptions:read",
    },
    "reviewer": {
        READ_REPORTS_PERMISSION,
        MANAGE_APPROVALS_PERMISSION,
        "runs:read",
        "reports:read",
        "approvals:read",
        "approvals:decide",
    },
    "read-only": {
        READ_REPORTS_PERMISSION,
        "runs:read",
        "reports:read",
        "sources:read",
        "memory:search",
        "workers:read",
        "schedules:read",
        "approvals:read",
        "mcp:read",
        "storage:read",
        "entities:read",
        "subscriptions:read",
    },
    "analyst_readonly": {
        READ_REPORTS_PERMISSION,
        "runs:read",
        "reports:read",
        "sources:read",
        "memory:search",
        "workers:read",
        "schedules:read",
        "approvals:read",
        "mcp:read",
        "storage:read",
        "entities:read",
        "subscriptions:read",
    },
    "mcp_client": {
        READ_REPORTS_PERMISSION,
        "mcp:read",
        "runs:read",
        "reports:read",
        "sources:read",
        "memory:search",
        "workers:read",
    },
    "service": {
        READ_REPORTS_PERMISSION,
        WRITE_RUNS_PERMISSION,
        MANAGE_SCHEDULES_PERMISSION,
        MANAGE_APPROVALS_PERMISSION,
        ADMIN_STORAGE_PERMISSION,
        "runs:create",
        "runs:read",
        "reports:read",
        "reports:publish",
        "sources:read",
        "sources:write",
        "memory:search",
        "workers:read",
        "schedules:read",
        "schedules:write",
        "approvals:read",
        "approvals:decide",
        "admin:diagnose",
        "mcp:read",
        "storage:read",
        "entities:read",
        "entities:write",
        "subscriptions:read",
        "subscriptions:write",
        READ_EVENTS_PERMISSION,
        OPERATE_EVENTS_PERMISSION,
    },
}


class ActorContext(BaseModel):
    actor_id: str
    actor_type: ActorType
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    request_id: str
    ip_address: str | None = None
    user_agent: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def has_permission(self, permission: str) -> bool:
        required = _equivalent_permissions(permission)
        return bool(required.intersection(self.effective_permissions))

    @property
    def effective_permissions(self) -> set[str]:
        permissions = set(self.permissions)
        for role in self.roles:
            permissions.update(ROLE_PERMISSIONS.get(role, set()))
        expanded = set(permissions)
        for permission in permissions:
            expanded.update(_equivalent_permissions(permission))
        # Research replay is a one-way grant of both event operations. A read
        # grant alone must never imply write access through a shared alias.
        if RESEARCH_EVENT_REPLAY_PERMISSION in permissions:
            expanded.update({READ_EVENTS_PERMISSION, OPERATE_EVENTS_PERMISSION})
        return expanded


def _equivalent_permissions(permission: str) -> set[str]:
    equivalents = {permission}
    aliases = PERMISSION_ALIASES.get(permission)
    if aliases:
        equivalents.update(aliases)
    for canonical, legacy_aliases in PERMISSION_ALIASES.items():
        if permission in legacy_aliases:
            equivalents.add(canonical)
            equivalents.update(legacy_aliases)
    return equivalents


def actor_context_from_headers(
    headers: Mapping[str, str],
    *,
    request_id: str,
    ip_address: str | None = None,
) -> ActorContext:
    actor_id = _header(headers, "x-news-actor") or _header(headers, "x-api-client-id")
    actor_type = _header(headers, "x-news-actor-type") or "anonymous"
    roles = _csv_header(headers, "x-news-roles")
    permissions = _csv_header(headers, "x-news-permissions")
    if not actor_id and _header(headers, "authorization"):
        actor_id = "api-token"
        actor_type = "service"
        if not roles:
            roles = ["service"]
    if not actor_id:
        actor_id = "anonymous"
    return ActorContext(
        actor_id=actor_id,
        actor_type=_actor_type(actor_type),
        roles=roles,
        permissions=permissions,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=_header(headers, "user-agent"),
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            text = str(value).strip()
            return text or None
    return None


def _csv_header(headers: Mapping[str, str], name: str) -> list[str]:
    value = _header(headers, name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _actor_type(value: str) -> ActorType:
    normalized = value.strip().lower()
    if normalized in {"user", "service", "mcp_client", "system", "anonymous"}:
        return normalized  # type: ignore[return-value]
    return "anonymous"
