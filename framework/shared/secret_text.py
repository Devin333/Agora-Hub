"""A string credential that never shows up in reprs, logs or serialized payloads."""
from __future__ import annotations

from typing import Any

from framework.shared.redaction import REDACTED_VALUE


class SecretText:
    """Wraps a credential; only :meth:`reveal` returns the plaintext."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if isinstance(value, SecretText):
            value = value.reveal()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("secret value must be a non-empty string")
        self._value = value.strip()

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretText({REDACTED_VALUE!r})"

    def __str__(self) -> str:
        return REDACTED_VALUE

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretText) and other._value == self._value

    def __hash__(self) -> int:
        return hash((SecretText, self._value))

    def __reduce__(self) -> Any:
        raise TypeError("SecretText cannot be pickled")

    def __deepcopy__(self, memo: dict[int, Any]) -> SecretText:
        return self

    def __copy__(self) -> SecretText:
        return self


__all__ = ["SecretText"]
