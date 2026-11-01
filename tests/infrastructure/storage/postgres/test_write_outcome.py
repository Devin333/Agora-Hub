"""A repository write says whether it may have committed.

A statement failure aborts the transaction, so nothing was written; only the
commit itself can leave the outcome unknown. The tools rely on the difference
(``no_effect_error_types``) to report a definite failure instead of an
indeterminate effect.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from backend.layers.output.postgres_tools import register_postgres_tools
from framework.tool import ToolRegistry
from infrastructure.storage.persistence import GraphRunRecord, ReportRecord, RunPersistenceBatch
from infrastructure.storage.postgres import PostgresRepository
from infrastructure.storage.postgres.transaction import PostgresWriteRejectedError, uncommitted_write
from tests.tools.harness.invoke import run_tool
from tests.tools.test_notification_webhook_tools import outcome


class _Rejection(errors.ForeignKeyViolation):
    sqlstate = "23503"


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.statements.append(sql)
        if self.connection.statement_error is not None:
            raise self.connection.statement_error


class _Connection:
    def __init__(self, *, statement_error: BaseException | None = None, commit_error: BaseException | None = None) -> None:
        self.statement_error = statement_error
        self.commit_error = commit_error
        self.statements: list[str] = []
        self.commits = 0

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1


def _repository(connection: _Connection) -> PostgresRepository:
    return PostgresRepository("postgresql://example", connection_factory=lambda: connection)


def _report(run_id: str = "run-1") -> ReportRecord:
    return ReportRecord(report_id="r1", run_id=run_id, status="completed", title="T", report_json={})


def _run(run_id: str = "run-1") -> GraphRunRecord:
    return GraphRunRecord(run_id=run_id, graph_id="g", graph_version="1", status="running", profile="p")


@pytest.mark.parametrize(
    "write",
    [
        lambda repository: repository.save_report(_report()),
        lambda repository: repository.save_run_records(RunPersistenceBatch(graph_run=_run(), report=_report())),
        lambda repository: repository.execute("UPDATE reports SET status = %s", ("x",)),
    ],
    ids=["save_report", "save_run_records", "execute"],
)
def test_a_rejected_statement_is_reported_as_never_committed(write) -> None:
    connection = _Connection(statement_error=_Rejection('insert violates foreign key constraint "reports_run_id_fkey"'))

    with pytest.raises(PostgresWriteRejectedError) as rejected:
        write(_repository(connection))

    assert connection.commits == 0
    assert rejected.value.rejected_sqlstate == "23503"
    assert "nothing was committed [23503]" in str(rejected.value)
    assert "reports_run_id_fkey" in str(rejected.value)
    assert isinstance(rejected.value, psycopg.Error)


def test_a_failed_commit_is_not_claimed_to_have_written_nothing() -> None:
    lost = psycopg.OperationalError("server closed the connection unexpectedly")
    connection = _Connection(commit_error=lost)

    with pytest.raises(psycopg.OperationalError) as failure:
        _repository(connection).save_report(_report())

    assert failure.value is lost
    assert not isinstance(failure.value, PostgresWriteRejectedError)


def test_a_rejection_never_quotes_a_password() -> None:
    with pytest.raises(PostgresWriteRejectedError) as rejected:
        with uncommitted_write():
            raise psycopg.ProgrammingError("bad target postgresql://bot:s3cret@db.internal/newsroom")

    assert "s3cret" not in str(rejected.value)
    assert "postgresql://bot:***@db.internal/newsroom" in str(rejected.value)


def test_errors_that_are_not_database_errors_pass_through_unchanged() -> None:
    with pytest.raises(TypeError):
        with uncommitted_write():
            raise TypeError("not adaptable")


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (_Connection(statement_error=_Rejection("violates foreign key constraint")), "not_applied"),
        (_Connection(commit_error=psycopg.OperationalError("connection lost during commit")), "indeterminate"),
    ],
    ids=["rejected-statement", "lost-commit"],
)
def test_save_report_tool_reports_what_is_known_about_the_write(connection: _Connection, expected: str) -> None:
    registry = ToolRegistry()
    register_postgres_tools(registry, repository=_repository(connection))

    run = run_tool(
        registry,
        "postgres.save_report",
        {"report_id": "r1", "run_id": "run-1", "status": "completed", "report_json": {}},
    )

    assert outcome(run) == expected, run.describe()
