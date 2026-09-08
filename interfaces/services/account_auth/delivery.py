from __future__ import annotations

import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Protocol

from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.errors import AuthDeliveryFailedError, AuthMethodUnavailableError


class OtpDelivery(Protocol):
    def send(self, *, destination: str, code: str) -> None: ...


class SmtpOtpDelivery:
    def __init__(self, settings: PublicAuthSettings) -> None:
        self.settings = settings

    def send(self, *, destination: str, code: str) -> None:
        if not self.settings.smtp_host or not self.settings.smtp_from:
            raise AuthMethodUnavailableError("email sign-in is not configured")
        if self.settings.smtp_security not in {"starttls", "ssl"}:
            raise AuthMethodUnavailableError("email transport security is not configured")
        message = EmailMessage()
        message["Subject"] = "Agora AI verification code"
        message["From"] = self.settings.smtp_from
        message["To"] = destination
        message.set_content(f"Your Agora AI verification code is {code}. It expires in 10 minutes.")
        try:
            if self.settings.smtp_security == "ssl":
                client = smtplib.SMTP_SSL(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=10,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10)
            with client:
                client.ehlo()
                if self.settings.smtp_security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.settings.smtp_username:
                    client.login(self.settings.smtp_username, self.settings.smtp_password or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise AuthDeliveryFailedError("verification email could not be delivered") from exc


class TwilioSmsOtpDelivery:
    def __init__(self, settings: PublicAuthSettings) -> None:
        self.settings = settings

    def send(self, *, destination: str, code: str) -> None:
        sid = self.settings.twilio_account_sid
        token = self.settings.twilio_auth_token
        sender = self.settings.twilio_from
        if not sid or not token or not sender:
            raise AuthMethodUnavailableError("phone sign-in is not configured")
        body = urllib.parse.urlencode(
            {"To": destination, "From": sender, "Body": f"Your Agora AI verification code is {code}. It expires in 10 minutes."}
        ).encode("ascii")
        request = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid, safe='')}/Messages.json",
            data=body,
            method="POST",
            headers={
                "Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode("ascii"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status not in {200, 201} or not payload.get("sid"):
                    raise AuthDeliveryFailedError("verification message could not be delivered")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise AuthDeliveryFailedError("verification message could not be delivered") from exc
