from __future__ import annotations

import json

import pytest

from interfaces.cli import news as news_cli
from interfaces.cli.commands import events as event_commands


@pytest.mark.parametrize(
    ("argv", "method", "expected_kwargs"),
    [
        (
            [
                "events",
                "quarantine",
                "list",
                "--reason",
                "missing_occurred_at",
                "--disposition",
                "pending",
                "--cursor",
                "quarantine-cursor",
                "--limit",
                "17",
                "--json",
            ],
            "list_quarantine",
            {
                "reason": "missing_occurred_at",
                "disposition": "pending",
                "cursor": "quarantine-cursor",
                "limit": 17,
            },
        ),
        (
            [
                "events",
                "replay-reports",
                "list",
                "--source-stream-id",
                "run:run-1",
                "--mode",
                "verify_history",
                "--status",
                "failed",
                "--cursor",
                "replay-cursor",
                "--limit",
                "23",
                "--json",
            ],
            "list_replay_reports",
            {
                "source_stream_id": "run:run-1",
                "mode": "verify_history",
                "status": "failed",
                "cursor": "replay-cursor",
                "limit": 23,
            },
        ),
        (
            [
                "events",
                "dead-letters",
                "list",
                "--subscription-id",
                "consumer-a",
                "--subscription-version",
                "3",
                "--disposition",
                "open",
                "--cursor",
                "dead-letter-cursor",
                "--limit",
                "31",
                "--json",
            ],
            "list_dead_letters",
            {
                "subscription_id": "consumer-a",
                "subscription_version": 3,
                "disposition": "open",
                "cursor": "dead-letter-cursor",
                "limit": 31,
            },
        ),
    ],
)
def test_event_operator_list_commands_forward_filters_and_cursor(
    argv,
    method,
    expected_kwargs,
    monkeypatch,
    capsys,
) -> None:
    payload = {
        "availability": "available",
        "items": [],
        "next_cursor": "next-cursor",
        "tenant_id": "tenant-a",
        "unavailable_reason_class": None,
    }
    service = _FakeOperatorService(payload)
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    exit_code = news_cli.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert service.calls == [(method, (), expected_kwargs)]
    assert captured.err == ""
    assert captured.out.strip() == json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_runtime_projection_rebuild_forwards_resume_scope(
    monkeypatch,
    capsys,
) -> None:
    payload = {
        "availability": "available",
        "tenant_id": "tenant-a",
        "replay_report": {"replay_id": "runtime-replay", "status": "succeeded"},
        "checkpoint": {"checkpoint_id": "checkpoint-runtime", "last_sequence": 4},
    }
    service = _FakeOperatorService(payload)
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    exit_code = news_cli.main(
        [
            "events",
            "runtime-projection",
            "rebuild",
            "--replay-id",
            "runtime-replay",
            "--source-stream-id",
            "runtime:run-1",
            "--from-sequence",
            "3",
            "--checkpoint-ref",
            "checkpoint-prefix",
            "--reason",
            "resume verified runtime projection",
            "--yes",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert service.calls == [
        (
            "rebuild_runtime_projection",
            (),
            {
                "replay_id": "runtime-replay",
                "source_stream_id": "runtime:run-1",
                "operator_reason": "resume verified runtime projection",
                "from_sequence": 3,
                "checkpoint_ref": "checkpoint-prefix",
            },
        )
    ]
    assert captured.err == ""
    assert json.loads(captured.out) == payload


def test_runtime_projection_rebuild_requires_complete_resume_pair(
    monkeypatch,
    capsys,
) -> None:
    service = _FakeOperatorService({})
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    exit_code = news_cli.main(
        [
            "events",
            "runtime-projection",
            "rebuild",
            "--replay-id",
            "runtime-replay",
            "--source-stream-id",
            "runtime:run-1",
            "--checkpoint-ref",
            "checkpoint-prefix",
            "--reason",
            "incomplete resume",
            "--yes",
        ]
    )

    assert exit_code == 1
    assert service.calls == []
    assert "must be supplied together" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "method", "record_id"),
    [
        (
            ["events", "quarantine", "show", "quarantine-1", "--json"],
            "get_quarantine",
            "quarantine-1",
        ),
        (
            ["events", "replay-reports", "show", "replay-1", "--json"],
            "get_replay_report",
            "replay-1",
        ),
        (
            ["events", "dead-letters", "show", "dead-letter-1", "--json"],
            "get_dead_letter",
            "dead-letter-1",
        ),
    ],
)
def test_event_operator_show_commands_use_record_identity(
    argv,
    method,
    record_id,
    monkeypatch,
    capsys,
) -> None:
    payload = {
        "availability": "available",
        "found": True,
        "tenant_id": "tenant-a",
    }
    service = _FakeOperatorService(payload)
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    assert news_cli.main(argv) == 0

    assert service.calls == [(method, (record_id,), {})]
    assert json.loads(capsys.readouterr().out) == payload


