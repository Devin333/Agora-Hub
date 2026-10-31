from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from framework.shared.time import format_datetime


def stable_json_dumps(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def ensure_strict_json(value: Any, field_name: str) -> None:
    """Refuse NaN, Infinity and non-JSON values.

    Python's encoder writes NaN as a bare token that strict readers (Postgres
    JSONB, browsers, other languages' parsers) reject, so anything that is
    persisted or handed to another process is checked before it leaves.
    """

    try:
        json.dumps(value, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"{field_name} must not contain NaN or Infinity") from exc
    except TypeError as exc:
        raise ValueError(f"{field_name} must contain only JSON values") from exc


def json_loads(text: str) -> Any:
    return json.loads(text)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, Path):
        return value.as_posix()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump(mode="json"))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_jsonable(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {_json_key(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [
            to_jsonable(item)
            for item in sorted(value, key=lambda item: stable_json_dumps(to_jsonable(item)))
        ]
    return value


def _json_key(key: Any) -> str:
    if isinstance(key, Enum):
        return str(key.value)
    if isinstance(key, datetime):
        return format_datetime(key) or ""
    if isinstance(key, Path):
        return key.as_posix()
    return str(key)
