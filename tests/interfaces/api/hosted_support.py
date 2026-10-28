"""Test helpers for running the API under the hosted deployment profile."""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Any

from interfaces.composition.hosted import HostedRuntime, build_hosted_runtime
from interfaces.services.account_administration import AccountAdministrationService

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_ORIGIN = "http://127.0.0.1:3000"
# A fixed, test-only master key; real deployments generate their own.
TEST_ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
TEST_ACTIVITY_ENCRYPTION_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).decode("ascii")
TEST_RESEARCH_REQUEST_TOKENS = 64_000


def hosted_env(data_root: Path, **overrides: str) -> dict[str, str]:
    values = {
        "NEWSROOM_DEPLOYMENT_PROFILE": "hosted",
        "NEWSROOM_PUBLIC_ORIGIN": HOSTED_ORIGIN,
        "NEWSROOM_ALLOW_INSECURE_ORIGIN": "1",
        "NEWSROOM_ACCOUNT_DATA_ROOT": str(data_root / "accounts"),
        "NEWSROOM_CREDENTIAL_ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "NEWS_ACTIVITY_ENCRYPTION_KEY": TEST_ACTIVITY_ENCRYPTION_KEY,
        "NEWSROOM_LLM_CREDENTIAL_MODE": "invocation",
        "NEWS_RESEARCH_PARSER_BACKENDS": "pymupdf",
        "NEWSROOM_PDF_PARSER_CASCADE": "pymupdf",
        "NEWSROOM_HOSTED_LLM_PROVIDERS_PATH": str(REPO_ROOT / "configs" / "hosted-llm-providers.yaml"),
    }
    values.update(overrides)
    return values


def configure_hosted_env(monkeypatch, data_root: Path, **overrides: str) -> dict[str, str]:
    values = hosted_env(data_root, **overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("NEWS_API_KEYS", "NEWS_API_TOKEN", "NEWSROOM_API_TOKEN", "NEWS_API_RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(key, raising=False)
    return values


def build_test_hosted_runtime(
    data_root: Path,
    *,
    auth: Any | None = None,
    **overrides: str,
) -> HostedRuntime:
    """Build the real hosted runtime; ``auth`` swaps in a session double."""

    runtime = build_hosted_runtime(
        hosted_env(data_root, **overrides),
        research_request_tokens=TEST_RESEARCH_REQUEST_TOKENS,
    )
    if auth is None:
        return runtime
    return replace(runtime, auth=auth, accounts=AccountAdministrationService(auth))


def sign_in(runtime: HostedRuntime, username: str, password: str, *, role: str = "user") -> dict[str, str]:
    """Create an account and return its session as BFF-style request headers."""

    runtime.accounts.create_account(username=username, role=role, password=password)
    session = runtime.auth.login(username=username, password=password)
    return {"X-Newsroom-Session": session.sessionToken}


def mutation_headers(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "Origin": HOSTED_ORIGIN}


__all__ = [
    "HOSTED_ORIGIN",
    "TEST_ENCRYPTION_KEY",
    "build_test_hosted_runtime",
    "configure_hosted_env",
    "hosted_env",
    "mutation_headers",
    "sign_in",
]
