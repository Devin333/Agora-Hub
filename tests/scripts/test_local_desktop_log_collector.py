from __future__ import annotations

import io

from scripts.local_desktop.log_collector import _read_bounded_line, _redact_line, collect


def test_log_collector_redacts_sensitive_headers_before_write() -> None:
    payload = _redact_line(b"Authorization: bearer-secret Cookie=session-secret\n", truncated=False)

    assert b"bearer-secret" not in payload
    assert b"session-secret" not in payload
    assert payload == b"event=log_content_omitted\n"


def test_log_collector_bounds_and_drains_oversized_line() -> None:
    stream = io.BytesIO((b"x" * (70 * 1024)) + b"\nnext\n")

    first = _read_bounded_line(stream)
    second = _read_bounded_line(stream)

    assert first is not None and first[1] is True
    assert second == (b"next\n", False)


def test_log_collector_keeps_only_structured_operational_metadata() -> None:
    payload = _redact_line(
        b'{"level":"error","status":"failed","reason_code":"provider_timeout","token":"tok-secret","session":"sid-secret","prompt":"private prompt","messages":[{"content":"private"}],"response":{"raw":"private output"}}\n',
        truncated=False,
    )

    assert payload == b'{"level":"error","reason_code":"provider_timeout","status":"failed"}\n'
    assert b"tok-secret" not in payload
    assert b"private" not in payload


def test_log_collector_omits_unknown_plaintext_and_multiline_content() -> None:
    first = _redact_line(b"user prompt containing private paper text\n", truncated=False)
    second = _redact_line(b"upstream raw response body\n", truncated=False)

    assert first == b"event=log_content_omitted\n"
    assert second == b"event=log_content_omitted\n"


def test_log_collector_preserves_bounded_plain_operational_line() -> None:
    payload = _redact_line(b"level=info event=server_ready status=ready role=api pid=1234\n", truncated=False)

    assert payload == b"level=info event=server_ready status=ready role=api pid=1234\n"



def test_log_collector_drops_freeform_error_and_reason_but_keeps_stable_diagnostics() -> None:
    payload = _redact_line(
        b'{"event":"provider_failed","error":"full upstream response and private paper text","reason":"user prompt body","error_type":"ProviderTimeout","reason_code":"provider_timeout"}\n',
        truncated=False,
    )

    assert payload == b'{"error_type":"ProviderTimeout","event":"provider_failed","reason_code":"provider_timeout"}\n'
    assert b"upstream" not in payload
    assert b"prompt" not in payload


def test_log_collector_omits_structured_values_that_are_not_stable_operational_tokens() -> None:
    payload = _redact_line(
        b'{"event":"server started with raw response","status":"ready now","reason_code":"ok","prompt":"secret","content":"private"}\n',
        truncated=False,
    )

    assert payload == b'{"reason_code":"ok"}\n'


def test_log_collector_writes_owned_lifecycle_events_around_omitted_content(tmp_path) -> None:
    target = tmp_path / "api.stdout.log"
    collect(target, max_bytes=4096, retain=2, stream=io.BytesIO(b'private prompt\n{"event":"server_ready","status":"ready","pid":42}\n'))

    written = target.read_text(encoding="utf-8")
    assert "event=collector_started role=api" in written
    assert "event=log_content_omitted" in written
    assert '{"event":"server_ready","pid":42,"status":"ready"}' in written
    assert "event=collector_stream_closed role=api" in written
    assert "private prompt" not in written
