from __future__ import annotations

import re


def normalize_dsn(dsn: str) -> str:
    """Normalize a Postgres DSN to the libpq/psycopg URI form.

    Strips a stray ``jdbc:`` prefix (a common copy-paste from JDBC configs that
    psycopg rejects with 'missing "=" after ...'). Centralizing this here means
    every psycopg.connect call site is protected by one function instead of each
    module defending itself.
    """
    if not dsn:
        return dsn
    return dsn.strip().removeprefix("jdbc:")


_URI_PASSWORD = re.compile(r"(?P<scheme>postgres(?:ql)?://)(?P<user>[^:@/\s]*):[^@/\s]*@", re.IGNORECASE)
_KEYWORD_PASSWORD = re.compile(r"(?P<key>\bpassword\s*=\s*)(?:'(?:[^'\\]|\\.)*'|\S+)", re.IGNORECASE)


def redact_dsn(text: str) -> str:
    """Remove passwords from anything that may quote a DSN (URI or keyword form)."""

    text = _URI_PASSWORD.sub(lambda match: f"{match['scheme']}{match['user']}:***@", text)
    return _KEYWORD_PASSWORD.sub(lambda match: f"{match['key']}***", text)


__all__ = ["normalize_dsn", "redact_dsn"]
