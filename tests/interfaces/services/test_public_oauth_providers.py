from __future__ import annotations

import time
from pathlib import Path

import pytest
from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from interfaces.services.account_auth import oauth
from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.errors import AuthProviderFailedError
from interfaces.services.account_auth.oauth import GoogleOidcProvider, QqOAuthProvider, WeChatOAuthProvider


def test_google_exchange_accepts_locally_signed_valid_id_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private_key, jwks = _rsa_keypair("valid-key")
    token = _google_token(private_key, kid="valid-key")
    monkeypatch.setattr(oauth, "_form_post", lambda *_args, **_kwargs: {"id_token": token})
    monkeypatch.setattr(oauth, "_json_get", lambda *_args, **_kwargs: jwks)

    identity = GoogleOidcProvider(_settings(tmp_path)).exchange(
        code="authorization-code",
        redirect_uri="https://agora.example/api/auth/oauth/google/callback",
        nonce="expected-nonce",
    )

    assert identity.subject == "google-subject"
    assert identity.display_name == "Ada Researcher"


@pytest.mark.parametrize(
    ("overrides", "wrong_signing_key"),
    [
        ({"aud": "different-client"}, False),
        ({"nonce": "different-nonce"}, False),
        ({"exp": int(time.time()) - 60}, False),
        ({}, True),
    ],
    ids=["wrong-audience", "wrong-nonce", "expired", "wrong-signature"],
)
def test_google_exchange_rejects_invalid_id_token_claims_or_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, object],
    wrong_signing_key: bool,
) -> None:
    trusted_key, jwks = _rsa_keypair("trusted-key")
    signing_key = _rsa_keypair("trusted-key")[0] if wrong_signing_key else trusted_key
    token = _google_token(signing_key, kid="trusted-key", overrides=overrides)
    monkeypatch.setattr(oauth, "_form_post", lambda *_args, **_kwargs: {"id_token": token})
    monkeypatch.setattr(oauth, "_json_get", lambda *_args, **_kwargs: jwks)

    with pytest.raises(AuthProviderFailedError, match="Google identity validation failed"):
        GoogleOidcProvider(_settings(tmp_path)).exchange(
            code="authorization-code",
            redirect_uri="https://agora.example/api/auth/oauth/google/callback",
            nonce="expected-nonce",
        )


def test_wechat_exchange_rejects_malformed_token_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(oauth, "_json_get", lambda *_args, **_kwargs: {"access_token": "token-without-openid"})

    with pytest.raises(AuthProviderFailedError, match="WeChat authorization failed"):
        WeChatOAuthProvider(_settings(tmp_path)).exchange(
            code="authorization-code",
            redirect_uri="https://agora.example/api/auth/oauth/wechat/callback",
            nonce="unused",
        )


def test_wechat_exchange_rejects_provider_token_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AuthProviderFailedError("identity provider rejected the request")

    monkeypatch.setattr(oauth, "_json_get", reject)

    with pytest.raises(AuthProviderFailedError, match="provider rejected"):
        WeChatOAuthProvider(_settings(tmp_path)).exchange(
            code="rejected-code",
            redirect_uri="https://agora.example/api/auth/oauth/wechat/callback",
            nonce="unused",
        )


@pytest.mark.parametrize(
    "token_response",
    ["error=100005&error_description=invalid_code", "access_token="],
)
def test_qq_exchange_rejects_token_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_response: str,
) -> None:
    monkeypatch.setattr(oauth, "_text_get", lambda *_args, **_kwargs: token_response)

    with pytest.raises(AuthProviderFailedError, match="QQ authorization failed"):
        QqOAuthProvider(_settings(tmp_path)).exchange(
            code="authorization-code",
            redirect_uri="https://agora.example/api/auth/oauth/qq/callback",
            nonce="unused",
        )


def test_qq_exchange_rejects_malformed_identity_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    responses = iter(["access_token=token&expires_in=3600", "not-json"])
    monkeypatch.setattr(oauth, "_text_get", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(AuthProviderFailedError, match="invalid response"):
        QqOAuthProvider(_settings(tmp_path)).exchange(
            code="authorization-code",
            redirect_uri="https://agora.example/api/auth/oauth/qq/callback",
            nonce="unused",
        )


def test_qq_exchange_rejects_identity_from_another_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    responses = iter(
        [
            "access_token=token&expires_in=3600",
            '{"client_id":"different-client","openid":"qq-subject"}',
        ]
    )
    monkeypatch.setattr(oauth, "_text_get", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(AuthProviderFailedError, match="valid identity"):
        QqOAuthProvider(_settings(tmp_path)).exchange(
            code="authorization-code",
            redirect_uri="https://agora.example/api/auth/oauth/qq/callback",
            nonce="unused",
        )


def _rsa_keypair(kid: str) -> tuple[object, dict[str, object]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    imported = JsonWebKey.import_key(private_pem, {"kid": kid})
    return imported, {"keys": [imported.as_dict(is_private=False)]}


def _google_token(
    signing_key: object,
    *,
    kid: str,
    overrides: dict[str, object] | None = None,
) -> str:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": "google-id",
        "sub": "google-subject",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nonce": "expected-nonce",
        "name": "Ada Researcher",
    }
    claims.update(overrides or {})
    return JsonWebToken(["RS256"]).encode({"alg": "RS256", "kid": kid}, claims, signing_key).decode("ascii")


def _settings(tmp_path: Path) -> PublicAuthSettings:
    return PublicAuthSettings(
        secret="s" * 64,
        terms_url="https://agora.example/terms",
        privacy_url="https://agora.example/privacy",
        public_origin="https://agora.example",
        otp_store_path=tmp_path / "otp.json",
        oauth_store_path=tmp_path / "oauth.json",
        google_client_id="google-id",
        google_client_secret="google-secret",
        wechat_client_id="wechat-id",
        wechat_client_secret="wechat-secret",
        qq_client_id="qq-id",
        qq_client_secret="qq-secret",
    )