def test_event_operator_mutations_use_deployment_identity_and_confirmation(
    monkeypatch,
    capsys,
) -> None:
    service = _FakeOperatorService(
        {
            "availability": "available",
            "dead_letter": {"dead_letter_id": "dead-letter-1"},
            "tenant_id": "tenant-a",
        }
    )
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    resolve_exit = news_cli.main(
        [
            "events",
            "dead-letters",
            "resolve",
            "dead-letter-1",
            "--reason",
            "incident closed",
            "--yes",
            "--json",
        ]
    )
    capsys.readouterr()
    requeue_exit = news_cli.main(
        [
            "events",
            "dead-letters",
            "requeue",
            "dead-letter-2",
            "--subscription-id",
            "consumer-a",
            "--subscription-version",
            "4",
            "--reason",
            "consumer repaired",
            "--yes",
            "--json",
        ]
    )

    assert resolve_exit == 0
    assert requeue_exit == 0
    assert service.calls == [
        (
            "resolve_dead_letter",
            ("dead-letter-1",),
            {"operator_reason": "incident closed"},
        ),
        (
            "requeue_dead_letter",
            ("dead-letter-2",),
            {
                "subscription_id": "consumer-a",
                "subscription_version": 4,
                "operator_reason": "consumer repaired",
                "idempotency_acknowledged": True,
            },
        ),
    ]
    assert json.loads(capsys.readouterr().out)["availability"] == "available"


