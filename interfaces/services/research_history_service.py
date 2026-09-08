from __future__ import annotations

import math
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from interfaces.services.json_file_store import (
    JsonFileInvalidError,
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)
from interfaces.services.research_workspace_model import activity_adapter, validate_workspace_items


DEFAULT_RESEARCH_HISTORY_PATH = ".newsroom/research/history.json"
RESEARCH_HISTORY_SCHEMA_VERSION = "newsroom_research_history.v1"
MAX_VISITS = 2000
MAX_GROUPS = 100
MAX_BODY_BYTES = 4 * 1024 * 1024

_MODULE_PATHS: dict[str, frozenset[str]] = {
    "papers": frozenset({"/design-demo/papers", "/papers"}),
    "projects": frozenset({"/projects"}),
    "community": frozenset({"/community"}),
    "reports": frozenset({"/reports"}),
}
_MODULES = frozenset(_MODULE_PATHS)
_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_MAX_SAFE_INTEGER = 2**53 - 1


class ResearchHistoryError(ValueError):
    code = "research_history_invalid"
    status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ResearchHistoryConflictError(ResearchHistoryError):
    code = "research_history_conflict"
    status_code = 409


class ResearchHistoryStorageError(RuntimeError):
    code = "research_history_unavailable"
    status_code = 503


@dataclass(frozen=True)
class ResearchHistorySnapshot:
    revision: int
    visits: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    workspace_items: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "visits": self.visits,
            "groups": self.groups,
            **self.workspace_items,
        }


