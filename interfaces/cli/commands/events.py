from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Mapping



_QUARANTINE_REASONS = (
    "unknown_envelope_schema",
    "unknown_data_schema",
    "schema_validation_failed",
    "missing_occurred_at",
    "invalid_occurred_at",
    "context_conflict",
    "identity_collision",
    "corrupt_record",
    "unsupported_legacy_mapping",
    "upcast_failed",
    "security_scope_ambiguous",
)
_QUARANTINE_DISPOSITIONS = ("pending", "released", "rejected")
_REPLAY_MODES = ("rebuild_state", "verify_history", "redeliver")
_REPLAY_STATUSES = ("pending", "running", "succeeded", "failed")
_DEAD_LETTER_DISPOSITIONS = ("open", "requeued", "resolved")


def event_operator_service_from_env() -> Any:
    # Keep operator-only composition lazy so deployments can omit optional
    # event operator capabilities.
    from interfaces.services.event_operator_factory import (
        event_operator_service_from_env as factory,
    )

    return factory()


def register(subparsers: argparse._SubParsersAction) -> None:
    events_parser = subparsers.add_parser(
        "events",
        help="Inspect and operate durable Graph event history",
    )
    events_subparsers = events_parser.add_subparsers(
        dest="events_command",
        required=True,
    )
    _register_operator_commands(events_subparsers)


def _register_operator_commands(
    events_subparsers: argparse._SubParsersAction,
) -> None:
    quarantine_parser = events_subparsers.add_parser(
        "quarantine",
        help="Inspect tenant-scoped quarantined event records",
    )
    quarantine_subparsers = quarantine_parser.add_subparsers(
        dest="quarantine_command",
        required=True,
    )
    quarantine_list_parser = quarantine_subparsers.add_parser(
        "list",
        help="List quarantined event records",
    )
    quarantine_list_parser.add_argument("--reason", choices=_QUARANTINE_REASONS)
    quarantine_list_parser.add_argument(
        "--disposition",
        choices=_QUARANTINE_DISPOSITIONS,
    )
    _add_page_arguments(quarantine_list_parser)
    quarantine_list_parser.set_defaults(handler=event_quarantine_list)
    quarantine_show_parser = quarantine_subparsers.add_parser(
        "show",
        help="Show one quarantined event record",
    )
    quarantine_show_parser.add_argument("quarantine_id")
    _add_json_argument(quarantine_show_parser)
    quarantine_show_parser.set_defaults(handler=event_quarantine_show)

    replay_parser = events_subparsers.add_parser(
        "replay-reports",
        help="Inspect tenant-scoped deterministic replay reports",
    )
    replay_subparsers = replay_parser.add_subparsers(
        dest="replay_reports_command",
        required=True,
    )
    replay_list_parser = replay_subparsers.add_parser(
        "list",
        help="List deterministic replay reports",
    )
    replay_list_parser.add_argument("--source-stream-id")
    replay_list_parser.add_argument("--mode", choices=_REPLAY_MODES)
    replay_list_parser.add_argument("--status", choices=_REPLAY_STATUSES)
    _add_page_arguments(replay_list_parser)
    replay_list_parser.set_defaults(handler=event_replay_reports_list)
    replay_show_parser = replay_subparsers.add_parser(
        "show",
        help="Show one deterministic replay report",
    )
    replay_show_parser.add_argument("replay_id")
    _add_json_argument(replay_show_parser)
    replay_show_parser.set_defaults(handler=event_replay_report_show)

    runtime_projection_parser = events_subparsers.add_parser(
        "runtime-projection",
        help="Rebuild the tenant-scoped runtime event projection",
    )
    runtime_projection_subparsers = runtime_projection_parser.add_subparsers(
        dest="runtime_projection_command",
        required=True,
    )
    runtime_projection_rebuild_parser = runtime_projection_subparsers.add_parser(
        "rebuild",
        help="Run a finite deterministic runtime projection rebuild",
    )
    runtime_projection_rebuild_parser.add_argument("--replay-id", required=True)
    runtime_projection_rebuild_parser.add_argument(
        "--source-stream-id",
        required=True,
    )
    runtime_projection_rebuild_parser.add_argument(
        "--from-sequence",
        type=_positive_int,
    )
    runtime_projection_rebuild_parser.add_argument("--checkpoint-ref")
    _add_mutation_arguments(runtime_projection_rebuild_parser)
    runtime_projection_rebuild_parser.set_defaults(
        handler=event_runtime_projection_rebuild
    )

    dead_letters_parser = events_subparsers.add_parser(
        "dead-letters",
        help="Inspect and operate tenant-scoped dead letters",
    )
    dead_letters_subparsers = dead_letters_parser.add_subparsers(
        dest="dead_letters_command",
        required=True,
    )
    dead_letters_list_parser = dead_letters_subparsers.add_parser(
        "list",
        help="List dead letters",
    )
    dead_letters_list_parser.add_argument("--subscription-id")
    dead_letters_list_parser.add_argument(
        "--subscription-version",
        type=_positive_int,
    )
    dead_letters_list_parser.add_argument(
        "--disposition",
        choices=_DEAD_LETTER_DISPOSITIONS,
    )
    _add_page_arguments(dead_letters_list_parser)
    dead_letters_list_parser.set_defaults(handler=event_dead_letters_list)
    dead_letters_show_parser = dead_letters_subparsers.add_parser(
        "show",
        help="Show one dead letter",
    )
    dead_letters_show_parser.add_argument("dead_letter_id")
    _add_json_argument(dead_letters_show_parser)
    dead_letters_show_parser.set_defaults(handler=event_dead_letter_show)
    dead_letters_resolve_parser = dead_letters_subparsers.add_parser(
        "resolve",
        help="Terminally resolve one dead letter",
    )
    dead_letters_resolve_parser.add_argument("dead_letter_id")
    _add_mutation_arguments(dead_letters_resolve_parser)
    dead_letters_resolve_parser.set_defaults(handler=event_dead_letter_resolve)
    dead_letters_requeue_parser = dead_letters_subparsers.add_parser(
        "requeue",
        help="Create an idempotent late-repair delivery generation",
    )
    dead_letters_requeue_parser.add_argument("dead_letter_id")
    dead_letters_requeue_parser.add_argument("--subscription-id", required=True)
    dead_letters_requeue_parser.add_argument(
        "--subscription-version",
        required=True,
        type=_positive_int,
    )
    _add_mutation_arguments(dead_letters_requeue_parser)
    dead_letters_requeue_parser.set_defaults(handler=event_dead_letter_requeue)

    consumer_status_parser = events_subparsers.add_parser(
        "consumer-status",
        help="Inspect consumer lag, pending work, and checkpoint status",
    )
    consumer_status_parser.add_argument("--subscription-id", required=True)
    consumer_status_parser.add_argument(
        "--subscription-version",
        required=True,
        type=_positive_int,
    )
    consumer_status_parser.add_argument("--stream-id", required=True)
    _add_json_argument(consumer_status_parser)
    consumer_status_parser.set_defaults(handler=event_consumer_status)

    projection_status_parser = events_subparsers.add_parser(
        "projection-status",
        help="Inspect one run event projection against durable history",
    )
    projection_status_parser.add_argument("run_id")
    _add_json_argument(projection_status_parser)
    projection_status_parser.set_defaults(handler=event_projection_status)


