"""Shared mapping of account and model-credential failures to API errors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request

from interfaces.api.deps import ApiRouteHelpers
from interfaces.services.account_administration import AccountAdministrationError
from interfaces.services.auth_service import (
    AuthAccountExistsError,
    AuthAccountNotFoundError,
    AuthInvalidCredentialsError,
    AuthSessionInvalidError,
)
from interfaces.services.hosted_boundary import HostedSessionActor
from interfaces.services.model_credentials import ModelCredentialError


def signed_in(request: Request) -> HostedSessionActor:
    """The session the hosted boundary authenticated for this request."""

    session = getattr(request.state, "hosted_session", None)
    if not isinstance(session, HostedSessionActor):
        raise AuthSessionInvalidError("valid session required")
    return session


def run_account_call(helpers: ApiRouteHelpers, call: Callable[[], dict[str, Any]]):
    try:
        return helpers.success(call())
    except AccountAdministrationError as exc:
        return _error(helpers, exc.status_code, exc.code, str(exc))
    except ModelCredentialError as exc:
        return helpers.error(
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            user_action_required=True,
        )
    except AuthSessionInvalidError:
        return _error(helpers, 401, "auth_required", "Valid account session required")
    except AuthInvalidCredentialsError as exc:
        return _error(helpers, 400, exc.code, str(exc))
    except AuthAccountNotFoundError:
        return _error(helpers, 404, "account_not_found", "Account was not found")
    except AuthAccountExistsError as exc:
        return _error(helpers, 409, exc.code, str(exc))
    except ValueError as exc:
        # Username, password and role policy violations.
        return _error(helpers, 400, "account_invalid_request", str(exc))


def _error(helpers: ApiRouteHelpers, status_code: int, code: str, message: str):
    return helpers.error(
        status_code=status_code,
        code=code,
        message=message,
        user_action_required=True,
    )


__all__ = ["run_account_call", "signed_in"]
