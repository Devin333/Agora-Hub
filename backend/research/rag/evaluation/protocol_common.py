"""Shared, versioned primitives for the Research RAG evaluation protocol."""

from __future__ import annotations

import hashlib
import json
from enum import Enum, StrEnum
from typing import Any, Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = "research-rag-eval/v1"


class EvaluationTrack(StrEnum):
    RETRIEVAL = "retrieval"
    ORACLE_GENERATION = "oracle_generation"
    END_TO_END = "end_to_end"


class ExecutionProfile(StrEnum):
    FIXTURE = "fixture"
    PRODUCTION = "production"


NonEmptyStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


def _reject_floating_version(value: str) -> str:
    if value.casefold() in {"latest", "current", "head"}:
        raise ValueError("version identity must be immutable and must not use latest/current/head aliases")
    return value


ImmutableVersionStr = Annotated[NonEmptyStr, AfterValidator(_reject_floating_version)]


class ProtocolModel(BaseModel):
    """Strict immutable base for every protocol value object and wire record."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value deterministically as UTF-8.

    NaN and infinities are deliberately rejected because they do not have a
    portable JSON representation and therefore cannot participate in identity.
    """

    return json.dumps(
        value,
        default=_json_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    """Return the lowercase SHA-256 digest of canonical JSON content."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "EvaluationTrack",
    "ExecutionProfile",
    "ProtocolModel",
    "NonEmptyStr",
    "Sha256Digest",
    "ImmutableVersionStr",
    "canonical_json_bytes",
    "content_digest",
]
