from __future__ import annotations

from dataclasses import replace
import json

import pytest

from backend.research.application.parse_paper import (
    InMemoryResearchEventSink,
    ParsePaperError,
    ParsePaperRequest,
    ParsePaperUseCase,
    _PARSER_DIAGNOSTIC_DETAIL_TEXT_LIMIT,
    _PARSER_DIAGNOSTIC_TEXT_LIMIT,
    _parser_failure_attempt,
    _sanitize_parser_attempt,
)
from framework.execution_environment.errors import (
    ExecutionEnvironmentUnavailableError,
    ExecutionPolicyViolationError,
)
from tests.backend.research.application.test_parse_paper_catalog_contract import _Parser, _Resolver


_SECRETS = (
    "provider-key-value",
    "cookie-value",
    "query-secret-value",
    "uri-password-value",
    "bearer-token-value",
    "sk-notarealtestcredential",
    "private-key-value",
    "environment-secret-value",
)


def _execution_error():
    return ExecutionEnvironmentUnavailableError(
        "Marker process failed at import\n"
        "Authorization: Bearer bearer-token-value\n"
        "api_key=provider-key-value\n"
        "source=https://worker:uri-password-value@example.test/model\n"
        "url=https://example.test/model?token=query-secret-value\n"
        "sk-notarealtestcredential\n" + "m" * 4000,
        details={
            "backend": "marker",
            "status": "indeterminate",
            "reason_code": "process_exit_nonzero",
            "exit_code": 137,
            "termination_confirmed": False,
            "execution_id": "exec:marker:failed",
            "attempt_id": "attempt:marker:failed",
            "receipt_checksum": "sha256:" + "e" * 64,
            "provider_diagnostic": 'import failed\n{"cookie": "cookie-value"}',
            "process_output_excerpt": "x" * 5000 + "\nModuleNotFoundError: No module named 'marker'",
            "stderr": (
                "-----BEGIN PRIVATE KEY-----\nprivate-key-value\n-----END PRIVATE KEY-----\n"
                "api_key=provider-key-value\nImportError: missing marker dependency\x00\x1b"
            ),
            "environment": {"API_KEY": "environment-secret-value"},
            "argv": ["--password", "environment-secret-value"],
            "arbitrary_nested_payload": {"stderr": "environment-secret-value"},
        },
    )


class _FailedParser:
    def parse(self, paper_id, source_bytes):
        raise _execution_error()


class _FailedCompiler:
    def compile(self, source, *, content=None):
        raise _execution_error()


class _PdfResolver(_Resolver):
    def resolve(self, request):
        resolved = super().resolve(request)
        return replace(
            resolved,
            content_type="application/pdf",
            snapshot=resolved.snapshot.model_copy(update={"content_type": "application/pdf"}),
        )


def _assert_safe_attempt(attempt, *, backend):
    assert attempt["backend"] == backend
    assert attempt["status"] == "parse_error"
    assert attempt["error_type"] == "ExecutionEnvironmentUnavailableError"
    # The receipt's actual failure must not become the wrapper's generic code.
    assert attempt["reason_code"] == "process_exit_nonzero"
    assert attempt["message"].startswith("Marker process failed at import")
    assert len(attempt["message"]) <= _PARSER_DIAGNOSTIC_TEXT_LIMIT
    details = attempt["details"]
    assert details["status"] == "indeterminate"
    assert details["termination_confirmed"] is False
    assert details["exit_code"] == 137
    assert details["reason_code"] == "process_exit_nonzero"
    assert details["execution_id"] == "exec:marker:failed"
    assert details["attempt_id"] == "attempt:marker:failed"
    assert details["receipt_checksum"] == "sha256:" + "e" * 64
    assert details["provider_diagnostic"].startswith("import failed")
    assert details["process_output_excerpt"].endswith("ModuleNotFoundError: No module named 'marker'")
    assert details["stderr"].endswith("ImportError: missing marker dependency")
    assert "environment" not in details
    assert "argv" not in details
    assert "arbitrary_nested_payload" not in details
    assert all(len(item) <= _PARSER_DIAGNOSTIC_DETAIL_TEXT_LIMIT for item in details.values() if isinstance(item, str))
    encoded = json.dumps(attempt)
    assert len(encoded) < 10000
    assert all(secret not in encoded for secret in _SECRETS)
    assert "\\u0000" not in encoded
    assert "\\u001b" not in encoded