def _add_page_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cursor", help="Opaque cursor returned by the previous page")
    parser.add_argument("--limit", type=_page_limit, default=100)
    _add_json_argument(parser)


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )


def _add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reason", required=True, help="Auditable operator reason")
    parser.add_argument(
        "--yes",
        action="store_true",
        required=True,
        help="Confirm the requested mutation",
    )
    _add_json_argument(parser)


def event_quarantine_list(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.list_quarantine(
            reason=args.reason,
            disposition=args.disposition,
            cursor=args.cursor,
            limit=args.limit,
        ),
    )


def event_quarantine_show(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.get_quarantine(args.quarantine_id),
    )


def event_replay_reports_list(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.list_replay_reports(
            source_stream_id=args.source_stream_id,
            mode=args.mode,
            status=args.status,
            cursor=args.cursor,
            limit=args.limit,
        ),
    )


def event_replay_report_show(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.get_replay_report(args.replay_id),
    )


def event_runtime_projection_rebuild(args: argparse.Namespace) -> int:
    if not args.yes:
        return _confirmation_required()
    if (args.from_sequence is None) != (args.checkpoint_ref is None):
        print(
            "--from-sequence and --checkpoint-ref must be supplied together",
            file=sys.stderr,
        )
        return 1
    return _run_operator_command(
        args,
        lambda service: service.rebuild_runtime_projection(
            replay_id=args.replay_id,
            source_stream_id=args.source_stream_id,
            operator_reason=args.reason,
            from_sequence=args.from_sequence,
            checkpoint_ref=args.checkpoint_ref,
        ),
    )


def event_dead_letters_list(args: argparse.Namespace) -> int:
    if args.subscription_version is not None and args.subscription_id is None:
        print(
            "--subscription-version requires --subscription-id",
            file=sys.stderr,
        )
        return 1
    return _run_operator_command(
        args,
        lambda service: service.list_dead_letters(
            subscription_id=args.subscription_id,
            subscription_version=args.subscription_version,
            disposition=args.disposition,
            cursor=args.cursor,
            limit=args.limit,
        ),
    )


