from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from framework.events.canonical import canonical_json_bytes
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.canonical import (
    canonical_checksum,
    freeze_json,
    thaw_json,
)


_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*\Z")
_REFERENCE_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+\Z|"
    r"[A-Za-z0-9][A-Za-z0-9._:/+-]*\Z"
)
_EXACT_REFERENCE_PATTERN = re.compile(
    r"(?P<identifier>[A-Za-z0-9][A-Za-z0-9._:/+-]*)@"
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)\Z"
)
_MOVING_VERSION_ALIASES = frozenset({"current", "default", "latest", "stable"})

T = TypeVar("T")


def required_text(value: Any, field_name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise _contract_error(
            "task_plan_required_field",
            f"{field_name} must be a non-blank trimmed string",
            field=field_name,
        )
    if len(value) > max_length:
        raise _contract_error(
            "task_plan_field_too_long",
            f"{field_name} exceeds its maximum length",
            field=field_name,
            max_length=max_length,
        )
    return value


def optional_text(
    value: Any,
    field_name: str,
    *,
    max_length: int = 512,
) -> str | None:
    if value is None:
        return None
    return required_text(value, field_name, max_length=max_length)


def identifier(value: Any, field_name: str) -> str:
    text = required_text(value, field_name)
    if _IDENTIFIER_PATTERN.fullmatch(text) is None:
        raise _contract_error(
            "invalid_task_plan_identifier",
            f"{field_name} must be a stable identifier",
            field=field_name,
        )
    return text


def reference(value: Any, field_name: str) -> str:
    text = required_text(value, field_name, max_length=2048)
    if _REFERENCE_PATTERN.fullmatch(text) is None:
        raise _contract_error(
            "invalid_task_plan_reference",
            f"{field_name} must be a stable reference",
            field=field_name,
        )
    return text


def exact_reference(value: Any, field_name: str) -> str:
    text = required_text(value, field_name)
    match = _EXACT_REFERENCE_PATTERN.fullmatch(text)
    if match is None or match.group("version").casefold() in _MOVING_VERSION_ALIASES:
        raise _contract_error(
            "task_plan_inexact_reference",
            f"{field_name} must use exact '<id>@<version>' form",
            field=field_name,
            reference=text,
        )
    return text


def task_reference_producer(
    value: str,
    known_task_ids: Sequence[str] = (),
) -> str | None:
    """Return the producer identity for canonical or known plain task refs."""

    task_ref = reference(value, "input_ref")
    remainder: str | None = None
    if task_ref.startswith("task://"):
        remainder = task_ref.removeprefix("task://")
    elif task_ref.startswith("task:"):
        remainder = task_ref.removeprefix("task:")
    if remainder is not None:
        producer = remainder.split("/", maxsplit=1)[0].split("#", maxsplit=1)[0]
        if not producer:
            raise _contract_error(
                "invalid_task_plan_reference",
                "task input reference must identify a producer task",
                field="input_ref",
            )
        return identifier(producer, "producer_task_id")
    return task_ref if task_ref in frozenset(known_task_ids) else None


def task_output_reference_producer(value: str, known_task_ids: Sequence[str] = ()) -> str | None:
    """Resolve the public candidate output selector, never private evidence."""
    producer = task_reference_producer(value, known_task_ids)
    if producer is None:
        return None
    allowed = {producer}
    for prefix in ("task:", "task://"):
        allowed.update(prefix + producer + suffix for suffix in ("", "/output"))
    allowed.add("task://" + producer + "#output")
    if value not in allowed:
        raise _contract_error(
            "REF_DEPENDENCY_SELECTOR_INVALID",
            "task dependencies can reference only the accepted public output",
            field="input_ref",
        )
    return producer


def checksum(value: Any, field_name: str) -> str:
    text = required_text(value, field_name)
    if _CHECKSUM_PATTERN.fullmatch(text) is None:
        raise _contract_error(
            "invalid_task_plan_checksum",
            f"{field_name} must use sha256:<64 lowercase hex> form",
            field=field_name,
        )
    return text


def non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _contract_error(
            "invalid_task_plan_limit",
            f"{field_name} must be a non-negative integer",
            field=field_name,
        )
    return value


def positive_int(value: Any, field_name: str) -> int:
    normalized = non_negative_int(value, field_name)
    if normalized == 0:
        raise _contract_error(
            "invalid_task_plan_limit",
            f"{field_name} must be greater than zero",
            field=field_name,
        )
    return normalized


def stable_text_tuple(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = True,
    item_kind: str = "identifier",
) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise _contract_error(
            "invalid_task_plan_collection",
            f"{field_name} must be an array",
            field=field_name,
        )
    normalizer = {
        "identifier": identifier,
        "reference": reference,
        "exact_reference": exact_reference,
        "text": required_text,
    }.get(item_kind)
    if normalizer is None:
        raise AssertionError(f"unsupported stable tuple item kind: {item_kind}")
    normalized = tuple(normalizer(item, field_name) for item in values)
    if not allow_empty and not normalized:
        raise _contract_error(
            "invalid_task_plan_collection",
            f"{field_name} must not be empty",
            field=field_name,
        )
    if len(set(normalized)) != len(normalized):
        raise _contract_error(
            "duplicate_task_plan_collection_value",
            f"{field_name} must contain unique values",
            field=field_name,
        )
    return tuple(sorted(normalized))


def frozen_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error(
            "invalid_task_plan_mapping",
            f"{field_name} must be an object",
            field=field_name,
        )
    frozen = freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise AssertionError("canonical mapping did not remain a mapping")
    return frozen


def canonical_json(value: Any) -> str:
    frozen = freeze_json(value, "task_plan")
    return canonical_json_bytes(frozen).decode("utf-8")


def canonical_payload_checksum(value: Any) -> str:
    return canonical_checksum(freeze_json(value, "task_plan_checksum"))


def thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    thawed = thaw_json(value)
    if not isinstance(thawed, dict):
        raise AssertionError("canonical mapping did not thaw to dict")
    return thawed


def exact_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    model: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise _contract_error(
            "invalid_task_plan_payload",
            f"{model} payload must be an object",
            model=model,
        )
    keys = frozenset(str(key) for key in payload)
    unknown = tuple(sorted(keys - required - optional))
    missing = tuple(sorted(required - keys))
    if unknown or missing:
        raise _contract_error(
            "invalid_task_plan_payload_fields",
            f"{model} payload fields do not match the contract",
            model=model,
            missing=list(missing),
            unknown=list(unknown),
        )
    return dict(payload)


def _contract_error(code: str, message: str, **details: Any) -> HarnessValidationError:
    return HarnessValidationError(
        message,
        code=code,
        details={"code": code, **details},
    )


__all__ = [
    "canonical_json",
    "canonical_payload_checksum",
    "checksum",
    "exact_keys",
    "exact_reference",
    "frozen_mapping",
    "identifier",
    "non_negative_int",
    "optional_text",
    "positive_int",
    "reference",
    "required_text",
    "stable_text_tuple",
    "task_reference_producer",
    "task_output_reference_producer",
    "thaw_mapping",
]
