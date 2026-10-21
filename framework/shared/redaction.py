from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

REDACTED_VALUE = "***REDACTED***"
DEFAULT_SENSITIVE_KEY_TOKENS = (
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "dsn",
    "password",
    "private_key",
    "secret",
    "token",
)

_SECRET_PREFIX = "sk" + "-"
_SECRET_PATTERNS = (
    re.compile(rf"(?<![A-Za-z0-9_-]){_SECRET_PREFIX}[A-Za-z0-9_-]{{8,}}(?![A-Za-z0-9_-])"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
)
# Match the former [a-z]/IGNORECASE grammar, including its four Unicode letters.
_URI_SCHEME_LETTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\u0130\u0131\u017f\u212a"
)
_URI_SCHEME_CONTINUATION = _URI_SCHEME_LETTERS | frozenset("0123456789+.-")
_URI_USER_TERMINATORS = frozenset("/:@?#")
_URI_PASSWORD_TERMINATORS = frozenset("/@?#")


@dataclass(frozen=True)
class RedactionRule:
    key_tokens: tuple[str, ...] = DEFAULT_SENSITIVE_KEY_TOKENS
    replacement: str = REDACTED_VALUE

    def matches_key(self, key: str) -> bool:
        normalized = str(key).casefold().replace("-", "_")
        return any(token.casefold() in normalized for token in self.key_tokens)


class Redactor:
    def __init__(self, rules: list[RedactionRule] | None = None) -> None:
        self.rules = list(rules or [RedactionRule()])

    def redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self.redact_mapping(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, set):
            return {self.redact(item) for item in value}
        if isinstance(value, str):
            return self._redact_secret_like_string(value)
        return value

    def redact_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: self._replacement_for_key(str(key)) if self._matches_key(str(key)) else self.redact(item)
            for key, item in value.items()
        }

    def contains_sensitive_key(self, value: Mapping[str, Any]) -> bool:
        return any(self._matches_key(str(key)) for key in value)

    def _matches_key(self, key: str) -> bool:
        return any(rule.matches_key(key) for rule in self.rules)

    def _replacement_for_key(self, key: str) -> str:
        for rule in self.rules:
            if rule.matches_key(key):
                return rule.replacement
        return REDACTED_VALUE

    def _redact_secret_like_string(self, value: str) -> str:
        redacted = value
        replacement = self.rules[0].replacement if self.rules else REDACTED_VALUE
        redacted = _SECRET_PATTERNS[0].sub(replacement, redacted)
        redacted = _SECRET_PATTERNS[1].sub(replacement, redacted)
        return _redact_uri_credentials(redacted, replacement)


def _redact_uri_credentials(value: str, replacement: str) -> str:
    """Redact URI user/password pairs with one forward scan.

    The old regex attempted the unbounded ``scheme`` prefix at every character,
    which made strings without ``://`` quadratic.  This scanner locates each
    delimiter once, validates the maximal scheme run, and then consumes the
    credential fields only until their URI delimiters.  Every later ``://``
    terminates both a scheme run and any preceding credential scan, so failed
    candidates cannot repeatedly rescan a shared suffix.  It intentionally keeps
    the original URI grammar, including schemes embedded in a larger token.
    """

    pieces: list[str] = []
    output_start = 0
    search_start = 0
    length = len(value)
    while True:
        delimiter = value.find("://", search_start)
        if delimiter < 0:
            break
        if not _has_uri_scheme(value, delimiter):
            search_start = delimiter + 3
            continue
        user_start = delimiter + 3
        user_end = _first_uri_delimiter(value, user_start, _URI_USER_TERMINATORS)
        if user_end >= length or value[user_end] != ":" or user_end == user_start:
            search_start = delimiter + 3
            continue
        password_start = user_end + 1
        password_end = _first_uri_delimiter(value, password_start, _URI_PASSWORD_TERMINATORS)
        if password_end >= length or value[password_end] != "@" or password_end == password_start:
            search_start = delimiter + 3
            continue
        pieces.append(value[output_start:user_start])
        pieces.append(replacement)
        pieces.append("@")
        output_start = password_end + 1
        search_start = output_start
    if not pieces:
        return value
    pieces.append(value[output_start:])
    return "".join(pieces)


def _has_uri_scheme(value: str, delimiter: int) -> bool:
    index = delimiter - 1
    has_letter = False
    while index >= 0 and value[index] in _URI_SCHEME_CONTINUATION:
        has_letter = has_letter or value[index] in _URI_SCHEME_LETTERS
        index -= 1
    return has_letter


def _first_uri_delimiter(value: str, start: int, terminators: frozenset[str]) -> int:
    index = start
    length = len(value)
    while index < length and value[index] not in terminators and not value[index].isspace():
        index += 1
    return index


def redact_sensitive_values(value: Any) -> Any:
    return Redactor().redact(value)


def contains_redacted_value(value: Any) -> bool:
    if value == REDACTED_VALUE:
        return True
    if isinstance(value, Mapping):
        return any(contains_redacted_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_redacted_value(item) for item in value)
    return False