def event_dead_letter_show(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.get_dead_letter(args.dead_letter_id),
    )


def event_dead_letter_resolve(args: argparse.Namespace) -> int:
    if not args.yes:
        return _confirmation_required()
    return _run_operator_command(
        args,
        lambda service: service.resolve_dead_letter(
            args.dead_letter_id,
            operator_reason=args.reason,
        ),
    )


def event_dead_letter_requeue(args: argparse.Namespace) -> int:
    if not args.yes:
        return _confirmation_required()
    return _run_operator_command(
        args,
        lambda service: service.requeue_dead_letter(
            args.dead_letter_id,
            subscription_id=args.subscription_id,
            subscription_version=args.subscription_version,
            operator_reason=args.reason,
            idempotency_acknowledged=args.yes,
        ),
    )


def event_consumer_status(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.get_consumer_status(
            subscription_id=args.subscription_id,
            subscription_version=args.subscription_version,
            stream_id=args.stream_id,
        ),
    )


def event_projection_status(args: argparse.Namespace) -> int:
    return _run_operator_command(
        args,
        lambda service: service.get_projection_status(args.run_id),
    )


def _run_operator_command(
    args: argparse.Namespace,
    operation: Callable[[Any], Mapping[str, Any]],
) -> int:
    try:
        service = event_operator_service_from_env()
        raw_payload = operation(service)
        if not isinstance(raw_payload, Mapping):
            raise TypeError("event operator service returned an invalid payload")
        payload = dict(raw_payload)
        encoded_payload = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        )
        rendered = encoded_payload if args.json else _render_operator_text(payload)
    except Exception:
        # Composition and adapter failures can contain DSNs, local paths, raw
        # source fragments, or credentials. Keep this public boundary bounded.
        print("event operator command failed", file=sys.stderr)
        return 1
    print(rendered)
    if payload.get("availability") == "unavailable" or payload.get("status") == "unavailable":
        return 2
    if payload.get("found") is False:
        return 1
    return 0


def _confirmation_required() -> int:
    print("event operator mutation requires --yes", file=sys.stderr)
    return 1


def _render_operator_text(payload: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for field in (
        "availability",
        "status",
        "tenant_id",
        "found",
        "run_id",
        "stream_id",
        "durable_high_watermark",
        "projection_high_watermark",
        "projection_event_count",
        "projection_checksum",
        "next_cursor",
        "unavailable_reason_class",
    ):
        if field in payload:
            lines.append(f"{field}={_format_text_value(payload[field])}")

    items = payload.get("items")
    if isinstance(items, list):
        lines.append(f"item_count={len(items)}")
        lines.extend(f"- {_summarize_operator_record(item)}" for item in items)

    for field in (
        "quarantine",
        "replay_report",
        "dead_letter",
        "delivery",
        "subscription",
        "stats",
        "checkpoint",
    ):
        value = payload.get(field)
        if isinstance(value, Mapping):
            summary = _summarize_operator_record(value, prefix=field)
            if summary:
                lines.append(summary)
    return "\n".join(lines) if lines else "status=ok"


def _summarize_operator_record(
    value: Any,
    *,
    prefix: str | None = None,
) -> str:
    if not isinstance(value, Mapping):
        return "record=invalid"
    fields: list[str] = []
    for field in (
        "quarantine_id",
        "replay_id",
        "dead_letter_id",
        "delivery_id",
        "event_id",
        "subscription_id",
        "subscription_version",
        "stream_id",
        "run_id",
        "status",
        "state",
        "disposition",
        "reason",
        "reason_class",
        "attempt_count",
        "pending_count",
        "lag",
        "oldest_pending_age_seconds",
        "late_repair_pending_count",
        "warning_threshold_reached",
        "capacity_remaining",
        "highest_contiguous_terminal_sequence",
        "last_event_id",
        "stream_sequence",
    ):
        field_value = value.get(field)
        if field_value is not None and not isinstance(field_value, (dict, list)):
            name = f"{prefix}.{field}" if prefix else field
            fields.append(f"{name}={_format_text_value(field_value)}")
    return " ".join(fields) if fields else (f"{prefix}=present" if prefix else "record=present")


def _format_text_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _page_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 1_000:
        raise argparse.ArgumentTypeError("must be at most 1000")
    return parsed


__all__ = [
    "event_consumer_status",
    "event_dead_letter_requeue",
    "event_dead_letter_resolve",
    "event_dead_letter_show",
    "event_dead_letters_list",
    "event_projection_status",
    "event_quarantine_list",
    "event_quarantine_show",
    "event_replay_report_show",
    "event_replay_reports_list",
    "event_runtime_projection_rebuild",
    "register",
]
