"""The live layer stays off unless asked for, and says why each test skipped.

Runs in the default fault layer, so a change that would make live tests hit
real services on a plain ``pytest`` run, or skip without a reason, fails here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tools.harness.live import LIVE_SWITCH, LiveSettings, skip_if_upstream_unavailable

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_TESTS = "tests/tools/live"
LIVE_VARIABLES = (
    LIVE_SWITCH,
    "GITHUB_TOKEN",
    "NEWS_TEST_POSTGRES_DSN",
    "NEWS_TEST_QDRANT_URL",
    "NEWS_TEST_QDRANT_API_KEY",
    "NEWS_TEST_SMTP_HOST",
    "NEWS_TEST_SMTP_PORT",
    "NEWS_TEST_SMTP_USERNAME",
    "NEWS_TEST_SMTP_PASSWORD",
    "NEWS_TEST_SMTP_FROM",
    "NEWS_TEST_SMTP_TO",
    "NEWS_TEST_SMTP_SECURITY",
    "NEWS_TEST_WEBHOOK_URL",
)


def _pytest(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    child_env = {key: value for key, value in os.environ.items() if key not in LIVE_VARIABLES}
    child_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", LIVE_TESTS, "-q", "-rs", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_plain_pytest_run_deselects_every_live_test() -> None:
    result = _pytest()

    assert result.returncode == 5, result.stdout + result.stderr  # 5: nothing selected
    assert "deselected" in result.stdout and "passed" not in result.stdout


def test_selected_live_tests_without_the_switch_all_skip_with_the_reason() -> None:
    result = _pytest("-m", "tool_live")

    assert result.returncode == 0, result.stdout + result.stderr
    summary = result.stdout.strip().splitlines()[-1]
    assert "skipped" in summary and "passed" not in summary and "failed" not in summary
    assert f"set {LIVE_SWITCH}=1" in result.stdout


def test_switch_on_without_credentials_names_each_missing_variable() -> None:
    # Only tests that name a credentialed service: the network-only ones would
    # really run with the switch on.
    result = _pytest("-m", "tool_live and live_service", **{LIVE_SWITCH: "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    for variable in ("GITHUB_TOKEN", "NEWS_TEST_POSTGRES_DSN", "NEWS_TEST_QDRANT_URL", "NEWS_TEST_SMTP_HOST", "NEWS_TEST_WEBHOOK_URL"):
        assert variable in result.stdout, variable


def test_application_variables_never_enable_a_live_write() -> None:
    """Pointing the app at its own database must not make live tests write there."""

    settings = LiveSettings.from_env(
        {LIVE_SWITCH: "1", "NEWS_DATABASE_DSN": "postgresql://app@db/news", "NEWS_QDRANT_URL": "http://qdrant:6333"}
    )

    assert settings.missing("postgres") == "NEWS_TEST_POSTGRES_DSN is not set"
    assert settings.missing("qdrant") == "NEWS_TEST_QDRANT_URL is not set"


@pytest.mark.parametrize(
    "env, problem",
    [
        ({"NEWS_TEST_SMTP_HOST": "smtp.example.com", "NEWS_TEST_SMTP_FROM": "a@example.com"}, "NEWS_TEST_SMTP_TO not set"),
        (
            {"NEWS_TEST_SMTP_HOST": "h", "NEWS_TEST_SMTP_FROM": "a@x.com", "NEWS_TEST_SMTP_TO": "b@x.com", "NEWS_TEST_SMTP_SECURITY": "ssl3"},
            "NEWS_TEST_SMTP_SECURITY must be one of starttls, tls, none",
        ),
        (
            {"NEWS_TEST_SMTP_HOST": "h", "NEWS_TEST_SMTP_FROM": "a@x.com", "NEWS_TEST_SMTP_TO": "b@x.com", "NEWS_TEST_SMTP_PORT": "smtp"},
            "NEWS_TEST_SMTP_PORT is not a number",
        ),
        ({"NEWS_TEST_SMTP_HOST": "h", "NEWS_TEST_SMTP_FROM": "nobody", "NEWS_TEST_SMTP_TO": "b@x.com"}, "must be email addresses"),
    ],
    ids=["missing-to", "bad-security", "bad-port", "bad-address"],
)
def test_incomplete_smtp_settings_skip_with_the_specific_problem(env: dict[str, str], problem: str) -> None:
    reason = LiveSettings.from_env({LIVE_SWITCH: "1", **env}).missing("smtp")
    assert reason is not None and problem in reason


def test_complete_smtp_settings_derive_port_and_allowed_domains() -> None:
    settings = LiveSettings.from_env(
        {
            LIVE_SWITCH: "1",
            "NEWS_TEST_SMTP_HOST": "smtp.example.com",
            "NEWS_TEST_SMTP_FROM": "bot@example.com",
            "NEWS_TEST_SMTP_TO": "inbox@Example.org",
            "NEWS_TEST_SMTP_SECURITY": "tls",
        }
    )

    assert settings.missing("smtp") is None
    assert settings.smtp is not None and settings.smtp.port == 465
    assert settings.smtp.domains == ["example.com", "example.org"]


def test_plain_http_webhook_target_is_not_accepted() -> None:
    settings = LiveSettings.from_env({LIVE_SWITCH: "1", "NEWS_TEST_WEBHOOK_URL": "http://hooks.example.com/x"})
    assert settings.missing("webhook") is not None


def _source_error(error_type: str, retryable: bool) -> dict[str, object]:
    return {"error_type": error_type, "error_message": f"{error_type} happened", "retryable": retryable}


def test_only_retryable_upstream_errors_skip_with_each_error_named() -> None:
    errors = [_source_error("fetch_timeout", True), _source_error("fetch_http_5xx", True)]

    with pytest.raises(pytest.skip.Exception) as skipped:
        skip_if_upstream_unavailable(errors)

    assert "fetch_timeout: fetch_timeout happened" in str(skipped.value)
    assert "fetch_http_5xx: fetch_http_5xx happened" in str(skipped.value)


@pytest.mark.parametrize(
    "errors",
    [
        [],
        [_source_error("robots_disallowed", False)],
        [_source_error("fetch_timeout", True), _source_error("parse_error", False)],
        [{"error_type": "fetch_timeout", "error_message": "no retryable flag"}],
    ],
    ids=["no-errors", "non-retryable", "mixed", "retryable-unknown"],
)
def test_any_non_retryable_or_unclassified_error_is_left_to_fail(errors: list[dict[str, object]]) -> None:
    skip_if_upstream_unavailable(errors)
