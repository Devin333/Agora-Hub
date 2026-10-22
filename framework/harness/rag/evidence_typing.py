from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceTypeResolver(Protocol):
    """Resolves a retrieved item's content-derived evidence type."""

    def resolve(self, metadata: Mapping[str, Any]) -> str | None:
        """Return the primary evidence type or None when metadata has no usable signal."""
        ...

    def resolve_types(self, metadata: Mapping[str, Any]) -> tuple[str, ...]:
        """Return every evidence type the content supports, primary type first."""
        ...


class MetadataKeyEvidenceTypeResolver:
    """Deterministic resolver driven by ordered metadata-key mapping tables."""

    def __init__(self, mapping: Mapping[str, Mapping[str, str]]) -> None:
        self._mapping: dict[str, dict[str, str]] = {
            str(key): {str(source): str(target) for source, target in table.items()}
            for key, table in mapping.items()
        }

    def resolve(self, metadata: Mapping[str, Any]) -> str | None:
        types = self.resolve_types(metadata)
        return types[0] if types else None

    def resolve_types(self, metadata: Mapping[str, Any]) -> tuple[str, ...]:
        # A passage can carry several roles (e.g. experiment and method); keep them all,
        # in mapping-key then value order so the first one remains the primary type.
        types: list[str] = []
        for metadata_key, table in self._mapping.items():
            for value in _metadata_values(metadata.get(metadata_key)):
                mapped = table.get(value)
                if mapped and mapped not in types:
                    types.append(mapped)
        return tuple(types)


def _metadata_values(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        value = raw.strip()
        return (value,) if value else ()
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    value = str(raw).strip()
    return (value,) if value else ()


__all__ = [
    "EvidenceTypeResolver",
    "MetadataKeyEvidenceTypeResolver",
]
