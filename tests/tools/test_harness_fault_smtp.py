"""The fault SMTP server fails the way smtplib clients observe real servers failing."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

import pytest

from tests.tools.harness.fault_smtp import FaultSMTPServer, SMTPScript

StartSMTP = Callable[[SMTPScript], FaultSMTPServer]


def _message(*recipients: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "bot@example.com"
    message["To"] = ", ".join(recipients)
    message["Subject"] = "Daily brief"
    message.set_content("line one\n.leading dot survives\n")
    return message


def test_accepts_and_records_a_message(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript())
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        refused = client.send_message(_message("a@example.com"), to_addrs=["a@example.com"])

    assert refused == {}
    [received] = server.messages
    assert received.mail_from == "bot@example.com"
    assert received.recipients == ("a@example.com",)
    assert received.message["Subject"] == "Daily brief"
    assert ".leading dot survives" in received.message.get_content()


def test_refusing_service_fails_the_connect(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(refuse_service=True))
    with pytest.raises(smtplib.SMTPConnectError):
        smtplib.SMTP(server.host, server.port, timeout=2)


def test_partially_rejected_recipients_are_reported(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(rejected_recipients=frozenset({"gone@example.com"})))
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        refused = client.send_message(
            _message("a@example.com", "gone@example.com"),
            to_addrs=["a@example.com", "gone@example.com"],
        )
    assert set(refused) == {"gone@example.com"}
    assert server.messages[0].recipients == ("a@example.com",)


def test_starttls_is_unavailable_unless_advertised(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript())
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        with pytest.raises(smtplib.SMTPNotSupportedError):
            client.starttls()


def test_advertised_starttls_is_refused(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(advertise_starttls=True))
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        with pytest.raises(smtplib.SMTPResponseException) as error:
            client.starttls()
    assert error.value.smtp_code == 454


def test_wrong_credentials_are_rejected(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(credentials=("bot", "right")))
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        with pytest.raises(smtplib.SMTPAuthenticationError):
            client.login("bot", "wrong")
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        client.login("bot", "right")
        assert client.send_message(_message("a@example.com"), to_addrs=["a@example.com"]) == {}


def test_drop_after_ehlo_disconnects_the_session(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(drop_after_ehlo=True))
    with pytest.raises(smtplib.SMTPServerDisconnected):
        with smtplib.SMTP(server.host, server.port, timeout=2) as client:
            client.send_message(_message("a@example.com"), to_addrs=["a@example.com"])


def test_temporary_data_failure_is_an_smtp_error(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(data_reply=(451, "try again later")))
    with smtplib.SMTP(server.host, server.port, timeout=2) as client:
        with pytest.raises(smtplib.SMTPDataError) as error:
            client.send_message(_message("a@example.com"), to_addrs=["a@example.com"])
    assert error.value.smtp_code == 451


def test_stall_after_data_records_the_message_but_never_acknowledges(fault_smtp: StartSMTP) -> None:
    server = fault_smtp(SMTPScript(stall_at="data_end"))
    with pytest.raises((TimeoutError, smtplib.SMTPServerDisconnected)):
        with smtplib.SMTP(server.host, server.port, timeout=0.3) as client:
            client.send_message(_message("a@example.com"), to_addrs=["a@example.com"])
    assert len(server.messages) == 1
