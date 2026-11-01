from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from framework.agent.artifacts.paths import (
    resolve_artifact_descendant,
    validate_artifact_path_segment,
    validate_relative_artifact_path,
)
from framework.tool.models.definition import ToolDefinition
from framework.tool.registry.registry import ToolRegistry
from framework.tool.runtime.errors import effect_not_applied_on
from framework.shared.graph_identity import GraphExecutionIdentity

# Loads are read whole into memory and returned to the model, so a file
# bigger than the tool's result limit is refused before it is read.
MAX_LOAD_BYTES = 1_000_000
SEARCH_PREVIEW_BYTES = 200_000


def register_artifact_tools(
    registry: ToolRegistry,
    *,
    artifact_manager: Any,
    execution_identity: GraphExecutionIdentity,
) -> None:
    if not isinstance(execution_identity, GraphExecutionIdentity):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    run_id = execution_identity.run_id
    registry.register(
        ToolDefinition(
            name="artifact.write",
            description="Write a JSON or text artifact under the current run directory.",
            input_schema={
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                    "content_type": {"type": "string", "enum": ["application/json", "text/plain"]},
                },
                "additionalProperties": False,
            },
            side_effect="writes_local_state",
            metadata={"no_effect_error_types": ["ArtifactPathError"]},
        ),
        lambda args: _write_artifact(artifact_manager, run_id, args),
        graph_identity=execution_identity,
    )
    registry.register(
        ToolDefinition(
            name="artifact.load",
            description="Load a JSON or text artifact from the current run directory.",
            input_schema={
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "content_type": {"type": "string", "enum": ["application/json", "text/plain"]},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _load_artifact(artifact_manager, run_id, args),
        graph_identity=execution_identity,
    )
    registry.register(
        ToolDefinition(
            name="artifact.search",
            description="Search artifact paths and text previews in the current run directory.",
            input_schema={
                "required": [],
                "properties": {
                    "query": {"type": "string"},
                    "path_prefix": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _search_artifacts(artifact_manager, run_id, args),
        graph_identity=execution_identity,
    )


def _write_artifact(artifact_manager: Any, run_id: str, args: dict[str, Any]) -> dict[str, Any]:
    # Path errors keep their ArtifactPathError type (callers and MCP envelopes
    # key on it); the definition lists it in no_effect_error_types.
    validate_artifact_path_segment(run_id, field="run_id")
    relative_path = validate_relative_artifact_path(
        str(args["path"]),
        field="artifact path",
    )
    with effect_not_applied_on():
        content = args["content"]
        content_type = str(args.get("content_type") or _content_type_for(content))
        if content_type == "application/json":
            try:
                json.dumps(content, allow_nan=False)
            except ValueError as exc:
                raise ValueError("JSON artifact content must not contain NaN or Infinity") from exc
    if content_type == "text/plain":
        target = artifact_manager.write_text(run_id, relative_path, str(content))
    else:
        target = artifact_manager.write_json(run_id, relative_path, content)
    return _artifact_payload(relative_path, content_type, Path(target))


def _load_artifact(artifact_manager: Any, run_id: str, args: dict[str, Any]) -> dict[str, Any]:
    relative_path = validate_relative_artifact_path(
        str(args["path"]),
        field="artifact path",
    )
    target = _artifact_path(artifact_manager, run_id, relative_path)
    content_type = str(args.get("content_type") or _content_type_for_path(target))
    text = _read_artifact_text(target, relative_path)
    try:
        content = json.loads(text) if content_type == "application/json" else text
    except json.JSONDecodeError as exc:
        raise ValueError(f"artifact {relative_path} is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    payload = _artifact_payload(relative_path, content_type, target)
    payload["content"] = content
    return payload


def _search_artifacts(artifact_manager: Any, run_id: str, args: dict[str, Any]) -> dict[str, Any]:
    validate_artifact_path_segment(run_id, field="run_id")
    run_dir = Path(artifact_manager.run_dir(run_id)).resolve(strict=False)
    raw_prefix = str(args.get("path_prefix") or "")
    prefix = (
        validate_relative_artifact_path(raw_prefix, field="artifact path prefix")
        if raw_prefix
        else None
    )
    root = (
        resolve_artifact_descendant(run_dir, prefix, field="artifact path prefix")
        if prefix is not None
        else run_dir
    )
    query = str(args.get("query") or "").casefold()
    max_results = max(1, min(int(args.get("max_results") or 20), 100))
    matches = []
    if not root.exists():
        return {"match_count": 0, "artifacts": []}
    paths = [root] if root.is_file() else sorted(
        path
        for path in root.rglob("*")
        # Dot entries are runtime bookkeeping (write locks, staging files), not artifacts.
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(run_dir).parts)
    )
    for candidate in paths:
        relative_path = candidate.relative_to(run_dir).as_posix()
        path = resolve_artifact_descendant(
            run_dir,
            relative_path,
            field="artifact search result path",
        )
        matched_on = _artifact_match_reason(path, relative_path, query)
        if matched_on is None:
            continue
        matches.append(
            {
                **_artifact_payload(relative_path, _content_type_for_path(path), path),
                "matched_on": matched_on,
            }
        )
        if len(matches) >= max_results:
            break
    return {"match_count": len(matches), "artifacts": matches}


def _artifact_path(artifact_manager: Any, run_id: str, relative_path: str) -> Path:
    validate_artifact_path_segment(run_id, field="run_id")
    relative = validate_relative_artifact_path(relative_path, field="artifact path")
    return resolve_artifact_descendant(
        artifact_manager.run_dir(run_id),
        relative,
        field="artifact path",
    )


def _artifact_match_reason(path: Path, relative_path: str, query: str) -> str | None:
    if not query:
        return "all"
    if query in relative_path.casefold():
        return "path"
    if not _is_text_artifact(path):
        return None
    with path.open("rb") as handle:
        preview = handle.read(SEARCH_PREVIEW_BYTES)
    text = preview.decode("utf-8", errors="replace")
    return "content" if query in text.casefold() else None


def _read_artifact_text(target: Path, relative_path: str) -> str:
    """Read a loadable artifact, naming it by its run-relative path in errors."""

    if not target.is_file():
        raise FileNotFoundError(f"artifact not found: {relative_path}")
    size = target.stat().st_size
    if size > MAX_LOAD_BYTES:
        raise ValueError(f"artifact {relative_path} is {size} bytes; artifact.load reads at most {MAX_LOAD_BYTES}")
    try:
        return target.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"artifact {relative_path} is not UTF-8 text") from exc


def _is_text_artifact(path: Path) -> bool:
    return path.suffix.lower() in {"", ".json", ".jsonl", ".md", ".txt", ".csv", ".xml"}


def _artifact_payload(relative_path: str, content_type: str, target: Path) -> dict[str, Any]:
    return {
        "artifact_id": f"artifact:{relative_path}",
        "relative_path": relative_path,
        "path": relative_path,
        "content_type": content_type,
        "size_bytes": target.stat().st_size,
    }


def _content_type_for(content: Any) -> str:
    return "text/plain" if isinstance(content, str) else "application/json"


def _content_type_for_path(path: Path) -> str:
    return "application/json" if path.suffix.lower() == ".json" else "text/plain"
