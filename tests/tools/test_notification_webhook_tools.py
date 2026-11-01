"""notification.webhook and notification.slack against plain and TLS fault servers.

Every case is classified by what it means for a caller: ``applied`` (the
receiver took it), ``not_applied`` (definitely nothing happened, so a
corrected retry is safe) or ``indeterminate`` (it may have happened).
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

from framework.tool import ToolCall, ToolEffectNotAppliedError, ToolExecutor, ToolStatus
from framework.workers.approval import ApprovalDecision, InMemoryApprovalStore
from infrastructure.tools.notification_tools import (
    NOTIFICATION_TIMEOUT_MAX_SECONDS,
    NOTIFICATION_TOOL_TIMEOUT_SECONDS,
    GuardedWebhookSender,
)
from tests.tools.harness.fault_http import Delay, Drop, FaultHTTPServer, Hang, Redirect, Reset, Respond, Step, Truncate
from tests.tools.harness.invoke import ToolRun, assert_message_is_safe, run_tool, stability_policy
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.harness.tls import LoopbackTLS

Build = Callable[..., ToolEnvironment]
SECRET_PATH = "/services/T0SECRET/B0SECRET/xoxSECRETTOKEN"


def outcome(run: ToolRun) -> str:
    """What the caller learns: applied, not_applied or indeterminate."""

    result = run.observation.result
    if run.status is ToolStatus.SUCCEEDED:
        return "applied"
    assert_message_is_safe(run.error_message)
    return "indeterminate" if result.indeterminate else "not_applied"


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _webhook(environment: ToolEnvironment, url: str, **extra: Any) -> ToolRun:
    return run_tool(environment.registry, "notification.webhook", {"url": url, "payload": {"event": "brief.published"}, **extra})


@pytest.mark.tool_scenario("notification.webhook", "A")
@pytest.mark.parametrize(
    "args",
    [
        {"url": "ftp://127.0.0.1/hook", "payload": {}},
        {"url": "javascript:alert(1)", "payload": {}},
        {"url": "", "payload": {}},
        {"url": "https://evil.example/hook", "payload": {}},
        {"url": "//127.0.0.1/no-scheme", "payload": {}},
        {"payload": ["not", "an", "object"]},
        {"payload": {"score": float("nan")}},
        {"payload": {}, "headers": {"Host": "internal.example"}},
        {"payload": {}, "headers": {"Authorization": "Bearer x"}},
        {"payload": {}, "headers": {"Transfer-Encoding": "chunked"}},
        {"payload": {}, "headers": {"X-Trace": "a\r\nX-Injected: 1"}},
        {"payload": {}, "headers": {"Bad Name": "x"}},
        {"payload": {}, "timeout_seconds": "soon"},
        {"payload": {}, "timeout_seconds": float("inf")},
    ],
    ids=[
        "ftp",
        "javascript",
        "empty-url",
        "unlisted-host",
        "no-scheme",
        "payload-not-object",
        "nan-payload",
        "host-header",
        "auth-header",
        "transfer-encoding",
        "crlf-header",
        "bad-header-name",
        "timeout-text",
        "timeout-infinite",
    ],
)
def test_unusable_arguments_send_nothing(tool_env: ToolEnvironment, fault_http: FaultHTTPServer, args: dict) -> None:
    fault_http.route("/hook", Respond(200, "ok"))
    arguments = {"url": fault_http.url("/hook"), **args}

    run = run_tool(tool_env.registry, "notification.webhook", arguments)

    assert outcome(run) == "not_applied", run.describe()
    assert fault_http.requests == []


@pytest.mark.tool_scenario("notification.webhook", "B")
@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (Respond(200, "ok"), "applied"),
        (Respond(204, ""), "applied"),
        (Respond(400, "bad payload"), "not_applied"),
        (Respond(404, "no such hook"), "not_applied"),
        (Respond(410, "hook deleted"), "not_applied"),
        (Respond(409, "conflict"), "indeterminate"),
        (Respond(429, "slow down", headers={"Retry-After": "30"}), "indeterminate"),
        (Respond(500, "boom"), "indeterminate"),
        (Respond(503, "busy"), "indeterminate"),
        (Reset(), "indeterminate"),
        (Drop(), "indeterminate"),
        (Truncate("x" * 100, sent_bytes=10), "applied"),
    ],
    ids=["200", "204", "400", "404", "410", "409", "429", "500", "503", "reset", "dropped", "truncated-2xx"],
)
def test_receiver_answers_map_to_what_the_caller_can_rely_on(
    tool_env: ToolEnvironment, fault_http: FaultHTTPServer, step: Step, expected: str
) -> None:
    fault_http.route("/hook", step)

    run = _webhook(tool_env, fault_http.url("/hook"))

    assert outcome(run) == expected, run.describe()
    assert len(fault_http.requests_for("/hook")) == 1


@pytest.mark.tool_scenario("notification.webhook", "B")
def test_refused_connection_sends_nothing(tool_env: ToolEnvironment) -> None:
    run = _webhook(tool_env, f"http://127.0.0.1:{_dead_port()}/hook")
    assert outcome(run) == "not_applied", run.describe()


@pytest.mark.tool_scenario("notification.webhook", "B")
def test_rate_limit_reports_retry_after(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(429, "slow down", headers={"Retry-After": "30"}))
    run = _webhook(tool_env, fault_http.url("/hook"))
    assert "Retry-After: 30" in run.error_message


@pytest.mark.tool_scenario("notification.webhook", "C")
def test_hanging_receiver_ends_on_the_requested_timeout(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Hang())

    run = _webhook(tool_env, fault_http.url("/hook"), timeout_seconds=0.5)

    assert outcome(run) == "indeterminate", run.describe()
    assert run.elapsed_seconds < 1.5


@pytest.mark.tool_scenario("notification.webhook", "C")
def test_slow_receiver_inside_the_timeout_is_delivered(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Delay(0.3, Respond(200, "ok")))
    assert outcome(_webhook(tool_env, fault_http.url("/hook"), timeout_seconds=2)) == "applied"


@pytest.mark.tool_scenario("notification.webhook", "C")
def test_tool_timeout_leaves_room_for_the_longest_allowed_request(tool_env: ToolEnvironment) -> None:
    definition = tool_env.registry.require("notification.webhook").definition
    assert definition.timeout_seconds == NOTIFICATION_TOOL_TIMEOUT_SECONDS > NOTIFICATION_TIMEOUT_MAX_SECONDS


@pytest.mark.tool_scenario("notification.webhook", "D")
def test_huge_response_body_is_previewed_not_returned(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(200, "y" * 3_000_000))

    run = _webhook(tool_env, fault_http.url("/hook"))

    assert outcome(run) == "applied"
    assert len(run.output["response_preview"]) == 500
    assert run.elapsed_seconds < 5.0


@pytest.mark.tool_scenario("notification.webhook", "D")
def test_large_payload_is_sent_whole(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(200, "ok"))
    payload = {"items": [{"id": index, "text": "z" * 200} for index in range(2000)]}

    run = run_tool(tool_env.registry, "notification.webhook", {"url": fault_http.url("/hook"), "payload": payload})

    assert outcome(run) == "applied"
    assert len(fault_http.requests_for("/hook")[0].body) == run.output["payload_bytes"] > 400_000


@pytest.mark.tool_scenario("notification.webhook", "F")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_never_followed(tool_env: ToolEnvironment, fault_http: FaultHTTPServer, status: int) -> None:
    with FaultHTTPServer() as elsewhere:
        elsewhere.route("/admin/delete", Respond(200, "done"))
        fault_http.route("/hook", Redirect(elsewhere.url("/admin/delete"), status=status))

        run = _webhook(tool_env, fault_http.url("/hook"), headers={"X-Trace": "t-1"})

        assert outcome(run) == "not_applied", run.describe()
        assert "redirect" in run.error_message
        assert elsewhere.requests == []
        assert len(fault_http.requests_for("/hook")) == 1


@pytest.mark.tool_scenario("notification.webhook", "F")
def test_possibly_applied_send_is_not_retried(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(502, "bad gateway"), Respond(200, "ok"))

    assert outcome(_webhook(tool_env, fault_http.url("/hook"))) == "indeterminate"
    assert len(fault_http.requests_for("/hook")) == 1


@pytest.mark.tool_scenario("notification.webhook", "F")
def test_an_approval_releases_exactly_one_send(tool_env: ToolEnvironment, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(200, "ok"))
    store = InMemoryApprovalStore()
    executor = ToolExecutor(tool_env.registry, run_id="run-approval", approval_store=store)
    policy = stability_policy(tool_env.registry, require_approval_for_side_effects=True)

    def call(call_id: str, approval_id: str | None = None) -> Any:
        return executor.execute(
            ToolCall(
                tool_name="notification.webhook",
                arguments={"url": fault_http.url("/hook"), "payload": {"event": "x"}},
                requested_by_agent_id="agent-1",
                call_id=call_id,
                approval_id=approval_id,
            ),
            policy,
        )

    pending = call("call-1")
    assert pending.status is ToolStatus.APPROVAL_REQUIRED
    assert fault_http.requests == []
    approval_id = pending.result.approval_id
    store.record_decision(approval_id, decision=ApprovalDecision(decision_type="approve", decided_by="reviewer"))

    assert call("call-1:approved", approval_id).status is ToolStatus.SUCCEEDED
    assert call("call-1:replayed", approval_id).status is ToolStatus.DENIED
    assert len(fault_http.requests_for("/hook")) == 1


@pytest.mark.tool_scenario("notification.webhook", "G")
def test_internal_addresses_need_an_explicit_private_network(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    fault_http.route("/hook", Respond(200, "ok"))
    environment = tool_env_factory(
        notification_options={"allowed_webhook_domains": ["127.0.0.1"], "allowed_private_networks": []}
    )

    run = _webhook(environment, fault_http.url("/hook"))

    assert outcome(run) == "not_applied"
    assert "non-public" in run.error_message
    assert fault_http.requests == []


@pytest.mark.tool_scenario("notification.webhook", "G")
@pytest.mark.parametrize("host", ["hooks.example.com.evil.example", "evilhooks.example.com", "hooks.example.co"])
def test_lookalike_hosts_are_refused(tool_env: ToolEnvironment, host: str) -> None:
    run = _webhook(tool_env, f"https://{host}/hook")
    assert outcome(run) == "not_applied"
    assert "allowed domains" in run.error_message


@pytest.mark.tool_scenario("notification.webhook", "G")
def test_webhook_secrets_in_the_url_path_never_reach_error_text(
    tool_env: ToolEnvironment, fault_http: FaultHTTPServer
) -> None:
    fault_http.route(SECRET_PATH, Redirect("http://169.254.169.254/latest/"))

    run = _webhook(tool_env, fault_http.url(SECRET_PATH))

    assert outcome(run) == "not_applied"
    assert "SECRET" not in run.error_message
    assert "169.254.169.254" in run.error_message


@pytest.mark.tool_scenario("notification.webhook", "G")
def test_sender_without_allowed_domains_fails_closed() -> None:
    sender = GuardedWebhookSender(allowed_domains=())
    with pytest.raises(ToolEffectNotAppliedError, match="no allowed notification domains"):
        sender("https://example.com/hook", b"{}", {}, 1.0)


def _slack_env(build: Build, server: FaultHTTPServer, tls: LoopbackTLS, *, path: str = SECRET_PATH) -> ToolEnvironment:
    sender = GuardedWebhookSender(
        allowed_domains=("127.0.0.1",),
        allowed_private_networks=("127.0.0.0/8",),
        require_https=True,
        ssl_context=tls.client_context(),
    )
    return build(
        notification_options={
            "slack_webhook_url": server.url(path),
            "allowed_slack_domains": ["127.0.0.1"],
            "slack_sender": sender,
        }
    )


def _slack(environment: ToolEnvironment, **args: Any) -> ToolRun:
    return run_tool(environment.registry, "notification.slack", {"text": "Brief published", **args})


@pytest.mark.tool_scenario("notification.slack", "A")
@pytest.mark.parametrize(
    "args",
    [{"text": ""}, {"text": None, "blocks": "not-a-list"}, {"text": None, "blocks": [1, 2]}, {"timeout_seconds": "x"}],
    ids=["empty-text", "blocks-string", "blocks-not-objects", "timeout-text"],
)
def test_slack_unusable_arguments_send_nothing(
    tool_env_factory: Build, fault_https: FaultHTTPServer, loopback_tls: LoopbackTLS, args: dict
) -> None:
    environment = _slack_env(tool_env_factory, fault_https, loopback_tls)
    arguments = {key: value for key, value in {"text": "Brief published", **args}.items() if value is not None}

    assert outcome(run_tool(environment.registry, "notification.slack", arguments)) == "not_applied"
    assert fault_https.requests == []


@pytest.mark.tool_scenario("notification.slack", "B")
@pytest.mark.parametrize(
    ("step", "expected"),
    [(Respond(200, "ok"), "applied"), (Respond(404, "no_service"), "not_applied"), (Respond(500, "boom"), "indeterminate")],
    ids=["200", "404", "500"],
)
def test_slack_over_https_maps_receiver_answers(
    tool_env_factory: Build, fault_https: FaultHTTPServer, loopback_tls: LoopbackTLS, step: Step, expected: str
) -> None:
    fault_https.route(SECRET_PATH, step)
    run = _slack(_slack_env(tool_env_factory, fault_https, loopback_tls))
    assert outcome(run) == expected, run.describe()
    assert len(fault_https.requests_for(SECRET_PATH)) == 1


@pytest.mark.tool_scenario("notification.slack", "B")
@pytest.mark.tool_scenario("notification.slack", "G")
def test_untrusted_certificate_sends_nothing(tool_env_factory: Build, fault_https: FaultHTTPServer) -> None:
    fault_https.route(SECRET_PATH, Respond(200, "ok"))
    sender = GuardedWebhookSender(
        allowed_domains=("127.0.0.1",), allowed_private_networks=("127.0.0.0/8",), require_https=True
    )
    environment = tool_env_factory(
        notification_options={
            "slack_webhook_url": fault_https.url(SECRET_PATH),
            "allowed_slack_domains": ["127.0.0.1"],
            "slack_sender": sender,
        }
    )

    run = _slack(environment)

    assert outcome(run) == "not_applied"
    assert "CERTIFICATE_VERIFY_FAILED" in run.error_message
    assert fault_https.requests == []
    assert "SECRET" not in run.error_message


@pytest.mark.tool_scenario("notification.slack", "C")
def test_slack_hang_ends_on_the_timeout(
    tool_env_factory: Build, fault_https: FaultHTTPServer, loopback_tls: LoopbackTLS
) -> None:
    fault_https.route(SECRET_PATH, Hang())
    run = _slack(_slack_env(tool_env_factory, fault_https, loopback_tls), timeout_seconds=0.5)
    assert outcome(run) == "indeterminate"
    assert run.elapsed_seconds < 1.5


@pytest.mark.tool_scenario("notification.slack", "D")
def test_slack_huge_reply_is_previewed(
    tool_env_factory: Build, fault_https: FaultHTTPServer, loopback_tls: LoopbackTLS
) -> None:
    fault_https.route(SECRET_PATH, Respond(200, "y" * 2_000_000))
    run = _slack(_slack_env(tool_env_factory, fault_https, loopback_tls))
    assert outcome(run) == "applied"
    assert len(run.output["response_preview"]) == 500


@pytest.mark.tool_scenario("notification.slack", "F")
def test_slack_redirect_is_never_followed(
    tool_env_factory: Build, fault_https: FaultHTTPServer, loopback_tls: LoopbackTLS
) -> None:
    fault_https.route(SECRET_PATH, Redirect(fault_https.url("/elsewhere"), status=307))
    fault_https.route("/elsewhere", Respond(200, "ok"))

    run = _slack(_slack_env(tool_env_factory, fault_https, loopback_tls))

    assert outcome(run) == "not_applied"
    assert fault_https.requests_for("/elsewhere") == []


@pytest.mark.tool_scenario("notification.slack", "G")
def test_slack_url_must_be_https(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(200, "ok"))
    environment = tool_env_factory(
        notification_options={"slack_webhook_url": fault_http.url("/hook"), "allowed_slack_domains": ["127.0.0.1"]}
    )

    run = _slack(environment)

    assert outcome(run) == "not_applied"
    assert "https" in run.error_message
    assert fault_http.requests == []
