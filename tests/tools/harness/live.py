"""Configuration for the live layer (marker ``tool_live``): real services, opt-in only.

Nothing here runs unless ``NEWS_TOOL_LIVE_TESTS=1``. Each service then needs
its own variable, and a test whose service is not configured is skipped with
a reason naming the missing variable. The variables deliberately differ from
the application's own (``NEWS_DATABASE_DSN``, ``NEWS_QDRANT_URL``): a live
run writes to what it is pointed at, so it must be pointed at a test target
on purpose, never at production by accident.

| Variable | Service | Used by |
|---|---|---|
| ``NEWS_TOOL_LIVE_TESTS=1`` | switch | every live test |
| ``GITHUB_TOKEN`` | api.github.com | github.* (anonymous quota is 10 searches/min) |
| ``NEWS_TEST_POSTGRES_DSN`` | Postgres | postgres.*, report.publish/search; a throwaway schema per run |
| ``NEWS_TEST_QDRANT_URL`` (+ ``NEWS_TEST_QDRANT_API_KEY``) | Qdrant | qdrant.*; a throwaway collection per run |
| ``NEWS_TEST_SMTP_HOST``, ``_PORT``, ``_USERNAME``, ``_PASSWORD``, ``_FROM``, ``_TO``, ``_SECURITY`` | SMTP | notification.email |
| ``NEWS_TEST_WEBHOOK_URL`` | an HTTPS endpoint that accepts POST | notification.webhook |

arXiv, public feeds and DNS need only the switch.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pytest

LIVE_SWITCH = "NEWS_TOOL_LIVE_TESTS"
SMTP_SECURITY_MODES = ("starttls", "tls", "none")


@dataclass(frozen=True)
class SmtpTarget:
    host: str
    port: int
    username: str | None
    password: str | None
    from_address: str
    to_address: str
    security: str

    @property
    def domains(self) -> list[str]:
        return sorted({address.rsplit("@", 1)[1].casefold() for address in (self.from_address, self.to_address)})


@dataclass(frozen=True)
class LiveSettings:
    enabled: bool
    github_token: str | None
    postgres_dsn: str | None
    qdrant_url: str | None
    qdrant_api_key: str | None
    smtp: SmtpTarget | None
    smtp_problem: str | None
    webhook_url: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LiveSettings":
        values = os.environ if env is None else env
        smtp, smtp_problem = _smtp_target(values)
        webhook_url = _text(values, "NEWS_TEST_WEBHOOK_URL")
        if webhook_url is not None and urlsplit(webhook_url).scheme != "https":
            webhook_url = None
        return cls(
            enabled=values.get(LIVE_SWITCH, "").strip() == "1",
            github_token=_text(values, "GITHUB_TOKEN"),
            postgres_dsn=_text(values, "NEWS_TEST_POSTGRES_DSN"),
            qdrant_url=_text(values, "NEWS_TEST_QDRANT_URL"),
            qdrant_api_key=_text(values, "NEWS_TEST_QDRANT_API_KEY"),
            smtp=smtp,
            smtp_problem=smtp_problem,
            webhook_url=webhook_url,
        )

    def missing(self, service: str) -> str | None:
        """Why a live test of ``service`` cannot run here, or ``None`` if it can."""

        if not self.enabled:
            return f"live tool tests are off; set {LIVE_SWITCH}=1"
        requirements = {
            "network": None,
            "github": None if self.github_token else "GITHUB_TOKEN is not set",
            "postgres": None if self.postgres_dsn else "NEWS_TEST_POSTGRES_DSN is not set",
            "qdrant": None if self.qdrant_url else "NEWS_TEST_QDRANT_URL is not set",
            "smtp": self.smtp_problem,
            "webhook": None if self.webhook_url else "NEWS_TEST_WEBHOOK_URL is not set to an https:// URL",
        }
        if service not in requirements:
            raise KeyError(f"unknown live service: {service}")
        return requirements[service]


def _text(values: Mapping[str, str], key: str) -> str | None:
    value = str(values.get(key) or "").strip()
    return value or None


def _smtp_target(values: Mapping[str, str]) -> tuple[SmtpTarget | None, str | None]:
    required = ("NEWS_TEST_SMTP_HOST", "NEWS_TEST_SMTP_FROM", "NEWS_TEST_SMTP_TO")
    absent = [key for key in required if _text(values, key) is None]
    if absent:
        return None, f"{', '.join(absent)} not set"
    security = (_text(values, "NEWS_TEST_SMTP_SECURITY") or "starttls").casefold()
    if security not in SMTP_SECURITY_MODES:
        return None, f"NEWS_TEST_SMTP_SECURITY must be one of {', '.join(SMTP_SECURITY_MODES)}"
    default_port = {"starttls": 587, "tls": 465, "none": 25}[security]
    try:
        port = int(_text(values, "NEWS_TEST_SMTP_PORT") or default_port)
    except ValueError:
        return None, "NEWS_TEST_SMTP_PORT is not a number"
    from_address = str(_text(values, "NEWS_TEST_SMTP_FROM"))
    to_address = str(_text(values, "NEWS_TEST_SMTP_TO"))
    if "@" not in from_address or "@" not in to_address:
        return None, "NEWS_TEST_SMTP_FROM and NEWS_TEST_SMTP_TO must be email addresses"
    return (
        SmtpTarget(
            host=str(_text(values, "NEWS_TEST_SMTP_HOST")),
            port=port,
            username=_text(values, "NEWS_TEST_SMTP_USERNAME"),
            password=_text(values, "NEWS_TEST_SMTP_PASSWORD"),
            from_address=from_address,
            to_address=to_address,
            security=security,
        ),
        None,
    )


def skip_if_upstream_unavailable(errors: list[dict[str, Any]]) -> None:
    """Skip, not fail, when every reported error says the real service was briefly unavailable.

    A tool that reports a retryable error (timeout, connection failure, 5xx,
    a retryable 4xx such as 429) did its job: it stayed inside its budget and
    said what happened. What it could not do is show the live contract, so the
    test is skipped with the errors as its reason. Any non-retryable error, or
    a mix, falls through to the test's own assertions and fails there.
    """

    if errors and all(error.get("retryable") is True for error in errors):
        summary = "; ".join(f"{error.get('error_type')}: {error.get('error_message')}" for error in errors)
        pytest.skip(f"upstream unavailable, retryable errors only: {summary}")
