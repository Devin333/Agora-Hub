"""notification.email against a fault SMTP server, and notification.rss_publish under contention."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from infrastructure.tools import notification_tools
from tests.tools.harness.fault_smtp import FaultSMTPServer, SMTPScript
from tests.tools.harness.invoke import expect_succeeded, run_tool
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.test_notification_webhook_tools import outcome

Build = Callable[..., ToolEnvironment]
StartSMTP = Callable[[SMTPScript], FaultSMTPServer]
REPO_ROOT = Path(__file__).resolve().parents[2]


def _mail_env(build: Build, server: FaultSMTPServer | None = None, *, port: int | None = None, **smtp: Any) -> ToolEnvironment:
    options: dict[str, Any] = {
        "smtp_host": "127.0.0.1",
        "smtp_port": port if port is not None else server.port,
        "smtp_use_starttls": False,
    }
    options.update(smtp)
    return build(notification_options=options)


def _send(environment: ToolEnvironment, **args: Any):
    arguments = {"to": ["editor@example.com"], "subject": "Daily brief", "text_body": "Today's brief.", **args}
    return run_tool(environment.registry, "notification.email", arguments)


@pytest.mark.tool_scenario("notification.email", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"to": []},
        {"to": "editor@example.com"},
        {"to": ["not-an-address"]},
        {"to": ["editor@example.com\r\nBcc: victim@example.com"]},
        {"to": ["editor@example.com, other@example.com"]},
        {"subject": ""},
        {"subject": "Brief\r\nBcc: victim@example.com"},
        {"text_body": None},
        {"reply_to": "a@example.com, b@example.com"},
        {"timeout_seconds": "soon"},
    ],
    ids=["no-recipient", "to-string", "bad-address", "crlf-recipient", "two-in-one", "empty-subject",
         "crlf-subject", "no-body", "two-reply-to", "timeout-text"],
)
def test_unusable_arguments_never_open_a_session(tool_env_factory: Build, fault_smtp: StartSMTP, args: dict) -> None:
    server = fault_smtp(SMTPScript())
    arguments = {"to": ["editor@example.com"], "subject": "Daily brief", "text_body": "Today's brief.", **args}
    arguments = {key: value for key, value in arguments.items() if value is not None}

    run = run_tool(_mail_env(tool_env_factory, server).registry, "notification.email", arguments)

    assert outcome(run) == "not_applied", run.describe()
    assert server.commands == []


@pytest.mark.tool_scenario("notification.email", "B")
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (SMTPScript(), "applied"),
        (SMTPScript(refuse_service=True), "not_applied"),
        (SMTPScript(drop_after_ehlo=True), "not_applied"),
        (SMTPScript(rejected_recipients=frozenset({"editor@example.com"})), "not_applied"),
        (SMTPScript(data_reply=(451, "try again later")), "not_applied"),
        (SMTPScript(data_reply=(554, "rejected as spam")), "not_applied"),
    ],
    ids=["accepted", "refused-service", "dropped-after-ehlo", "all-recipients-refused", "451-after-data", "554-after-data"],
)
def test_server_answers_map_to_what_the_caller_can_rely_on(
    tool_env_factory: Build, fault_smtp: StartSMTP, script: SMTPScript, expected: str
) -> None:
    server = fault_smtp(script)
    run = _send(_mail_env(tool_env_factory, server))
    assert outcome(run) == expected, run.describe()


@pytest.mark.tool_scenario("notification.email", "B")
def test_partially_refused_recipients_are_reported(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(rejected_recipients=frozenset({"gone@example.com"})))

    output = expect_succeeded(_send(_mail_env(tool_env_factory, server), to=["editor@example.com", "gone@example.com"]))

    assert output["accepted_count"] == 1
    assert output["refused_recipients"] == ["gone@example.com"]
    assert server.messages[0].recipients == ("editor@example.com",)


@pytest.mark.tool_scenario("notification.email", "B")
def test_unreachable_server_sends_nothing(tool_env_factory: Build) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    assert outcome(_send(_mail_env(tool_env_factory, port=port))) == "not_applied"


@pytest.mark.tool_scenario("notification.email", "C")
@pytest.mark.parametrize(
    ("stall_at", "expected"),
    [("greeting", "not_applied"), ("data_command", "not_applied"), ("data_end", "indeterminate")],
)
def test_stalls_end_on_the_timeout_and_say_whether_mail_may_be_queued(
    tool_env_factory: Build, fault_smtp: StartSMTP, stall_at: str, expected: str
) -> None:
    server = fault_smtp(SMTPScript(stall_at=stall_at))  # type: ignore[arg-type]

    run = _send(_mail_env(tool_env_factory, server), timeout_seconds=0.5)

    assert outcome(run) == expected, run.describe()
    assert run.elapsed_seconds < 1.5
    # data_end: the server holds the message without acknowledging it.
    assert len(server.messages) == (1 if stall_at == "data_end" else 0)


@pytest.mark.tool_scenario("notification.email", "C")
def test_slow_local_hostname_lookup_never_spends_the_send_budget(
    tool_env_factory: Build, fault_smtp: StartSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows with Docker or VPN adapters socket.getfqdn() can take about a
    # second; smtplib would run it inside every send's budget.
    lookups: list[float] = []

    def slow_getfqdn(name: str = "") -> str:
        lookups.append(time.monotonic())
        time.sleep(0.8)
        return "mailer.example.com"

    monkeypatch.setattr(socket, "getfqdn", slow_getfqdn)
    notification_tools.smtp_local_hostname.cache_clear()
    try:
        server = fault_smtp(SMTPScript())
        environment = _mail_env(tool_env_factory, server)

        runs = [_send(environment, timeout_seconds=0.5) for _ in range(2)]
    finally:
        notification_tools.smtp_local_hostname.cache_clear()

    for run in runs:
        assert outcome(run) == "applied", run.describe()
        assert run.elapsed_seconds < 0.5
    assert len(lookups) == 1
    assert "EHLO MAILER.EXAMPLE.COM" in [command.upper() for command in server.commands]


@pytest.mark.tool_scenario("notification.email", "C")
def test_tool_timeout_leaves_room_for_the_longest_allowed_send(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    definition = _mail_env(tool_env_factory, fault_smtp(SMTPScript())).registry.require("notification.email").definition
    assert definition.timeout_seconds == notification_tools.NOTIFICATION_TOOL_TIMEOUT_SECONDS


@pytest.mark.tool_scenario("notification.email", "D")
def test_large_body_is_delivered_whole(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript())
    body = "line of the daily brief\n" * 80_000

    assert outcome(_send(_mail_env(tool_env_factory, server), text_body=body)) == "applied"
    assert len(server.messages[0].message.get_content()) >= len(body)


@pytest.mark.tool_scenario("notification.email", "F")
def test_one_call_is_one_message_even_when_the_outcome_is_unknown(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(stall_at="data_end"))

    _send(_mail_env(tool_env_factory, server), timeout_seconds=0.5)

    assert len(server.messages) == 1
    assert sum(command.upper().startswith("MAIL") for command in server.commands) == 1


@pytest.mark.tool_scenario("notification.email", "G")
def test_starttls_required_but_unavailable_sends_nothing(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(credentials=("bot", "s3cret-pass")))

    run = _send(_mail_env(tool_env_factory, server, smtp_use_starttls=True, smtp_username="bot", smtp_password="s3cret-pass"))

    assert outcome(run) == "not_applied"
    assert not any(command.upper().startswith("AUTH") for command in server.commands)
    assert "s3cret-pass" not in run.error_message


@pytest.mark.tool_scenario("notification.email", "G")
def test_bcc_is_delivered_but_never_written_into_the_message(tool_env_factory: Build, fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript())

    expect_succeeded(_send(_mail_env(tool_env_factory, server), bcc=["hidden@example.com"]))

    [received] = server.messages
    assert "hidden@example.com" in received.recipients
    assert b"hidden@example.com" not in received.data


@pytest.mark.tool_scenario("notification.email", "G")
@pytest.mark.parametrize("address", ["a@example.com.evil.example", "a@evilexample.com", "a@example.co"])
def test_recipients_outside_allowed_domains_are_refused(
    tool_env_factory: Build, fault_smtp: StartSMTP, address: str
) -> None:
    server = fault_smtp(SMTPScript())
    assert outcome(_send(_mail_env(tool_env_factory, server), to=[address])) == "not_applied"
    assert server.commands == []


def _publish(environment: ToolEnvironment, **args: Any):
    arguments = {"title": "Daily brief", "link": "https://example.com/briefs/1", **args}
    return run_tool(environment.registry, "notification.rss_publish", arguments)


def _items(environment: ToolEnvironment) -> list[ElementTree.Element]:
    return ElementTree.parse(environment.rss_feed_path).getroot().findall("./channel/item")


@pytest.mark.tool_scenario("notification.rss_publish", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"title": ""},
        {"link": "javascript:alert(1)"},
        {"link": "/relative/path"},
        {"title": "bell\x07 and null\x00"},
        {"description": "vertical\x0btab"},
        {"guid": "form\x0cfeed"},
        {"published_at": "not a date"},
        {"published_at": 1735689600},
    ],
    ids=["empty-title", "javascript-link", "relative-link", "control-title", "control-description",
         "control-guid", "bad-date", "numeric-date"],
)
def test_unusable_items_leave_the_feed_untouched(tool_env: ToolEnvironment, args: dict) -> None:
    expect_succeeded(_publish(tool_env, guid="first"))
    before = tool_env.rss_feed_path.read_bytes()

    assert outcome(_publish(tool_env, **args)) == "not_applied"
    assert tool_env.rss_feed_path.read_bytes() == before


@pytest.mark.tool_scenario("notification.rss_publish", "B")
@pytest.mark.parametrize(
    "existing",
    [b"<<< not xml", b"<?xml version='1.0'?><rss version='2.0'></rss>", b""],
    ids=["garbage", "no-channel", "empty-file"],
)
def test_a_broken_existing_feed_is_reported_and_kept(tool_env: ToolEnvironment, existing: bytes) -> None:
    tool_env.rss_feed_path.parent.mkdir(parents=True, exist_ok=True)
    tool_env.rss_feed_path.write_bytes(existing)

    run = _publish(tool_env)

    assert outcome(run) == "not_applied"
    assert "left unchanged" in run.error_message
    assert tool_env.rss_feed_path.read_bytes() == existing


@pytest.mark.tool_scenario("notification.rss_publish", "B")
def test_unwritable_feed_location_is_reported(tool_env: ToolEnvironment) -> None:
    tool_env.rss_feed_path.mkdir(parents=True)

    run = _publish(tool_env)

    assert outcome(run) == "not_applied"
    assert tool_env.rss_feed_path.is_dir()


@pytest.mark.tool_scenario("notification.rss_publish", "F")
def test_a_failed_write_leaves_the_previous_feed_whole(
    tool_env: ToolEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    expect_succeeded(_publish(tool_env, guid="first"))
    before = tool_env.rss_feed_path.read_bytes()

    def disk_full(path: Any, content: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(notification_tools, "write_bytes_atomically_unlocked", disk_full)
    run = _publish(tool_env, guid="second")

    assert outcome(run) == "not_applied"
    assert tool_env.rss_feed_path.read_bytes() == before


@pytest.mark.tool_scenario("notification.rss_publish", "F")
def test_publishing_the_same_guid_again_changes_nothing(tool_env: ToolEnvironment) -> None:
    first = expect_succeeded(_publish(tool_env, guid="brief-2026-01-01"))
    before = tool_env.rss_feed_path.read_bytes()
    again = expect_succeeded(_publish(tool_env, title="Edited title", guid="brief-2026-01-01"))

    assert first["published"] is True and again["already_published"] is True
    assert tool_env.rss_feed_path.read_bytes() == before


@pytest.mark.tool_scenario("notification.rss_publish", "G")
def test_markup_in_items_stays_text(tool_env: ToolEnvironment) -> None:
    title = "</title></item><item><title>forged</title>"
    expect_succeeded(_publish(tool_env, title=title, description="<script>alert(1)</script> & more"))

    [item] = _items(tool_env)
    assert item.findtext("title") == title
    assert item.findtext("description") == "<script>alert(1)</script> & more"


@pytest.mark.tool_scenario("notification.rss_publish", "E")
def test_concurrent_publishers_in_one_process_keep_every_item(tool_env: ToolEnvironment) -> None:
    def publish(index: int) -> None:
        expect_succeeded(_publish(tool_env, title=f"Item {index}", link=f"https://example.com/{index}"))

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(item.findtext("guid") for item in _items(tool_env)) == sorted(
        f"https://example.com/{index}" for index in range(40)
    )


_PUBLISHER_SCRIPT = """
import sys
from datetime import datetime, timezone
from infrastructure.tools.notification_tools import RssFeedPublisher

