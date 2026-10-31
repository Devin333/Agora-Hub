from __future__ import annotations

import json
import math
import re
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime, timezone as _tz
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
from xml.etree import ElementTree

from framework.tool.models import ToolDefinition
from framework.tool.registry import ToolRegistry
from framework.tool.runtime.errors import ToolEffectNotAppliedError, effect_not_applied_on
from infrastructure.external.egress import (
    EgressConnectError,
    EgressDenied,
    EgressPolicy,
    TooManyRedirectsError,
    display_origin,
    open_guarded,
)
from infrastructure.external.fetch_deadline import (
    FetchDeadlineExceeded,
    SocketWatchdog,
    bounded_timeout,
    fetch_deadline,
)
from infrastructure.storage.json_file_store import locked_file, write_bytes_atomically_unlocked

UTC = _tz.utc

WebhookSender = Callable[[str, bytes, dict[str, str], float], dict[str, Any]]
EmailSender = Callable[[EmailMessage, list[str], float], dict[str, Any]]

# Model-supplied notification timeouts are clamped to this range; each tool
# declares one more second so its own deadline always fires first.
NOTIFICATION_TIMEOUT_DEFAULT_SECONDS = 10.0
NOTIFICATION_TIMEOUT_MAX_SECONDS = 30.0
NOTIFICATION_TOOL_TIMEOUT_SECONDS = NOTIFICATION_TIMEOUT_MAX_SECONDS + 1.0
RSS_PUBLISH_TIMEOUT_SECONDS = 10.0
DEFAULT_RSS_MAX_ITEMS = 500
RESPONSE_PREVIEW_BYTES = 10_000

_SECRET_HEADER_NAMES = {"authorization", "cookie", "x-api-key", "api-key"}
# Framing and routing headers belong to the transport; letting a model set
# them would allow request smuggling or virtual-host confusion.
_TRANSPORT_HEADER_NAMES = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
}
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Status codes after which the receiver may have acted on the request anyway.
_AMBIGUOUS_CLIENT_STATUSES = {408, 409, 425, 429}
# XML 1.0 Char production: anything outside it makes the whole feed unparseable.
_XML_INVALID_CHARACTER_RE = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


class WebhookDeliveryError(RuntimeError):
    """The request reached the receiver and failed; it may still have been applied."""


