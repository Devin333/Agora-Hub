"""Administrator-managed accounts for the hosted service.

Accounts are created by an administrator (or the server CLI for the first
one); there is no self-registration. Every rule that looks across accounts,
such as keeping one enabled administrator, is checked inside the store lock
through ``AuthApplicationService.modify_account``.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import datetime, timezone

from interfaces.services.auth_service import (
    AuthApplicationService,
    AuthUser,
    StoredAuthUser,
    validate_account_role,
    with_new_password,
)

UTC = timezone.utc
GENERATED_PASSWORD_BYTES = 12


class AccountAdministrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AccountAdministrationService:
    def __init__(self, auth: AuthApplicationService) -> None:
        self._auth = auth

    def list_accounts(self) -> list[AuthUser]:
        return self._auth.list_accounts()

    def create_account(
        self,
        *,
        username: str,
        role: str,
        password: str | None = None,
    ) -> tuple[AuthUser, str | None]:
        """Create an account; returns the generated password when none was given.

        A generated password is returned exactly once so the administrator can
        hand it over; it is never stored in clear.
        """

        generated = None if password else generate_initial_password()
        account = self._auth.create_account(
            username=username,
            password=password or generated or "",
            role=validate_account_role(role),
        )
        return account, generated

    def set_enabled(self, *, actor_user_id: str, user_id: str, enabled: bool) -> AuthUser:
        if not enabled and user_id == actor_user_id:
            raise AccountAdministrationError(
                "cannot_disable_self",
                "Administrators cannot disable their own account",
                status_code=409,
            )

        def change(user: StoredAuthUser, accounts: list[StoredAuthUser]) -> StoredAuthUser:
            if enabled:
                if user.enabled:
                    return user
                now = datetime.now(UTC)
                return replace(user, disabledAt=None, updatedAt=now)
            if not user.enabled:
                return user
            _require_another_admin(user, accounts)
            now = datetime.now(UTC)
            return replace(user, disabledAt=now, sessionsValidFrom=now, updatedAt=now)

        return self._auth.modify_account(user_id, change)

    def reset_password(self, *, user_id: str, password: str | None = None) -> tuple[AuthUser, str | None]:
        generated = None if password else generate_initial_password()
        new_password = password or generated or ""

        def change(user: StoredAuthUser, _accounts: list[StoredAuthUser]) -> StoredAuthUser:
            return with_new_password(user, new_password)

        return self._auth.modify_account(user_id, change), generated

    def change_role(self, *, user_id: str, role: str) -> AuthUser:
        """Change a role; demoting oneself is allowed while another admin remains."""

        target_role = validate_account_role(role)

        def change(user: StoredAuthUser, accounts: list[StoredAuthUser]) -> StoredAuthUser:
            if user.role == target_role:
                return user
            if user.role == "admin":
                _require_another_admin(user, accounts)
            now = datetime.now(UTC)
            # A role change alters what every open session may do; make the
            # account sign in again instead of carrying the old role around.
            return replace(user, role=target_role, sessionsValidFrom=now, updatedAt=now)

        return self._auth.modify_account(user_id, change)


def generate_initial_password() -> str:
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def _require_another_admin(user: StoredAuthUser, accounts: list[StoredAuthUser]) -> None:
    if user.role != "admin" or not user.enabled:
        return
    if any(
        other.userId != user.userId and other.role == "admin" and other.enabled
        for other in accounts
    ):
        return
    raise AccountAdministrationError(
        "last_admin_required",
        "At least one enabled administrator must remain",
        status_code=409,
    )


__all__ = [
    "AccountAdministrationError",
    "AccountAdministrationService",
    "generate_initial_password",
]
