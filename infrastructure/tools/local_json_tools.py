from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from framework.agent.artifacts.paths import ArtifactPathError, validate_artifact_path_segment
from framework.tool.models import ToolDefinition
from framework.tool.registry import ToolRegistry
from framework.tool.runtime.errors import ToolEffectNotAppliedError, effect_not_applied_on
from infrastructure.storage.json_file_store import locked_file, write_bytes_atomically_unlocked

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# Leaves room for "<collection>/<record>.json" inside common path limits.
MAX_NAME_LENGTH = 120


def register_local_json_tools(
    registry: ToolRegistry,
    *,
    root: str | Path,
) -> None:
    store = LocalJsonToolStore(root)
    registry.register(
        ToolDefinition(
            name="local_json.save",
            description="Save a scoped JSON record under the configured local JSON root.",
            input_schema={
                "required": ["collection", "record_id", "value"],
                "properties": {
                    "collection": {"type": "string"},
                    "record_id": {"type": "string"},
                    "value": {
                        "type": ["object", "array", "string", "number", "boolean", "null"]
                    },
                    "metadata": {"type": "object"},
                },
                "additionalProperties": False,
            },
            side_effect="writes_local_state",
            concurrency_safe=False,
            max_result_bytes=100_000,
            metadata={"writes_local_json": True},
        ),
        lambda args: _save_local_json(args, store=store),
    )


class LocalJsonToolStore:
    """One JSON file per record, replaced atomically under a cross-process lock.

    A crash or a full disk mid-write leaves the previous record intact, and
    concurrent writers to one record never interleave; the last complete write
    wins.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, *, collection: str, record_id: str, value: Any, metadata: dict[str, Any]) -> Path:
        with effect_not_applied_on():
            collection_name = _safe_name(collection, "collection")
            record_name = _safe_name(record_id, "record_id")
            payload = {
                "collection": collection_name,
                "record_id": record_name,
                "value": value,
                "metadata": dict(metadata),
            }
            try:
                text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            except ValueError as exc:
                # NaN/Infinity are not JSON; strict readers would reject the file.
                raise ValueError("value must not contain NaN or Infinity") from exc
            except TypeError as exc:
                raise ValueError("value must contain only JSON values") from exc
            content = (text + "\n").encode("utf-8")
        target = self.root / collection_name / f"{record_name}.json"
        try:
            with locked_file(target):
                write_bytes_atomically_unlocked(target, content)
        except OSError as exc:
            raise ToolEffectNotAppliedError(
                f"local JSON record could not be written ({type(exc).__name__}); "
                "any previous version was left unchanged"
            ) from exc
        return target


def _save_local_json(args: dict[str, Any], *, store: LocalJsonToolStore) -> dict[str, Any]:
    with effect_not_applied_on():
        metadata = args.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        collection = str(args["collection"])
        record_id = str(args["record_id"])
    target = store.save(
        collection=collection,
        record_id=record_id,
        value=args["value"],
        metadata=dict(metadata),
    )
    return {
        "saved": True,
        "collection": _safe_name(collection, "collection"),
        "record_id": _safe_name(record_id, "record_id"),
        "relative_path": target.relative_to(store.root).as_posix(),
        "size_bytes": target.stat().st_size,
    }


def _safe_name(value: str, field_name: str) -> str:
    """One portable path segment: no traversal, no Windows device names or
    trailing dots (which Windows silently drops), bounded length."""

    name = value.strip()
    if not name:
        raise ValueError(f"{field_name} is required")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{field_name} must be at most {MAX_NAME_LENGTH} characters")
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"{field_name} must be a safe record name")
    try:
        validate_artifact_path_segment(name, field=field_name)
    except ArtifactPathError as exc:
        raise ValueError(f"{field_name} must be a safe record name: {exc}") from exc
    return name