def register_notification_tools(
    registry: ToolRegistry,
    *,
    allowed_webhook_domains: list[str] | None = None,
    allowed_private_networks: list[str] | None = None,
    webhook_sender: WebhookSender | None = None,
    allowed_slack_domains: list[str] | None = None,
    slack_webhook_url: str | None = None,
    slack_sender: WebhookSender | None = None,
    allowed_email_domains: list[str] | None = None,
    email_sender: EmailSender | None = None,
    email_from_address: str | None = None,
    smtp_host: str | None = None,
    smtp_port: int = 587,
    smtp_username: str | None = None,
    smtp_password: str | None = None,
    smtp_use_tls: bool = False,
    smtp_use_starttls: bool = True,
    rss_feed_path: str | Path | None = None,
    rss_feed_title: str = "Framework Updates",
    rss_feed_link: str = "https://localhost/",
    rss_feed_description: str = "Framework published updates",
    rss_max_items: int = DEFAULT_RSS_MAX_ITEMS,
) -> None:
    """Register the notification tools.

    ``allowed_private_networks`` opens non-public webhook destinations (for an
    intranet chat server, for example); everything else must be public.
    """

    allowed_domain_tuple = _allowed_domains(allowed_webhook_domains)
    private_networks = tuple(allowed_private_networks or ())
    sender = webhook_sender or GuardedWebhookSender(
        allowed_domains=allowed_domain_tuple,
        allowed_private_networks=private_networks,
    )
    registry.register(
        ToolDefinition(
            name="notification.webhook",
            description="Send a JSON webhook notification to an allowed HTTP(S) endpoint.",
            input_schema={
                "required": ["url", "payload"],
                "properties": {
                    "url": {"type": "string"},
                    "payload": {"type": "object"},
                    "headers": {"type": "object"},
                    "timeout_seconds": {"type": "number"},
                },
                "additionalProperties": False,
            },
            side_effect="writes_external_state",
            requires_approval=True,
            concurrency_safe=False,
            timeout_seconds=NOTIFICATION_TOOL_TIMEOUT_SECONDS,
            max_result_bytes=100_000,
            metadata={"notification_channel": "webhook"},
        ),
        lambda args: _send_webhook(args, allowed_domains=allowed_domain_tuple, sender=sender),
    )
    if slack_webhook_url is not None:
        slack_url = str(slack_webhook_url).strip()
        slack_domain_tuple = _allowed_domains(allowed_slack_domains or ["hooks.slack.com"])
        slack_sender_fn = slack_sender or webhook_sender or GuardedWebhookSender(
            allowed_domains=slack_domain_tuple,
            allowed_private_networks=private_networks,
            require_https=True,
        )
        registry.register(
            ToolDefinition(
                name="notification.slack",
                description="Send a Slack incoming webhook notification through the configured URL.",
                input_schema={
                    "required": [],
                    "properties": {
                        "text": {"type": "string"},
                        "blocks": {"type": "array"},
                        "attachments": {"type": "array"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
                side_effect="writes_external_state",
                requires_approval=True,
                concurrency_safe=False,
                timeout_seconds=NOTIFICATION_TOOL_TIMEOUT_SECONDS,
                max_result_bytes=100_000,
                metadata={"notification_channel": "slack"},
            ),
            lambda args: _send_slack(
                args,
                webhook_url=slack_url,
                allowed_domains=slack_domain_tuple,
                sender=slack_sender_fn,
            ),
        )
    if email_sender is not None or smtp_host is not None or email_from_address is not None:
        from_address = _single_email_address(email_from_address, "email_from_address")
        sender_fn = email_sender or SmtpEmailSender(
            host=_required_text(smtp_host, "smtp_host"),
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            use_tls=smtp_use_tls,
            use_starttls=smtp_use_starttls,
        )
        registry.register(
            ToolDefinition(
                name="notification.email",
                description="Send an email notification through the configured SMTP sender.",
                input_schema={
                    "required": ["to", "subject"],
                    "properties": {
                        "to": {"type": "array"},
                        "cc": {"type": "array"},
                        "bcc": {"type": "array"},
                        "subject": {"type": "string"},
                        "text_body": {"type": "string"},
                        "html_body": {"type": "string"},
                        "reply_to": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
                side_effect="writes_external_state",
                requires_approval=True,
                concurrency_safe=False,
                timeout_seconds=NOTIFICATION_TOOL_TIMEOUT_SECONDS,
                max_result_bytes=100_000,
                metadata={"notification_channel": "email"},
            ),
            lambda args: _send_email(
                args,
                from_address=from_address,
                allowed_domains=_allowed_domains(allowed_email_domains),
                sender=sender_fn,
            ),
        )
    if rss_feed_path is not None:
        rss_publisher = RssFeedPublisher(
            rss_feed_path,
            feed_title=rss_feed_title,
            feed_link=rss_feed_link,
            feed_description=rss_feed_description,
            max_items=rss_max_items,
        )
        registry.register(
            ToolDefinition(
                name="notification.rss_publish",
                description=(
                    "Publish an item to the configured RSS feed file. Publishing a guid "
                    "that is already in the feed changes nothing."
                ),
                input_schema={
                    "required": ["title", "link"],
                    "properties": {
                        "title": {"type": "string"},
                        "link": {"type": "string"},
                        "description": {"type": "string"},
                        "guid": {"type": "string"},
                        "published_at": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                side_effect="publishing",
                requires_approval=True,
                concurrency_safe=False,
                timeout_seconds=RSS_PUBLISH_TIMEOUT_SECONDS,
                max_result_bytes=100_000,
                metadata={"notification_channel": "rss"},
            ),
            lambda args: _publish_rss(args, rss_publisher=rss_publisher),
        )


class GuardedWebhookSender:
    """POST a notification through the egress guard, never following a redirect.

    A redirected notification would be re-sent (or silently turned into a GET)
    against a destination nobody approved, so any 3xx is a refusal. Failures
    before a connection exists, redirects and definite 4xx rejections raise
    ``ToolEffectNotAppliedError``; anything after the request may have been
    processed raises ``WebhookDeliveryError`` and stays indeterminate.
    """

    def __init__(
        self,
        *,
        allowed_domains: tuple[str, ...],
        allowed_private_networks: tuple[str, ...] = (),
        require_https: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._allowed_domains = tuple(allowed_domains)
        self._policy = EgressPolicy(
            allowed_domains=self._allowed_domains,
            allowed_private_networks=tuple(allowed_private_networks),
            max_redirects=0,
        )
        self._require_https = require_https
        self._ssl_context = ssl_context

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
        if not self._allowed_domains:
            # An empty egress allowlist means "any public host"; notifications fail closed.
            raise ToolEffectNotAppliedError("no allowed notification domains are configured")
        if self._require_https and urlsplit(url).scheme.casefold() != "https":
            raise ToolEffectNotAppliedError("this notification endpoint must use https")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "FrameworkToolRuntime/1.0", **headers},
            method="POST",
        )
        try:
            response = open_guarded(
                request,
                self._policy,
                timeout_seconds=timeout_seconds,
                ssl_context=self._ssl_context,
            )
        except (EgressDenied, EgressConnectError) as exc:
            raise ToolEffectNotAppliedError(f"notification was not sent: {exc}") from exc
        except FetchDeadlineExceeded as exc:
            if exc.before_send:
                raise ToolEffectNotAppliedError(f"notification was not sent: {exc}") from exc
            raise
        except TooManyRedirectsError as exc:
            raise ToolEffectNotAppliedError(
                f"notification endpoint answered with a redirect to {display_origin(exc.url)}; "
                "notifications are never re-sent to a redirect target"
            ) from exc
        except HTTPError as exc:
            raise _http_failure(exc) from exc
        except URLError as exc:
            raise WebhookDeliveryError(f"notification delivery failed after the request was sent: {exc.reason}") from exc
        with response:
            try:
                preview = response.read(RESPONSE_PREVIEW_BYTES)
            except (OSError, ValueError, HTTPException):
                # The receiver already answered 2xx; an unreadable or cut-off
                # body does not undo that.
                preview = b""
            response_headers = getattr(response, "headers", None)
            return {
                "status_code": getattr(response, "status", None),
                "content_type": response_headers.get_content_type() if response_headers is not None else None,
                "response_text": preview.decode("utf-8", errors="replace"),
            }


def _http_failure(exc: HTTPError) -> Exception:
    try:
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        detail = ""
    finally:
        exc.close()
    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
    message = f"notification endpoint answered HTTP {exc.code}"
    if retry_after:
        message += f" (Retry-After: {retry_after})"
    if detail:
        message += f": {detail[:200]}"
    if 300 <= exc.code < 400 or (400 <= exc.code < 500 and exc.code not in _AMBIGUOUS_CLIENT_STATUSES):
        return ToolEffectNotAppliedError(message)
    return WebhookDeliveryError(message)


class _GuardedSMTPSession:
    """smtplib hooks that bound the whole session and record whether DATA began."""

    _watchdog: SocketWatchdog | None = None
    data_started = False

    def _get_socket(self, host: Any, port: Any, timeout: Any) -> Any:
        sock = super()._get_socket(host, port, timeout)  # type: ignore[misc]
        self._watchdog = SocketWatchdog.for_current_deadline()
        if self._watchdog is not None:
            self._watchdog.watch(sock)
        return sock

    def starttls(self, *args: Any, **kwargs: Any) -> Any:
        reply = super().starttls(*args, **kwargs)  # type: ignore[misc]
        if self._watchdog is not None:
            self._watchdog.watch(self.sock)  # type: ignore[attr-defined]
        return reply

    def data(self, msg: Any) -> Any:
        # smtplib.SMTP.data, split so the moment the server says 354 is
        # recorded: before it, nothing of the message has been sent.
        session: Any = self
        session.putcmd("data")
        code, reply = session.getreply()
        if code != 354:
            raise smtplib.SMTPDataError(code, reply)
        self.data_started = True
        if isinstance(msg, str):
            msg = smtplib._fix_eols(msg).encode("ascii")
        quoted = smtplib._quote_periods(msg)
        if quoted[-2:] != smtplib.bCRLF:
            quoted = quoted + smtplib.bCRLF
        session.send(quoted + b"." + smtplib.bCRLF)
        return session.getreply()

    def release_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()

    def raise_if_spent(self, error: BaseException) -> None:
        if self._watchdog is not None:
            self._watchdog.raise_if_spent(error)


class _GuardedSMTP(_GuardedSMTPSession, smtplib.SMTP):
    pass


class _GuardedSMTPSSL(_GuardedSMTPSession, smtplib.SMTP_SSL):
    pass


# Failures that prove the server did not accept the message.
_SMTP_NOT_SENT_ERRORS = (
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPDataError,
)


class SmtpEmailSender:
    """Send one message within one time budget.

    Before DATA starts, every failure means the server has not accepted the
    message (``ToolEffectNotAppliedError``). After DATA starts, only an
    explicit rejection is definite; a timeout or dropped connection may follow
    a message the server already queued, so it stays indeterminate.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = False,
        use_starttls: bool = True,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.host = _required_text(host, "smtp_host")
        self.port = int(port)
        if self.port <= 0:
            raise ValueError("smtp_port must be positive")
        if use_tls and use_starttls:
            raise ValueError("smtp_use_tls and smtp_use_starttls cannot both be true")
        if (username is None) != (password is None):
            raise ValueError("smtp_username and smtp_password must be configured together")
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_starttls = use_starttls
        self._ssl_context = ssl_context

    def __call__(self, message: EmailMessage, recipients: list[str], timeout_seconds: float) -> dict[str, Any]:
        context = self._ssl_context or ssl.create_default_context()
        with fetch_deadline(timeout_seconds):
            session: _GuardedSMTP | _GuardedSMTPSSL = (
                _GuardedSMTPSSL(context=context) if self.use_tls else _GuardedSMTP()
            )
            session.timeout = bounded_timeout(timeout_seconds)
            try:
                session.connect(self.host, self.port)
                if self.use_starttls:
                    session.starttls(context=context)
                if self.username is not None and self.password is not None:
                    session.login(self.username, self.password)
                refused = session.send_message(message, to_addrs=recipients)
            except (smtplib.SMTPException, OSError) as exc:
                session.close()
                raise _smtp_failure(session, exc) from exc
            finally:
                session.release_watchdog()
            try:
                session.quit()
            except (smtplib.SMTPException, OSError):
                # The message was accepted; a failed goodbye does not undo that.
                session.close()
        refused_recipients = sorted(str(recipient) for recipient in refused)
        accepted = [recipient for recipient in recipients if recipient not in refused]
        return {"accepted_recipients": accepted, "refused_recipients": refused_recipients}


def _smtp_failure(session: _GuardedSMTPSession, exc: BaseException) -> BaseException:
    try:
        session.raise_if_spent(exc)
    except FetchDeadlineExceeded as deadline:
        if not session.data_started:
            return ToolEffectNotAppliedError(f"email was not sent: {deadline}")
        return deadline
    if not session.data_started or isinstance(exc, _SMTP_NOT_SENT_ERRORS):
        return ToolEffectNotAppliedError(f"email was not sent: {_smtp_error_text(exc)}")
    return exc


def _smtp_error_text(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"every recipient was refused ({len(exc.recipients)})"
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"SMTP {exc.smtp_code} {detail}".strip()
    return f"{type(exc).__name__}: {exc}"


class RssFeedPublisher:
    """Append items to an RSS file safely under concurrent publishers.

    The read-modify-write happens under ``locked_file`` (threads and
    processes) and the new feed replaces the old one atomically, so a crash or
    a racing publisher can neither lose items nor leave a half-written file.
    A guid already in the feed is a no-op, which makes a repeated publish
    harmless. Only the newest ``max_items`` are kept, bounding the file that
    every publish must parse and rewrite.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        feed_title: str,
        feed_link: str,
        feed_description: str,
        max_items: int = DEFAULT_RSS_MAX_ITEMS,
    ) -> None:
        if type(max_items) is not int or max_items < 1:
            raise ValueError("rss_max_items must be a positive integer")
        self.path = Path(path)
        self.feed_title = feed_title
        self.feed_link = feed_link
        self.feed_description = feed_description
        self.max_items = max_items

    def publish(
        self,
        *,
        title: str,
        link: str,
        description: str | None,
        guid: str | None,
        published_at: datetime,
    ) -> dict[str, Any]:
        try:
            return self._publish(
                title=title,
                link=link,
                description=description,
                guid=guid,
                published_at=published_at,
            )
        except OSError as exc:
            # The feed is replaced atomically, so a failed read, lock or write
            # always leaves the previous feed in place.
            raise ToolEffectNotAppliedError(
                f"the RSS feed could not be updated ({type(exc).__name__}); it was left unchanged"
            ) from exc

    def _publish(
        self,
        *,
        title: str,
        link: str,
        description: str | None,
        guid: str | None,
        published_at: datetime,
    ) -> dict[str, Any]:
        item_guid = guid or link
        with locked_file(self.path):
            root = self._load_or_create()
            channel = root.find("channel")
            if channel is None:
                raise ToolEffectNotAppliedError("the existing RSS feed has no channel; it was left unchanged")
            items = channel.findall("item")
            if any((existing.findtext("guid") or "") == item_guid for existing in items):
                return {"item_count": len(items), "already_published": True, "pruned_count": 0}
            item = ElementTree.SubElement(channel, "item")
            ElementTree.SubElement(item, "title").text = title
            ElementTree.SubElement(item, "link").text = link
            if description:
                ElementTree.SubElement(item, "description").text = description
            ElementTree.SubElement(item, "guid").text = item_guid
            ElementTree.SubElement(item, "pubDate").text = format_datetime(published_at, usegmt=True)
            items = channel.findall("item")
            pruned = items[: max(0, len(items) - self.max_items)]
            for old in pruned:
                channel.remove(old)
            write_bytes_atomically_unlocked(
                self.path,
                ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
            )
            return {
                "item_count": len(items) - len(pruned),
                "already_published": False,
                "pruned_count": len(pruned),
            }

    def _load_or_create(self) -> ElementTree.Element:
        if self.path.exists():
            try:
                return ElementTree.parse(self.path).getroot()
            except ElementTree.ParseError as exc:
                raise ToolEffectNotAppliedError(
                    "the existing RSS feed is not valid XML; it was left unchanged"
                ) from exc
        root = ElementTree.Element("rss", {"version": "2.0"})
        channel = ElementTree.SubElement(root, "channel")
        ElementTree.SubElement(channel, "title").text = self.feed_title
        ElementTree.SubElement(channel, "link").text = self.feed_link
        ElementTree.SubElement(channel, "description").text = self.feed_description
        return root


def _send_webhook(args: dict[str, Any], *, allowed_domains: tuple[str, ...], sender: WebhookSender) -> dict[str, Any]:
    with effect_not_applied_on():
        url = _required_text(args.get("url"), "url")
        _ensure_http_url(url, "url")
        _ensure_allowed_domain(url, allowed_domains, channel="notification.webhook")
        payload = args["payload"]
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        headers = _headers(args.get("headers"))
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        timeout = _timeout(args.get("timeout_seconds"))
    response = sender(url, body, headers, timeout)
    return {
        "sent": True,
        "url": _url_without_query(url),
        "status_code": response.get("status_code"),
        "content_type": response.get("content_type"),
        "response_preview": str(response.get("response_text") or "")[:500],
        "payload_bytes": len(body),
    }


def _send_slack(
    args: dict[str, Any],
    *,
    webhook_url: str,
    allowed_domains: tuple[str, ...],
    sender: WebhookSender,
) -> dict[str, Any]:
    with effect_not_applied_on():
        webhook_url = webhook_url.strip()
        if not webhook_url:
            raise ValueError("slack_webhook_url is required")
        _ensure_slack_url(webhook_url, allowed_domains)
        payload = _slack_payload(args)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        timeout = _timeout(args.get("timeout_seconds"))
    response = sender(webhook_url, body, {}, timeout)
    return {
        "sent": True,
        "host": urlsplit(webhook_url).hostname,
        "status_code": response.get("status_code"),
        "content_type": response.get("content_type"),
        "response_preview": str(response.get("response_text") or "")[:500],
        "payload_bytes": len(body),
    }


def _send_email(
    args: dict[str, Any],
    *,
    from_address: str,
    allowed_domains: tuple[str, ...],
    sender: EmailSender,
) -> dict[str, Any]:
    with effect_not_applied_on():
        to_addresses = _email_addresses(args["to"], "to")
        cc_addresses = _email_addresses(args.get("cc"), "cc")
        bcc_addresses = _email_addresses(args.get("bcc"), "bcc")
        recipients = to_addresses + cc_addresses + bcc_addresses
        if not recipients:
            raise ValueError("to must contain at least one recipient")
        _ensure_allowed_email_domains(recipients, allowed_domains)
        subject = _required_text(args["subject"], "subject")
        text_body = _optional_text(args.get("text_body"))
        html_body = _optional_text(args.get("html_body"))
        if text_body is None and html_body is None:
            raise ValueError("text_body or html_body is required")
        reply_to = args.get("reply_to")
        reply_to_address = _single_email_address(reply_to, "reply_to") if reply_to is not None else None
        if reply_to_address is not None:
            _ensure_allowed_email_domains([reply_to_address], allowed_domains)
        message = _email_message(
            from_address=from_address,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            reply_to=reply_to_address,
        )
        timeout = _timeout(args.get("timeout_seconds"))
    send_result = sender(message, recipients, timeout)
    accepted_recipients = list(send_result.get("accepted_recipients") or recipients)
    refused_recipients = list(send_result.get("refused_recipients") or [])
    return {
        "sent": True,
        "message_id": message["Message-ID"],
        "from": from_address,
        "to": list(to_addresses),
        "cc": list(cc_addresses),
        "bcc_count": len(bcc_addresses),
        "subject": subject,
        "accepted_count": len(accepted_recipients),
        "refused_count": len(refused_recipients),
        "refused_recipients": refused_recipients,
    }


def _publish_rss(args: dict[str, Any], *, rss_publisher: RssFeedPublisher) -> dict[str, Any]:
    with effect_not_applied_on():
        title = _xml_text(_required_text(args.get("title"), "title"), "title")
        link = _xml_text(_required_text(args.get("link"), "link"), "link")
        _ensure_http_url(link, "link")
        description = _optional_text(args.get("description"))
        guid = _optional_text(args.get("guid"))
        published_at = _published_at(args.get("published_at"))
        published = rss_publisher.publish(
            title=title,
            link=link,
            description=_xml_text(description, "description") if description is not None else None,
            guid=_xml_text(guid, "guid") if guid is not None else None,
            published_at=published_at,
        )
    return {
        "published": not published["already_published"],
        "already_published": published["already_published"],
        "feed_path": str(rss_publisher.path),
        "item_count": published["item_count"],
        "pruned_count": published["pruned_count"],
        "title": title,
        "link": link,
    }


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("headers must be an object")
    headers: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            raise ValueError("header names must be non-empty")
        if _HEADER_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"header name is not a valid HTTP token: {name!r}")
        lowered = name.casefold()
        if lowered in _SECRET_HEADER_NAMES or lowered in _TRANSPORT_HEADER_NAMES:
            raise ValueError(f"header is not allowed in tool arguments: {name}")
        text = str(item)
        if "\r" in text or "\n" in text:
            raise ValueError(f"header {name} contains a line break")
        headers[name] = text
    return headers


def _slack_payload(args: dict[str, Any]) -> dict[str, Any]:
    text = _optional_text(args.get("text"))
    blocks = _optional_object_array(args.get("blocks"), "blocks")
    attachments = _optional_object_array(args.get("attachments"), "attachments")
    if text is None and not blocks and not attachments:
        raise ValueError("text, blocks, or attachments is required")
    payload: dict[str, Any] = {}
    if text is not None:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    if attachments:
        payload["attachments"] = attachments
    return payload


def _email_message(
    *,
    from_address: str,
    to_addresses: list[str],
    cc_addresses: list[str],
    subject: str,
    text_body: str | None,
    html_body: str | None,
    reply_to: str | None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC), usegmt=True)
    message["Message-ID"] = make_msgid(domain=_email_domain(from_address))
    if text_body is not None:
        message.set_content(text_body)
        if html_body is not None:
            message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(html_body or "", subtype="html")
    return message


def _email_addresses(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    raw_items = [str(item) for item in value]
    if any("\r" in item or "\n" in item for item in raw_items):
        raise ValueError(f"{field_name} contains a line break")
    addresses: list[str] = []
    for _, address in getaddresses(raw_items):
        normalized = address.strip().casefold()
        if not _is_email_address(normalized):
            raise ValueError(f"{field_name} contains an invalid email address")
        addresses.append(normalized)
    if len(addresses) != len(value):
        raise ValueError(f"{field_name} contains an invalid email address")
    return addresses


def _optional_object_array(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    objects: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} items must be objects")
        objects.append(dict(item))
    return objects


def _single_email_address(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    addresses = _email_addresses([text], field_name)
    if len(addresses) != 1:
        raise ValueError(f"{field_name} must contain exactly one email address")
    return addresses[0]


def _is_email_address(value: str) -> bool:
    local_part, separator, domain = value.partition("@")
    return bool(local_part and separator and _email_domain(value))


def _email_domain(value: str) -> str:
    return value.rsplit("@", maxsplit=1)[-1].casefold()


def _ensure_allowed_email_domains(addresses: list[str], allowed_domains: tuple[str, ...]) -> None:
    if not allowed_domains:
        raise ValueError("notification.email has no allowed email domains configured")
    for address in addresses:
        domain = _email_domain(address)
        if any(domain == allowed or domain.endswith(f".{allowed}") for allowed in allowed_domains):
            continue
        raise ValueError(f"notification.email recipient domain is not allowed: {domain}")


def _timeout(value: Any) -> float:
    if value is None:
        return NOTIFICATION_TIMEOUT_DEFAULT_SECONDS
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError("timeout_seconds must be a finite number")
    return max(0.1, min(seconds, NOTIFICATION_TIMEOUT_MAX_SECONDS))


def _required_text(value: Any, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _xml_text(value: str, field_name: str) -> str:
    if _XML_INVALID_CHARACTER_RE.search(value):
        raise ValueError(f"{field_name} contains characters that are not allowed in an RSS feed")
    return value


def _published_at(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, str):
        raise ValueError("published_at must be an ISO 8601 or RFC 2822 date string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ensure_http_url(url: str, field_name: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{field_name} must be an absolute http or https URL")


def _allowed_domains(allowed_domains: list[str] | None) -> tuple[str, ...]:
    return tuple(
        domain.strip().casefold().lstrip(".")
        for domain in allowed_domains or []
        if domain.strip()
    )


def _ensure_allowed_domain(url: str, allowed_domains: tuple[str, ...], *, channel: str) -> None:
    if not allowed_domains:
        raise ValueError(f"{channel} has no allowed domains configured")
    host = (urlsplit(url).hostname or "").casefold()
    if any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
        return
    raise ValueError(f"{channel} host is not in allowed domains: {host}")


def _ensure_slack_url(url: str, allowed_domains: tuple[str, ...]) -> None:
    if urlsplit(url).scheme.casefold() != "https":
        raise ValueError("notification.slack webhook URL must use https")
    _ensure_allowed_domain(url, allowed_domains, channel="notification.slack")


def _url_without_query(url: str) -> str:
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()
