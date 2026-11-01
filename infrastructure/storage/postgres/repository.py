from __future__ import annotations

import json
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from typing import Any, Callable

import psycopg

from backend.foundation.models.source_error_normalization import (
    normalize_source_errors,
)
from infrastructure.storage.postgres.connect import connect_postgres
from infrastructure.storage.postgres.transaction import uncommitted_write

from infrastructure.external.sources.models import SourceError, SourceHealth, SourceHealthStatus
from infrastructure.storage.local_json import ReportNotFoundError
from infrastructure.storage.postgres.migrations import load_migration_sql
from infrastructure.storage.records import ClaimRecord, EvidenceItemRecord, QualityResultRecord, ReportDetailRecord, ReportSummaryRecord, SourceItemRecord
from infrastructure.storage.persistence import GraphRunRecord, ReportRecord, RunPersistenceBatch


ConnectionFactory = Callable[[], Any]


class PostgresRepository:
    def __init__(self, dsn: str, *, connection_factory: ConnectionFactory | None = None) -> None:
        self.dsn = dsn
        self._connection_factory = connection_factory or (lambda: connect_postgres(dsn))

    def migrate(self) -> None:
        sql = load_migration_sql()
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                _cursor_execute(cursor, sql)
            connection.commit()

    def save_graph_run(self, record: GraphRunRecord) -> None:
        self._execute_with_cursor(self._insert_graph_run, record)

    def save_report(self, record: ReportRecord) -> None:
        self._execute_with_cursor(self._insert_report, record)

    def save_source_item(self, record: SourceItemRecord) -> None:
        self._execute_with_cursor(self._insert_source_item, record)

    def save_evidence_item(self, record: EvidenceItemRecord) -> None:
        self._execute_with_cursor(self._insert_evidence_item, record)

    def save_claim(self, record: ClaimRecord) -> None:
        self._execute_with_cursor(self._insert_claim, record)

    def save_quality_result(self, record: QualityResultRecord) -> None:
        self._execute_with_cursor(self._insert_quality_result, record)

    def save_run_records(self, batch: RunPersistenceBatch) -> None:
        with self._connection_factory() as connection:
            with uncommitted_write(), connection.cursor() as cursor:
                self._insert_graph_run(cursor, batch.graph_run)
                if batch.report is not None:
                    self._insert_report(cursor, batch.report)
                for source_item in batch.source_items:
                    self._insert_source_item(cursor, source_item)
                for evidence_item in batch.evidence_items:
                    self._insert_evidence_item(cursor, evidence_item)
                for claim in batch.claims:
                    self._insert_claim(cursor, claim)
                if batch.quality_result is not None:
                    self._insert_quality_result(cursor, batch.quality_result)
            connection.commit()

    def update_source_health(self, health: SourceHealth) -> None:
        sql = """
        INSERT INTO source_health (
            source_id, source_name, url, status, consecutive_failures,
            last_success_at, last_failure_at, cooldown_until, last_error,
            success_count_24h, failure_count_24h, avg_latency_ms_24h
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (source_id) DO UPDATE SET
            source_name = EXCLUDED.source_name,
            url = EXCLUDED.url,
            status = EXCLUDED.status,
            consecutive_failures = EXCLUDED.consecutive_failures,
            last_success_at = EXCLUDED.last_success_at,
            last_failure_at = EXCLUDED.last_failure_at,
            cooldown_until = EXCLUDED.cooldown_until,
            last_error = EXCLUDED.last_error,
            success_count_24h = EXCLUDED.success_count_24h,
            failure_count_24h = EXCLUDED.failure_count_24h,
            avg_latency_ms_24h = EXCLUDED.avg_latency_ms_24h,
            updated_at = now()
        """
        params = (
            health.source_id,
            health.source_name,
            health.url,
            health.status.value,
            health.consecutive_failures,
            health.last_success_at,
            health.last_failure_at,
            health.cooldown_until,
            _json_or_none(health.last_error.to_dict() if health.last_error else None),
            health.success_count_24h,
            health.failure_count_24h,
            health.avg_latency_ms_24h,
        )
        self._execute(sql, params)

    def get_source_health(self, source_id: str) -> SourceHealth | None:
        sql = """
        SELECT
            source_id, source_name, url, status, consecutive_failures,
            last_success_at, last_failure_at, cooldown_until, last_error,
            success_count_24h, failure_count_24h, avg_latency_ms_24h
        FROM source_health
        WHERE source_id = %s
        """
        row = self._fetch_one(sql, (source_id,))
        return _source_health_from_row(row) if row is not None else None

    def list_source_health(self, *, status: str | None = None) -> list[SourceHealth]:
        where = ""
        params: tuple[Any, ...] = ()
        if status is not None:
            where = "WHERE status = %s"
            params = (status,)
        sql = f"""
        SELECT
            source_id, source_name, url, status, consecutive_failures,
            last_success_at, last_failure_at, cooldown_until, last_error,
            success_count_24h, failure_count_24h, avg_latency_ms_24h
        FROM source_health
        {where}
        ORDER BY source_id
        """
        return [_source_health_from_row(row) for row in self._fetch_all(sql, params)]

    def latest_report(self) -> ReportDetailRecord:
        sql = """
        SELECT
            r.report_id, r.run_id, r.status, r.title, r.report_json,
            r.report_markdown, r.quality_score, r.citation_coverage_score,
            r.manifest_path, r.updated_at
        FROM reports r
        ORDER BY r.updated_at DESC
        LIMIT 1
        """
        row = self._fetch_one(sql, ())
        if row is None:
            raise ReportNotFoundError("no PostgreSQL report found")
        return _detail_from_row(row)

    def get_report(self, report_id: str) -> ReportDetailRecord:
        sql = """
        SELECT
            r.report_id, r.run_id, r.status, r.title, r.report_json,
            r.report_markdown, r.quality_score, r.citation_coverage_score,
            r.manifest_path, r.updated_at
        FROM reports r
        WHERE r.report_id = %s
        """
        row = self._fetch_one(sql, (report_id,))
        if row is None:
            raise ReportNotFoundError(f"report not found: {report_id}")
        return _detail_from_row(row)

    def list_reports(
        self,
        *,
        limit: int = 20,
        graph_id: str | None = None,
        graph_ids: tuple[str, ...] | None = None,
    ) -> list[ReportSummaryRecord]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if graph_id and graph_ids:
            raise ValueError("graph_id and graph_ids are mutually exclusive")
        if graph_ids is not None and not graph_ids:
            raise ValueError("graph_ids must not be empty")
        where = ""
        params: tuple[Any, ...]
        if graph_id:
            where = "WHERE gr.graph_id = %s"
            params = (graph_id, limit)
        elif graph_ids:
            placeholders = ", ".join(["%s"] * len(graph_ids))
            where = f"WHERE gr.graph_id IN ({placeholders})"
            params = (*graph_ids, limit)
        else:
            params = (limit,)
        sql = f"""
        SELECT
            r.report_id, r.run_id, r.status, r.title, r.quality_score,
            r.citation_coverage_score, r.manifest_path, r.updated_at,
            gr.graph_id, gr.graph_version
        FROM reports r
        LEFT JOIN graph_runs gr ON gr.run_id = r.run_id
        {where}
        ORDER BY r.updated_at DESC
        LIMIT %s
        """
        rows = self._fetch_all(sql, params)
        return [_list_record_from_row(row) for row in rows]

    def search_reports(self, query: str, *, limit: int = 20) -> list[ReportSummaryRecord]:
        sql = """
        SELECT
            r.report_id, r.run_id, r.status, r.title, r.quality_score,
            r.citation_coverage_score, r.manifest_path, r.updated_at,
            gr.graph_id, gr.graph_version
        FROM reports r
        LEFT JOIN graph_runs gr ON gr.run_id = r.run_id
        WHERE
            COALESCE(r.title, '') ILIKE %s
            OR COALESCE(r.report_markdown, '') ILIKE %s
            OR r.report_json::text ILIKE %s
        ORDER BY r.updated_at DESC
        LIMIT %s
        """
        pattern = f"%{query}%"
        rows = self._fetch_all(sql, (pattern, pattern, pattern, limit))
        return [_search_record_from_row(row) for row in rows]

    def list_source_items(self, run_id: str) -> list[dict[str, Any]]:
        sql = """
        SELECT payload
        FROM source_items
        WHERE run_id = %s
        ORDER BY published_at ASC NULLS LAST, source_item_id ASC
        """
        return [_dict_or_empty(row[0]) for row in self._fetch_all(sql, (run_id,))]

    def list_evidence_items(self, run_id: str) -> list[dict[str, Any]]:
        sql = """
        SELECT payload
        FROM evidence_items
        WHERE run_id = %s
        ORDER BY published_at ASC NULLS LAST, evidence_id ASC
        """
        return [_dict_or_empty(row[0]) for row in self._fetch_all(sql, (run_id,))]

    def list_claims(self, run_id: str) -> list[dict[str, Any]]:
        sql = """
        SELECT payload
        FROM claims
        WHERE run_id = %s
        ORDER BY created_at ASC, claim_id ASC
        """
        return [_dict_or_empty(row[0]) for row in self._fetch_all(sql, (run_id,))]

    def list_quality_results(self, run_id: str) -> list[dict[str, Any]]:
        sql = """
        SELECT payload
        FROM quality_results
        WHERE run_id = %s
        ORDER BY created_at ASC, quality_result_id ASC
        """
        return [_dict_or_empty(row[0]) for row in self._fetch_all(sql, (run_id,))]

    def _insert_graph_run(self, cursor: Any, record: GraphRunRecord) -> None:
        sql = """
        INSERT INTO graph_runs (
            run_id, graph_id, graph_version, status, profile,
            artifact_dir, manifest_path, events_path, error, metrics
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (run_id) DO UPDATE SET
            status = EXCLUDED.status,
            artifact_dir = EXCLUDED.artifact_dir,
            manifest_path = EXCLUDED.manifest_path,
            events_path = EXCLUDED.events_path,
            error = EXCLUDED.error,
            metrics = EXCLUDED.metrics,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.run_id,
                record.graph_id,
                record.graph_version,
                record.status,
                record.profile,
                record.artifact_dir,
                record.manifest_path,
                record.events_path,
                _json(record.error),
                _json(record.metrics),
            ),
        )

    def _insert_report(self, cursor: Any, record: ReportRecord) -> None:
        sql = """
        INSERT INTO reports (
            report_id, run_id, status, title, report_json,
            report_markdown, quality_score, citation_coverage_score, manifest_path
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
        ON CONFLICT (report_id) DO UPDATE SET
            status = EXCLUDED.status,
            title = EXCLUDED.title,
            report_json = EXCLUDED.report_json,
            report_markdown = EXCLUDED.report_markdown,
            quality_score = EXCLUDED.quality_score,
            citation_coverage_score = EXCLUDED.citation_coverage_score,
            manifest_path = EXCLUDED.manifest_path,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.report_id,
                record.run_id,
                record.status,
                record.title,
                _json(record.report_json),
                record.report_markdown,
                record.quality_score,
                record.citation_coverage_score,
                record.manifest_path,
            ),
        )

    def _insert_source_item(self, cursor: Any, record: SourceItemRecord) -> None:
        sql = """
        INSERT INTO source_items (
            source_item_id, run_id, source_id, title, url, canonical_url,
            published_at, fetched_at, raw_artifact_id, payload, metadata_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (source_item_id) DO UPDATE SET
            run_id = EXCLUDED.run_id,
            source_id = EXCLUDED.source_id,
            title = EXCLUDED.title,
            url = EXCLUDED.url,
            canonical_url = EXCLUDED.canonical_url,
            published_at = EXCLUDED.published_at,
            fetched_at = EXCLUDED.fetched_at,
            raw_artifact_id = EXCLUDED.raw_artifact_id,
            payload = EXCLUDED.payload,
            metadata_json = EXCLUDED.metadata_json,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.source_item_id,
                record.run_id,
                record.source_id,
                record.title,
                record.url,
                record.canonical_url,
                record.published_at,
                record.fetched_at,
                record.raw_artifact_id,
                _json(record.to_dict()),
                _json(record.metadata),
            ),
        )

    def _insert_evidence_item(self, cursor: Any, record: EvidenceItemRecord) -> None:
        sql = """
        INSERT INTO evidence_items (
            evidence_id, run_id, source_url, source_urls, source_item_ids,
            title, summary, confidence, category, published_at, lineage_json,
            payload, metadata_json
        )
        VALUES (
            %s, %s, %s, %s::jsonb, %s::jsonb,
            %s, %s, %s, %s, %s, %s::jsonb,
            %s::jsonb, %s::jsonb
        )
        ON CONFLICT (evidence_id) DO UPDATE SET
            run_id = EXCLUDED.run_id,
            source_url = EXCLUDED.source_url,
            source_urls = EXCLUDED.source_urls,
            source_item_ids = EXCLUDED.source_item_ids,
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            confidence = EXCLUDED.confidence,
            category = EXCLUDED.category,
            published_at = EXCLUDED.published_at,
            lineage_json = EXCLUDED.lineage_json,
            payload = EXCLUDED.payload,
            metadata_json = EXCLUDED.metadata_json,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.evidence_id,
                record.run_id,
                record.source_urls[0] if record.source_urls else "",
                _json_list(record.source_urls),
                _json_list(record.source_item_ids),
                record.claim,
                record.summary,
                record.confidence,
                record.category,
                record.published_at,
                _json(record.lineage_json),
                _json(record.to_dict()),
                _json(record.metadata),
            ),
        )

    def _insert_claim(self, cursor: Any, record: ClaimRecord) -> None:
        sql = """
        INSERT INTO claims (
            claim_id, run_id, status, text, payload
        )
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (claim_id) DO UPDATE SET
            run_id = EXCLUDED.run_id,
            status = EXCLUDED.status,
            text = EXCLUDED.text,
            payload = EXCLUDED.payload,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.claim_id,
                record.run_id,
                record.status,
                record.text,
                _json(record.to_dict()),
            ),
        )
        cursor.execute("DELETE FROM claim_supports WHERE claim_id = %s", (record.claim_id,))
        self._insert_claim_supports(cursor, record, "supporting", record.supporting_evidence_ids)
        self._insert_claim_supports(cursor, record, "rejecting", record.rejecting_evidence_ids)

    def _insert_claim_supports(
        self,
        cursor: Any,
        record: ClaimRecord,
        support_type: str,
        evidence_ids: list[str],
    ) -> None:
        sql = """
        INSERT INTO claim_supports (
            claim_support_id, claim_id, run_id, evidence_id, support_type, confidence, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (claim_support_id) DO UPDATE SET
            run_id = EXCLUDED.run_id,
            evidence_id = EXCLUDED.evidence_id,
            support_type = EXCLUDED.support_type,
            confidence = EXCLUDED.confidence,
            payload = EXCLUDED.payload
        """
        for evidence_id in evidence_ids:
            payload = {
                "claim_id": record.claim_id,
                "evidence_id": evidence_id,
                "support_type": support_type,
                "supporting_sources": list(record.supporting_sources),
                "rejecting_sources": list(record.rejecting_sources),
            }
            cursor.execute(
                sql,
                (
                    _claim_support_id(record.claim_id, support_type, evidence_id),
                    record.claim_id,
                    record.run_id,
                    evidence_id,
                    support_type,
                    record.confidence,
                    _json(payload),
                ),
            )

    def _insert_quality_result(self, cursor: Any, record: QualityResultRecord) -> None:
        sql = """
        INSERT INTO quality_results (
            quality_result_id, run_id, decision, passed, quality_score,
            citation_coverage_score, claim_support_score, evidence_alignment_score, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (quality_result_id) DO UPDATE SET
            run_id = EXCLUDED.run_id,
            decision = EXCLUDED.decision,
            passed = EXCLUDED.passed,
            quality_score = EXCLUDED.quality_score,
            citation_coverage_score = EXCLUDED.citation_coverage_score,
            claim_support_score = EXCLUDED.claim_support_score,
            evidence_alignment_score = EXCLUDED.evidence_alignment_score,
            payload = EXCLUDED.payload,
            updated_at = now()
        """
        cursor.execute(
            sql,
            (
                record.quality_result_id,
                record.run_id,
                record.decision,
                record.passed,
                record.quality_score,
                record.citation_coverage_score,
                record.claim_support_score,
                record.evidence_alignment_score,
                _json(record.payload),
            ),
        )

    def _execute_with_cursor(self, operation: Callable[[Any, Any], None], record: Any) -> None:
        with self._connection_factory() as connection:
            with uncommitted_write(), connection.cursor() as cursor:
                operation(cursor, record)
            connection.commit()

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.execute(sql, params)

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connection_factory() as connection:
            with uncommitted_write(), connection.cursor() as cursor:
                _cursor_execute(cursor, sql, params)
            connection.commit()

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        return self.fetch_one(sql, params)

    def fetch_one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                _cursor_execute(cursor, sql, params)
                return cursor.fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        return self.fetch_all(sql, params)

    def fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                _cursor_execute(cursor, sql, params)
                return list(cursor.fetchall())


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _cursor_execute(cursor: Any, sql: str, params: tuple[Any, ...] | None = None) -> Any:
    if params is None:
        return cursor.execute(sql)
    return cursor.execute(sql, params)


def _json_list(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False, sort_keys=True)


def _claim_support_id(claim_id: str, support_type: str, evidence_id: str) -> str:
    return f"{claim_id}:{support_type}:{evidence_id}"


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _detail_from_row(row: tuple[Any, ...]) -> ReportDetailRecord:
    return ReportDetailRecord(
        report_id=str(row[0]),
        run_id=str(row[1]),
        status=str(row[2]),
        title=row[3],
        report_json=_dict_or_none(row[4]),
        report_markdown=row[5],
        quality_score=row[6],
        citation_coverage_score=row[7],
        manifest_path=row[8],
        finished_at=_timestamp(row[9]),
    )


def _search_record_from_row(row: tuple[Any, ...]) -> ReportSummaryRecord:
    return ReportSummaryRecord(
        report_id=str(row[0]),
        run_id=str(row[1]),
        status=str(row[2]),
        title=row[3],
        quality_score=row[4],
        citation_coverage_score=row[5],
        manifest_path=row[6],
        finished_at=_timestamp(row[7]),
        graph_id=row[8],
        graph_version=row[9],
    )


def _list_record_from_row(row: tuple[Any, ...]) -> ReportSummaryRecord:
    return ReportSummaryRecord(
        report_id=str(row[0]),
        run_id=str(row[1]),
        status=str(row[2]),
        title=row[3],
        quality_score=row[4],
        citation_coverage_score=row[5],
        manifest_path=row[6],
        finished_at=_timestamp(row[7]),
        graph_id=row[8],
        graph_version=row[9],
    )


def _source_health_from_row(row: tuple[Any, ...]) -> SourceHealth:
    return SourceHealth(
        source_id=str(row[0]),
        source_name=row[1],
        url=row[2],
        status=SourceHealthStatus(str(row[3])),
        consecutive_failures=int(row[4] or 0),
        last_success_at=_datetime_or_none(row[5]),
        last_failure_at=_datetime_or_none(row[6]),
        cooldown_until=_datetime_or_none(row[7]),
        last_error=_source_error_from_payload(row[8]),
        success_count_24h=int(row[9] or 0),
        failure_count_24h=int(row[10] or 0),
        avg_latency_ms_24h=(float(row[11]) if row[11] is not None else None),
    )


def _source_error_from_payload(value: Any) -> SourceError | None:
    payload = _dict_or_none(value)
    if not payload:
        return None
    normalized = normalize_source_errors(
        [payload],
        context="postgres source health last_error",
    )[0]
    return SourceError(
        source_id=normalized.source_id,
        source_name=normalized.source_name,
        error_type=normalized.error_type,
        error_message=normalized.error_message,
        url=normalized.url,
        retryable=normalized.retryable,
        request_ref=normalized.request_ref,
        response_ref=normalized.response_ref,
        occurred_at=normalized.occurred_at,
        metadata=dict(normalized.metadata),
    )


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else None
    return dict(value)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    parsed = _dict_or_none(value)
    return parsed if parsed is not None else {}


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value or "")
