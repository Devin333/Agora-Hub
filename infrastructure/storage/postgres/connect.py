"""Open Postgres connections without leaking credentials into errors.

Kept apart from ``dsn``: modules that only normalize a DSN must stay
importable when the optional psycopg dependency is not installed.
"""

from __future__ import annotations

from typing import Any

import psycopg

from infrastructure.storage.postgres.dsn import normalize_dsn, redact_dsn


class PostgresConnectionError(psycopg.OperationalError):
    """A connection could not be opened. The message never contains the password."""


def connect_postgres(dsn: str, **options: Any) -> psycopg.Connection:
    """``psycopg.connect`` whose failures cannot leak credentials.

    psycopg quotes the whole connection string in parse errors, password
    included, and those messages reach logs and tool results.
    """

    try:
        return psycopg.connect(normalize_dsn(dsn), **options)
    except psycopg.Error as exc:
        # ``from None``: the original exception carries the unredacted text.
        raise PostgresConnectionError(f"could not connect to Postgres: {redact_dsn(str(exc))}") from None


__all__ = ["PostgresConnectionError", "connect_postgres"]