def test_event_operator_status_commands_forward_only_target_scope(
    monkeypatch,
    capsys,
) -> None:
    service = _FakeOperatorService(
        {
            "availability": "available",
            "found": True,
            "tenant_id": "tenant-a",
        }
    )
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    consumer_exit = news_cli.main(
        [
            "events",
            "consumer-status",
            "--subscription-id",
            "consumer-a",
            "--subscription-version",
            "2",
            "--stream-id",
            "run:run-1",
            "--json",
        ]
    )
    capsys.readouterr()
    service.payload = {
        "durable_high_watermark": 9,
        "projection_high_watermark": 7,
        "projection_status": "stale",
        "run_id": "run-1",
        "status": "stale",
        "stream_id": "run:run-1",
        "tenant_id": "tenant-a",
    }
    projection_exit = news_cli.main(
        ["events", "projection-status", "run-1", "--json"]
    )

    assert consumer_exit == 0
    assert projection_exit == 0
    assert service.calls == [
        (
            "get_consumer_status",
            (),
            {
                "subscription_id": "consumer-a",
                "subscription_version": 2,
                "stream_id": "run:run-1",
            },
        ),
        ("get_projection_status", ("run-1",), {}),
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "stale"


def test_event_operator_unavailable_payload_returns_exit_two(
    monkeypatch,
    capsys,
) -> None:
    payload = {
        "availability": "unavailable",
        "items": [],
        "next_cursor": None,
        "tenant_id": "tenant-a",
        "unavailable_reason_class": "EventStoreUnavailableError",
    }
    service = _FakeOperatorService(payload)
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    exit_code = news_cli.main(["events", "quarantine", "list", "--json"])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == payload


def test_event_projection_unavailable_status_returns_exit_two(
    monkeypatch,
    capsys,
) -> None:
    payload = {
        "durable_high_watermark": None,
        "projection_high_watermark": 7,
        "run_id": "run-1",
        "status": "unavailable",
        "stream_id": "run:run-1",
        "tenant_id": "tenant-a",
        "unavailable_reason_class": "EventStoreUnavailableError",
    }
    service = _FakeOperatorService(payload)
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    exit_code = news_cli.main(
        ["events", "projection-status", "run-1", "--json"]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == payload


def test_event_operator_factory_failure_is_bounded_and_secret_free(
    monkeypatch,
    capsys,
) -> None:
    secret = "operator-secret-must-not-leak"

    def fail_closed():
        raise RuntimeError(f"postgresql://operator:{secret}@host/events")

    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        fail_closed,
    )

    exit_code = news_cli.main(["events", "dead-letters", "list"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.strip() == "event operator command failed"
    assert secret not in captured.err
    assert "postgresql://" not in captured.err


def test_event_operator_real_factory_fails_closed_without_deployment_identity(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("NEWS_EVENT_OPERATOR_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("NEWS_TENANT_ID", raising=False)

    exit_code = news_cli.main(["events", "quarantine", "list"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.strip() == "event operator command failed"


def test_event_operator_mutations_require_reason_and_confirmation() -> None:
    with pytest.raises(SystemExit) as missing_confirmation:
        news_cli.main(
            [
                "events",
                "dead-letters",
                "resolve",
                "dead-letter-1",
                "--reason",
                "resolved",
            ]
        )
    with pytest.raises(SystemExit) as missing_reason:
        news_cli.main(
            [
                "events",
                "dead-letters",
                "requeue",
                "dead-letter-1",
                "--subscription-id",
                "consumer-a",
                "--subscription-version",
                "1",
                "--yes",
            ]
        )

    assert missing_confirmation.value.code == 2
    assert missing_reason.value.code == 2


def test_event_operator_cli_does_not_expose_identity_or_time_overrides(
    capsys,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        news_cli.main(["events", "dead-letters", "requeue", "--help"])

    help_text = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "--operator-id" not in help_text
    assert "--tenant-id" not in help_text
    assert "--requested-at" not in help_text
    assert "--idempotency-ready" not in help_text
    assert "--reason" in help_text
    assert "--yes" in help_text


def test_event_operator_text_output_is_concise_and_omits_record_payload(
    monkeypatch,
    capsys,
) -> None:
    service = _FakeOperatorService(
        {
            "availability": "available",
            "items": [
                {
                    "dead_letter_id": "dead-letter-1",
                    "disposition": "open",
                    "payload": {"credential": "must-not-print"},
                }
            ],
            "next_cursor": None,
            "tenant_id": "tenant-a",
            "unavailable_reason_class": None,
        }
    )
    monkeypatch.setattr(
        event_commands,
        "event_operator_service_from_env",
        lambda: service,
    )

    assert news_cli.main(["events", "dead-letters", "list"]) == 0

    output = capsys.readouterr().out
    assert "availability=available" in output
    assert "item_count=1" in output
    assert "dead_letter_id=dead-letter-1" in output
    assert "must-not-print" not in output


class _FakeOperatorService:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, tuple, dict]] = []

    def _call(self, method: str, args: tuple, kwargs: dict) -> dict:
        self.calls.append((method, args, kwargs))
        return self.payload

    def list_quarantine(self, **kwargs):
        return self._call("list_quarantine", (), kwargs)

    def get_quarantine(self, quarantine_id):
        return self._call("get_quarantine", (quarantine_id,), {})

    def list_replay_reports(self, **kwargs):
        return self._call("list_replay_reports", (), kwargs)

    def get_replay_report(self, replay_id):
        return self._call("get_replay_report", (replay_id,), {})

    def rebuild_runtime_projection(self, **kwargs):
        return self._call("rebuild_runtime_projection", (), kwargs)

    def list_dead_letters(self, **kwargs):
        return self._call("list_dead_letters", (), kwargs)

    def get_dead_letter(self, dead_letter_id):
        return self._call("get_dead_letter", (dead_letter_id,), {})

    def resolve_dead_letter(self, dead_letter_id, **kwargs):
        return self._call("resolve_dead_letter", (dead_letter_id,), kwargs)

    def requeue_dead_letter(self, dead_letter_id, **kwargs):
        return self._call("requeue_dead_letter", (dead_letter_id,), kwargs)

    def get_consumer_status(self, **kwargs):
        return self._call("get_consumer_status", (), kwargs)

    def get_projection_status(self, run_id):
        return self._call("get_projection_status", (run_id,), {})