publisher = RssFeedPublisher(sys.argv[1], feed_title="t", feed_link="https://example.com/", feed_description="d")
for index in range(int(sys.argv[3])):
    publisher.publish(
        title=f"{sys.argv[2]}-{index}",
        link=f"https://example.com/{sys.argv[2]}/{index}",
        description=None,
        guid=None,
        published_at=datetime.now(timezone.utc),
    )
"""


@pytest.mark.tool_scenario("notification.rss_publish", "E")
def test_concurrent_publisher_processes_keep_every_item(tool_env: ToolEnvironment) -> None:
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _PUBLISHER_SCRIPT, str(tool_env.rss_feed_path), name, "15"],
            cwd=REPO_ROOT,
            env=environment,
        )
        for name in ("alpha", "beta")
    ]
    assert [worker.wait(timeout=120) for worker in workers] == [0, 0]

    guids = [item.findtext("guid") for item in _items(tool_env)]
    assert len(guids) == len(set(guids)) == 30


@pytest.mark.tool_scenario("notification.rss_publish", "D")
def test_feed_keeps_only_the_newest_items(tool_env_factory: Build) -> None:
    environment = tool_env_factory(notification_options={"rss_max_items": 5})
    for index in range(8):
        result = expect_succeeded(_publish(environment, title=f"Item {index}", link=f"https://example.com/{index}"))

    assert result["item_count"] == 5 and result["pruned_count"] == 1
    assert [item.findtext("title") for item in _items(environment)] == [f"Item {index}" for index in range(3, 8)]


@pytest.mark.tool_scenario("notification.rss_publish", "D")
def test_large_description_is_stored_and_the_feed_stays_valid(tool_env: ToolEnvironment) -> None:
    description = "paragraph of the brief. " * 10_000
    expect_succeeded(_publish(tool_env, description=description))
    assert _items(tool_env)[0].findtext("description") == description.strip()
