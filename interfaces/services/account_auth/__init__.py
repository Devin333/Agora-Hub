"""Public account authentication services and provider adapters."""

from typing import Any

from interfaces.services.account_auth.config import PublicAuthSettings

__all__ = ["PublicAccountAuthService", "PublicAuthSettings"]


def __getattr__(name: str) -> Any:
    if name == "PublicAccountAuthService":
        from interfaces.services.account_auth.service import PublicAccountAuthService

        return PublicAccountAuthService
    raise AttributeError(name)
