"""Tell a write that never committed from one whose outcome is unknown.

A repository write runs its statements in one transaction and then commits.
If a statement fails, Postgres has rejected it (a constraint, a type, a
missing table, a cancelled query) and aborted the transaction: nothing was
written, and a corrected retry cannot duplicate anything. Only a failure of
the commit itself, or a connection lost while committing, leaves the outcome
unknown. ``uncommitted_write`` marks the first case so callers (the
``postgres.*`` and ``report.publish`` tools) can report it as a plain failure
instead of a possibly-applied effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from infrastructure.storage.postgres.dsn import redact_dsn


class PostgresWriteRejectedError(psycopg.Error):
    """A write failed before its commit, so its transaction was rolled back.

    Subclasses ``psycopg.Error`` so code that handles database errors
    generically still sees one. ``sqlstate`` carries the server's code when
    there was one.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.rejected_sqlstate = sqlstate


@contextmanager
def uncommitted_write() -> Iterator[None]:
    """Wrap the statements of a write, not its ``commit()``."""

    try:
        yield
    except PostgresWriteRejectedError:
        raise
    except psycopg.Error as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        detail = " ".join(redact_dsn(str(exc)).split())
        code = f" [{sqlstate}]" if sqlstate else ""
        # ``from None``: the original text may quote parameters or the DSN.
        raise PostgresWriteRejectedError(
            f"Postgres rejected the write; nothing was committed{code}: {type(exc).__name__}: {detail}",
            sqlstate=sqlstate,
        ) from None


__all__ = ["PostgresWriteRejectedError", "uncommitted_write"]
