from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any


PARSE_CACHE_IDENTITY_SCHEMA = "research-parse-cache-identity.v2"
PARSE_NORMALIZER_VERSION = "research-document-normalizer.v2"
OPAQUE_IDENTITY_VALUES = frozenset({"unknown", "unspecified", "not_declared"})


def parse_cache_identity_fingerprint(identity: Mapping[str, Any] | None) -> str | None:
    canonical = validate_parse_cache_identity(identity)
    if canonical is None:
        return None
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def validate_parse_cache_identity(
    identity: Mapping[str, Any] | None,
    *,
    source_hash: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any] | None:
    """Return a canonical complete cache identity, otherwise fail closed."""

    if not isinstance(identity, Mapping):
        return None
    try:
        encoded = json.dumps(
            dict(identity),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        canonical = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(canonical, dict) or canonical.get("schema") != PARSE_CACHE_IDENTITY_SCHEMA:
        return None

    source = canonical.get("source")
    parser = canonical.get("parser")
    compiler = canonical.get("compiler")
    chunker = canonical.get("chunker")
    schemas = canonical.get("schemas")
    profile = canonical.get("profile")
    normalizer = canonical.get("normalizer")
    if not all(isinstance(item, dict) for item in (source, parser, compiler, chunker, schemas, profile, normalizer)):
        return None
    if not _required_text(source, "hash") or not _required_text(source, "content_type"):
        return None
    if source_hash is not None and source["hash"] != str(source_hash).strip():
        return None
    if not _component_identity_complete(parser, allow_none=True):
        return None
    if parser.get("type") is None and compiler.get("type") is None:
        return None
    if not _component_identity_complete(compiler, allow_none=True):
        return None
    if not _component_identity_complete(chunker, allow_none=True):
        return None
    if not _required_text(schemas, "document") or not _required_text(schemas, "document_sha256"):
        return None
    if not _required_text(schemas, "fidelity") or not _required_text(schemas, "fidelity_sha256"):
        return None
    if not _required_text(profile, "quality_profile"):
        return None
    if normalizer.get("version") != PARSE_NORMALIZER_VERSION:
        return None
    if not _required_text(normalizer, "module_sha256"):
        return None
    if _contains_opaque_identity(canonical):
        return None
    if fingerprint is not None and parse_cache_identity_fingerprint(canonical) != str(fingerprint).strip():
        return None
    return canonical


def _component_identity_complete(value: dict[str, Any], *, allow_none: bool = False) -> bool:
    component_type = value.get("type")
    if component_type is None:
        return allow_none and set(value) == {"type"}
    return isinstance(component_type, str) and bool(component_type.strip())


def _required_text(value: Mapping[str, Any], key: str) -> bool:
    raw = value.get(key)
    return isinstance(raw, str) and bool(raw.strip())


def _contains_opaque_identity(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_opaque_identity(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_opaque_identity(item) for item in value)
    return isinstance(value, str) and value.strip().casefold() in OPAQUE_IDENTITY_VALUES


__all__ = [
    "PARSE_CACHE_IDENTITY_SCHEMA",
    "PARSE_NORMALIZER_VERSION",
    "parse_cache_identity_fingerprint",
    "validate_parse_cache_identity",
]
