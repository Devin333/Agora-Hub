from __future__ import annotations


class PublicAuthError(ValueError):
    code = "auth_public_error"
    status_code = 400
    retryable = False


class AuthMethodUnavailableError(PublicAuthError):
    code = "auth_method_unavailable"
    status_code = 503


class AuthConsentRequiredError(PublicAuthError):
    code = "auth_consent_required"


class AuthInvalidBindingError(PublicAuthError):
    code = "auth_invalid_browser_binding"


class AuthChallengeInvalidError(PublicAuthError):
    code = "auth_challenge_invalid"
    status_code = 401


class AuthChallengeExpiredError(PublicAuthError):
    code = "auth_challenge_invalid"
    status_code = 400


class AuthChallengeRateLimitedError(PublicAuthError):
    code = "auth_rate_limited"
    status_code = 429
    retryable = True

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class AuthDeliveryFailedError(PublicAuthError):
    code = "auth_delivery_failed"
    status_code = 502
    retryable = True


class AuthProviderStateInvalidError(PublicAuthError):
    code = "auth_provider_rejected"
    status_code = 400


class AuthProviderStateExpiredError(PublicAuthError):
    code = "auth_provider_rejected"
    status_code = 400


class AuthProviderFailedError(PublicAuthError):
    code = "auth_provider_rejected"
    status_code = 400


class AuthProviderUnavailableError(PublicAuthError):
    code = "auth_provider_unavailable"
    status_code = 503
    retryable = True


class AuthIdentityConflictError(PublicAuthError):
    code = "auth_identity_conflict"
    status_code = 409