@pytest.mark.parametrize("backend", ["document_parser", "document_compiler"])
def test_typed_execution_failure_keeps_safe_bounded_root_and_is_idempotently_sanitized(backend):
    attempt = _parser_failure_attempt(_execution_error(), backend=backend)
    _assert_safe_attempt(attempt, backend=backend)
    assert _sanitize_parser_attempt(attempt) == attempt


@pytest.mark.parametrize("backend", ["document_parser", "document_compiler"])
@pytest.mark.parametrize("readable_fallback", [False, True])
def test_typed_execution_root_survives_parse_attempts_and_durable_events_without_passing_gate(
    backend, readable_fallback,
):
    sink = InMemoryResearchEventSink()
    use_case = ParsePaperUseCase(
        source_resolver=_PdfResolver(
            source_url="https://publisher.example/broken.pdf",
            paper_id="failed-pdf",
            content=b"%PDF\n" + (b"Readable residual PDF text, not structured evidence." if readable_fallback else b""),
        ),
        document_parser=_FailedParser() if backend == "document_parser" else None,
        document_compiler=_FailedCompiler() if backend == "document_compiler" else None,
        event_sink=sink,
    )
    request = ParsePaperRequest(
        source="https://publisher.example/broken.pdf",
        run_id="failed-parse",
        tenant_id="tenant",
        user_id="reader",
        options={"quality_profile": "reading"},
    )
    if readable_fallback:
        result = use_case.parse(request)
        assert result.status == "degraded"
        assert result.document is not None
        assert result.document.parser_backend == "text_fallback"
        assert result.quality_report["passed"] is False
        assert result.quality_report["catalogEligible"] is False
        assert "parser_execution_degraded" in {item["code"] for item in result.quality_report["hardFailures"]}
        _assert_safe_attempt(result.parser_attempts[0], backend=backend)
        _assert_safe_attempt(result.document.parser_attempts[0], backend=backend)
    else:
        with pytest.raises(ParsePaperError) as captured:
            use_case.parse(request)
        assert captured.value.code == "document_parse_failed"
        _assert_safe_attempt(captured.value.details["parser_attempts"][0], backend=backend)
    events_with_attempts = [event for event in sink.events if event.get("parser_attempts")]
    assert events_with_attempts
    for event in events_with_attempts:
        _assert_safe_attempt(event["parser_attempts"][0], backend=backend)
    final = sink.load_final("failed-parse", actor_scope={"tenant_id": "tenant", "user_id": "reader"})
    assert final is not None
    assert final["status"] == ("degraded" if readable_fallback else "failed")
    if not readable_fallback:
        _assert_safe_attempt(final["parser_attempts"][0], backend=backend)
    serialized = json.dumps(sink.events)
    assert all(secret not in serialized for secret in _SECRETS)


def test_typed_policy_reason_is_preserved_without_receipt_and_untyped_exception_text_stays_private():
    typed = _parser_failure_attempt(
        ExecutionPolicyViolationError("parser denied", reason_code="parser_environment_not_allowed"),
        backend="document_parser",
    )
    assert typed["reason_code"] == "parser_environment_not_allowed"
    assert typed["message"] == "parser denied"
    untyped = _parser_failure_attempt(RuntimeError("raw private exception"), backend="document_parser")
    assert untyped["reason_code"] == "document_parser_failed"
    assert "message" not in untyped
    assert "raw private exception" not in json.dumps(untyped)


@pytest.mark.parametrize("ledger", ["compiler_attempts", "parser_cascade"])
def test_compiler_attempt_ledger_cannot_hide_prior_typed_parser_failure(ledger):
    class _StructuredCompiler:
        def compile(self, source, *, content):
            document = _Parser().parse(source.paper_id, content)
            attempts = [{"backend": "structured_compiler", "status": "succeeded"}]
            return document.model_copy(update={
                "metadata": {ledger: {"attempts": attempts} if ledger == "parser_cascade" else attempts},
            })

    sink = InMemoryResearchEventSink()
    result = ParsePaperUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/structured",
            paper_id="structured-fallback",
            content=b"Structured source content.",
        ),
        document_parser=_FailedParser(),
        document_compiler=_StructuredCompiler(),
        event_sink=sink,
    ).parse(ParsePaperRequest(source="https://publisher.example/structured", run_id="structured-fallback"))
    failures = [attempt for attempt in result.parser_attempts if attempt.get("backend") == "document_parser"]
    assert len(failures) == 1
    _assert_safe_attempt(failures[0], backend="document_parser")
    assert {attempt["backend"] for attempt in result.parser_attempts} == {"structured_compiler", "document_parser"}
    for event in sink.events:
        if event.get("parser_attempts"):
            assert failures[0] in event["parser_attempts"]