class ResearchHistoryService:
    """Durable, user-scoped research history with compare-and-swap writes."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("NEWSROOM_RESEARCH_HISTORY_PATH") or DEFAULT_RESEARCH_HISTORY_PATH)

    def read(self, *, user_id: str) -> ResearchHistorySnapshot:
        owner = _require_user_id(user_id)
        try:
            with locked_json_file(self.path) as path:
                payload = _read_payload(path)
                return _snapshot_for_owner(payload, owner)
        except (OSError, JsonFileInvalidError, TypeError, ValueError) as exc:
            raise ResearchHistoryStorageError("research history storage is unavailable") from exc

    def replace(
        self,
        *,
        user_id: str,
        revision: int,
        visits: Sequence[Mapping[str, Any]],
        groups: Sequence[Mapping[str, Any]],
        workspace_items: Mapping[str, Any] | None = None,
    ) -> ResearchHistorySnapshot:
        owner = _require_user_id(user_id)
        requested_revision = _validate_revision(revision)
        validated_visits = _validate_visits(visits)
        validated_groups = _validate_groups(groups)
        group_ids = {item["id"] for item in validated_groups}
        if any(item["groupId"] is not None and item["groupId"] not in group_ids for item in validated_visits):
            raise ResearchHistoryError("visit groupId must reference an existing group")
        try:
            with locked_json_file(self.path) as path:
                payload = _read_payload(path)
                try:
                    current = _snapshot_for_owner(payload, owner)
                except ResearchHistoryError as exc:
                    raise ResearchHistoryStorageError("research history storage is unavailable") from exc
                if requested_revision != current.revision:
                    raise ResearchHistoryConflictError(
                        "research history has changed; reload before saving",
                        details={"revision": current.revision},
                    )
                try:
                    validated_items = validate_workspace_items(
                        {**current.workspace_items, **(workspace_items or {})},
                        group_ids=group_ids,
                        visits=validated_visits,
                    )
                except ValueError as exc:
                    raise ResearchHistoryError(str(exc)) from exc
                next_snapshot = ResearchHistorySnapshot(
                    revision=current.revision + 1,
                    visits=validated_visits,
                    groups=validated_groups,
                    workspace_items=validated_items,
                )
                if len(json.dumps(next_snapshot.to_dict(), ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
                    raise ResearchHistoryError("workspace exceeds the 4 MiB storage limit")
                payload.setdefault("users", {})[owner] = next_snapshot.to_dict()
                payload["schemaVersion"] = RESEARCH_HISTORY_SCHEMA_VERSION
                write_json_object_unlocked(path, payload)
                return next_snapshot
        except (ResearchHistoryConflictError, ResearchHistoryError):
            raise
        except (OSError, JsonFileInvalidError, TypeError, ValueError) as exc:
            raise ResearchHistoryStorageError("research history storage is unavailable") from exc


def _read_payload(path: Path) -> dict[str, Any]:
    payload = read_json_object_unlocked(path, default={"schemaVersion": RESEARCH_HISTORY_SCHEMA_VERSION, "users": {}}, strict=True)
    schema_version = payload.get("schemaVersion", RESEARCH_HISTORY_SCHEMA_VERSION)
    if schema_version != RESEARCH_HISTORY_SCHEMA_VERSION:
        raise JsonFileInvalidError("unsupported research history schema version")
    users = payload.get("users", {})
    if not isinstance(users, Mapping):
        raise JsonFileInvalidError("research history users must be an object")
    return {**payload, "users": dict(users)}


def _snapshot_for_owner(payload: Mapping[str, Any], owner: str) -> ResearchHistorySnapshot:
    raw = payload.get("users", {}).get(owner, {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise JsonFileInvalidError("research history owner record must be an object")
    revision = _validate_revision(raw.get("revision", 0))
    visits = _validate_visits(raw.get("visits", []))
    groups = _validate_groups(raw.get("groups", []))
    group_ids = {item["id"] for item in groups}
    if any(item["groupId"] is not None and item["groupId"] not in group_ids for item in visits):
        raise JsonFileInvalidError("research history visit groupId is not defined")
    items = validate_workspace_items(
        {key: value for key, value in raw.items() if key not in {"revision", "visits", "groups"}},
        group_ids=group_ids,
        visits=visits,
    )
    return ResearchHistorySnapshot(revision=revision, visits=visits, groups=groups, workspace_items=items)


def _require_user_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise ResearchHistoryError("authenticated user id is required")
    return value.strip()


def _validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_SAFE_INTEGER:
        raise ResearchHistoryError("revision must be a non-negative integer")
    return value


def _validate_visits(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ResearchHistoryError("visits must be an array")
    if len(values) > MAX_VISITS:
        raise ResearchHistoryError(f"visits cannot contain more than {MAX_VISITS} records")
    result = [_validate_visit(item, index=index) for index, item in enumerate(values)]
    ids = [item["id"] for item in result]
    if len(ids) != len(set(ids)):
        raise ResearchHistoryError("visit ids must be unique")
    return result


def _validate_visit(value: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchHistoryError(f"visit {index} must be an object")
    allowed = {"id", "module", "question", "href", "scrollY", "createdAt", "updatedAt", "title", "groupId", "isFavorite", "deletedAt", "archivedAt", "activity"}
    unknown = set(value) - allowed
    if unknown:
        raise ResearchHistoryError(f"visit {index} contains unsupported fields")
    required = (
        "id", "module", "question", "href", "scrollY", "createdAt", "updatedAt",
        "title", "groupId", "isFavorite", "deletedAt",
    )
    for field in required:
        if field not in value:
            raise ResearchHistoryError(f"visit {index}.{field} is required")
    visit_id = _history_id(value["id"], f"visit {index}.id")
    module = _text(value["module"], f"visit {index}.module", max_length=32)
    if module not in _MODULES:
        raise ResearchHistoryError(f"visit {index}.module is invalid")
    question = _question(value["question"], f"visit {index}.question")
    href = _validate_href(
        value["href"], module=module, question=question, visit_id=visit_id, field=f"visit {index}.href",
    )
    scroll_y = _non_negative_number(value["scrollY"], f"visit {index}.scrollY", maximum=1_000_000)
    created_at = _positive_safe_integer(value["createdAt"], f"visit {index}.createdAt")
    updated_at = _positive_safe_integer(value["updatedAt"], f"visit {index}.updatedAt")
    if updated_at < created_at:
        raise ResearchHistoryError(f"visit {index}.updatedAt cannot precede createdAt")
    title = _text(value["title"], f"visit {index}.title", max_length=120)
    group_id = value["groupId"]
    if group_id is not None:
        group_id = _history_id(group_id, f"visit {index}.groupId")
    favorite = value["isFavorite"]
    if not isinstance(favorite, bool):
        raise ResearchHistoryError(f"visit {index}.isFavorite must be boolean")
    deleted_at = value["deletedAt"]
    if deleted_at is not None:
        deleted_at = _positive_safe_integer(deleted_at, f"visit {index}.deletedAt")
    extras: dict[str, Any] = {}
    if "archivedAt" in value:
        archived = value["archivedAt"]
        extras["archivedAt"] = None if archived is None else _positive_safe_integer(archived, f"visit {index}.archivedAt")
    if "activity" in value:
        activity = value["activity"]
        try:
            parsed = activity_adapter.validate_python(activity) if activity is not None else None
        except ValueError as exc:
            raise ResearchHistoryError(f"visit {index}.activity is invalid") from exc
        if parsed is not None and module != ("papers" if parsed.kind == "reader" else "reports"):
            raise ResearchHistoryError(f"visit {index}.activity does not match module")
        extras["activity"] = parsed.model_dump(exclude_unset=True) if parsed else None
    return {
        "id": visit_id,
        "module": module,
        "question": question,
        "href": href,
        "scrollY": scroll_y,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "title": title,
        "groupId": group_id,
        "isFavorite": favorite,
        "deletedAt": deleted_at,
        **extras,
    }


def _validate_groups(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ResearchHistoryError("groups must be an array")
    if len(values) > MAX_GROUPS:
        raise ResearchHistoryError(f"groups cannot contain more than {MAX_GROUPS} records")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ResearchHistoryError(f"group {index} must be an object")
        allowed = {"id", "name", "createdAt", "updatedAt"}
        if set(value) - allowed:
            raise ResearchHistoryError(f"group {index} contains unsupported fields")
        if not all(field in value for field in allowed):
            raise ResearchHistoryError(f"group {index} is missing required fields")
        group_id = _history_id(value["id"], f"group {index}.id")
        if group_id in ids:
            raise ResearchHistoryError("group ids must be unique")
        ids.add(group_id)
        name = _text(value["name"], f"group {index}.name", max_length=60)
        created_at = _positive_safe_integer(value["createdAt"], f"group {index}.createdAt")
        updated_at = _positive_safe_integer(value["updatedAt"], f"group {index}.updatedAt")
        if updated_at < created_at:
            raise ResearchHistoryError(f"group {index}.updatedAt cannot precede createdAt")
        result.append({"id": group_id, "name": name, "createdAt": created_at, "updatedAt": updated_at})
    return result


def _validate_href(value: Any, *, module: str, question: str, visit_id: str, field: str) -> str:
    href = _text(value, field, max_length=10000)
    if "\\" in href or any(char in href for char in "\r\n"):
        raise ResearchHistoryError(f"{field} is unsafe")
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path or parsed.path not in _MODULE_PATHS[module]:
        raise ResearchHistoryError(f"{field} must be a same-origin research module path")
    params = parse_qs(parsed.query, keep_blank_values=True)
    questions = params.get("question", [])
    if len(questions) != 1 or questions[0] != question:
        raise ResearchHistoryError(f"{field} question does not match visit.question")
    sessions = params.get("researchSession", [])
    if len(sessions) > 1 or (sessions and sessions[0] != visit_id):
        raise ResearchHistoryError(f"{field} researchSession does not match visit.id")
    return href


def _text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ResearchHistoryError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(char) < 0x20 for char in normalized):
        raise ResearchHistoryError(f"{field} is invalid")
    return normalized


def _question(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000 or any(ord(char) < 0x20 for char in value):
        raise ResearchHistoryError(f"{field} is invalid")
    return value


def _history_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ResearchHistoryError(f"{field} is invalid")
    return value


def _positive_safe_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > _MAX_SAFE_INTEGER:
        raise ResearchHistoryError(f"{field} must be a positive safe integer")
    return value


def _non_negative_number(value: Any, field: str, *, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > maximum:
        raise ResearchHistoryError(f"{field} must be a non-negative number")
    return value


__all__ = [
    "DEFAULT_RESEARCH_HISTORY_PATH",
    "MAX_BODY_BYTES",
    "MAX_GROUPS",
    "MAX_VISITS",
    "ResearchHistoryConflictError",
    "ResearchHistoryError",
    "ResearchHistoryService",
    "ResearchHistorySnapshot",
    "ResearchHistoryStorageError",
]
