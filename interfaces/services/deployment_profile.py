"""Deployment profile selection and the validated hosted service settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from urllib.parse import urlsplit

PROFILE_ENV = "NEWSROOM_DEPLOYMENT_PROFILE"
PUBLIC_ORIGIN_ENV = "NEWSROOM_PUBLIC_ORIGIN"
ALLOW_INSECURE_ORIGIN_ENV = "NEWSROOM_ALLOW_INSECURE_ORIGIN"
TRUSTED_PROXY_CIDRS_ENV = "NEWSROOM_TRUSTED_PROXY_CIDRS"
LOGIN_RATE_LIMIT_ENV = "NEWSROOM_HOSTED_LOGIN_RATE_LIMIT_PER_MINUTE"
RESEARCH_RATE_LIMIT_ENV = "NEWSROOM_HOSTED_RESEARCH_RATE_LIMIT_PER_MINUTE"
ACCOUNT_DATA_ROOT_ENV = "NEWSROOM_ACCOUNT_DATA_ROOT"

DEFAULT_ACCOUNT_DATA_ROOT = ".newsroom/accounts"
DEFAULT_LOGIN_RATE_LIMIT = 10
DEFAULT_RESEARCH_RATE_LIMIT = 30

ProxyNetwork = IPv4Network | IPv6Network


class DeploymentProfile(StrEnum):
    DEFAULT = "default"
    HOSTED = "hosted"


class DeploymentConfigurationError(RuntimeError):
    """A deployment variable is missing or invalid; startup must stop."""

    def __init__(self, variable: str, message: str) -> None:
        super().__init__(f"{variable}: {message}")
        self.variable = variable


@dataclass(frozen=True, slots=True)
class HostedSettings:
    public_origin: str
    insecure_origin_allowed: bool
    trusted_proxies: tuple[ProxyNetwork, ...]
    login_rate_limit_per_minute: int
    research_rate_limit_per_minute: int
    account_data_root: Path

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"


def deployment_profile(env: Mapping[str, str] | None = None) -> DeploymentProfile:
    values = env if env is not None else os.environ
    raw = str(values.get(PROFILE_ENV) or DeploymentProfile.DEFAULT.value).strip()
    try:
        return DeploymentProfile(raw)
    except ValueError as exc:
        raise DeploymentConfigurationError(PROFILE_ENV, "must be default or hosted") from exc


def hosted_profile_enabled(env: Mapping[str, str] | None = None) -> bool:
    return deployment_profile(env) is DeploymentProfile.HOSTED


def load_hosted_settings(env: Mapping[str, str] | None = None) -> HostedSettings:
    values = env if env is not None else os.environ
    if deployment_profile(values) is not DeploymentProfile.HOSTED:
        raise DeploymentConfigurationError(PROFILE_ENV, "hosted settings require the hosted profile")
    insecure = _flag(values, ALLOW_INSECURE_ORIGIN_ENV)
    return HostedSettings(
        public_origin=validate_public_origin(
            str(values.get(PUBLIC_ORIGIN_ENV) or ""),
            allow_insecure=insecure,
        ),
        insecure_origin_allowed=insecure,
        trusted_proxies=_proxy_networks(str(values.get(TRUSTED_PROXY_CIDRS_ENV) or "")),
        login_rate_limit_per_minute=_bounded_int(
            values, LOGIN_RATE_LIMIT_ENV, default=DEFAULT_LOGIN_RATE_LIMIT, maximum=600
        ),
        research_rate_limit_per_minute=_bounded_int(
            values, RESEARCH_RATE_LIMIT_ENV, default=DEFAULT_RESEARCH_RATE_LIMIT, maximum=6000
        ),
        account_data_root=Path(
            str(values.get(ACCOUNT_DATA_ROOT_ENV) or DEFAULT_ACCOUNT_DATA_ROOT).strip()
        ),
    )


def validate_public_origin(value: str, *, allow_insecure: bool) -> str:
    text = value.strip()
    if not text:
        raise DeploymentConfigurationError(PUBLIC_ORIGIN_ENV, "is required in the hosted profile")
    parsed = urlsplit(text)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentConfigurationError(
            PUBLIC_ORIGIN_ENV, "must be a scheme, host and optional port only"
        )
    if parsed.scheme == "https" or (parsed.scheme == "http" and allow_insecure):
        return f"{parsed.scheme}://{parsed.netloc.lower()}"
    raise DeploymentConfigurationError(
        PUBLIC_ORIGIN_ENV,
        f"must use https (set {ALLOW_INSECURE_ORIGIN_ENV}=1 only for local integration)",
    )


def _proxy_networks(raw: str) -> tuple[ProxyNetwork, ...]:
    networks: list[ProxyNetwork] = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            networks.append(ip_network(text, strict=False))
        except ValueError as exc:
            raise DeploymentConfigurationError(
                TRUSTED_PROXY_CIDRS_ENV, f"{text!r} is not an IP network"
            ) from exc
    return tuple(networks)


def _flag(values: Mapping[str, str], name: str) -> bool:
    raw = str(values.get(name) or "").strip().lower()
    if raw in {"", "0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    raise DeploymentConfigurationError(name, "must be 1 or 0")


def _bounded_int(values: Mapping[str, str], name: str, *, default: int, maximum: int) -> int:
    raw = str(values.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise DeploymentConfigurationError(name, "must be an integer") from exc
    if value < 1 or value > maximum:
        raise DeploymentConfigurationError(name, f"must be between 1 and {maximum}")
    return value


__all__ = [
    "ACCOUNT_DATA_ROOT_ENV",
    "ALLOW_INSECURE_ORIGIN_ENV",
    "DeploymentConfigurationError",
    "DeploymentProfile",
    "HostedSettings",
    "LOGIN_RATE_LIMIT_ENV",
    "PROFILE_ENV",
    "PUBLIC_ORIGIN_ENV",
    "RESEARCH_RATE_LIMIT_ENV",
    "TRUSTED_PROXY_CIDRS_ENV",
    "deployment_profile",
    "hosted_profile_enabled",
    "load_hosted_settings",
    "validate_public_origin",
]
