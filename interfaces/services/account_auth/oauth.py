from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError

from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.errors import (
    AuthMethodUnavailableError,
    AuthProviderFailedError,
    AuthProviderUnavailableError,
)

HTTP_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class VerifiedProviderIdentity:
    subject: str
    display_name: str


class OAuthProvider:
    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str: ...
    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity: ...


class GoogleOidcProvider(OAuthProvider):
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    jwks_endpoint = "https://www.googleapis.com/oauth2/v3/certs"

    def __init__(self, settings: PublicAuthSettings) -> None:
        self.client_id = settings.google_client_id
        self.client_secret = settings.google_client_secret

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("Google sign-in is not configured")
        return self.authorization_endpoint + "?" + urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "prompt": "select_account",
            }
        )

    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("Google sign-in is not configured")
        token = _form_post(
            self.token_endpoint,
            {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        id_token = str(token.get("id_token") or "")
        if not id_token:
            raise AuthProviderFailedError("Google did not return a valid identity")
        jwks = _json_get(self.jwks_endpoint)
        try:
            claims = JsonWebToken(["RS256"]).decode(
                id_token,
                jwks,
                claims_options={
                    "iss": {"essential": True, "values": ["https://accounts.google.com", "accounts.google.com"]},
                    "aud": {"essential": True, "value": self.client_id},
                    "sub": {"essential": True},
                    "exp": {"essential": True},
                    "nonce": {"essential": True, "value": nonce},
                },
            )
            claims.validate()
        except (JoseError, ValueError) as exc:
            raise AuthProviderFailedError("Google identity validation failed") from exc
        return VerifiedProviderIdentity(subject=str(claims["sub"]), display_name=str(claims.get("name") or "Google user"))


class WeChatOAuthProvider(OAuthProvider):
    authorization_endpoint = "https://open.weixin.qq.com/connect/qrconnect"
    token_endpoint = "https://api.weixin.qq.com/sns/oauth2/access_token"
    userinfo_endpoint = "https://api.weixin.qq.com/sns/userinfo"

    def __init__(self, settings: PublicAuthSettings) -> None:
        self.client_id = settings.wechat_client_id
        self.client_secret = settings.wechat_client_secret

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("WeChat sign-in is not configured")
        query = urllib.parse.urlencode(
            {"appid": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": "snsapi_login", "state": state}
        )
        return f"{self.authorization_endpoint}?{query}#wechat_redirect"

    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("WeChat sign-in is not configured")
        token = _json_get(self.token_endpoint, {"appid": self.client_id, "secret": self.client_secret, "code": code, "grant_type": "authorization_code"})
        access_token, openid = str(token.get("access_token") or ""), str(token.get("openid") or "")
        if not access_token or not openid:
            raise AuthProviderFailedError("WeChat authorization failed")
        profile = _json_get(self.userinfo_endpoint, {"access_token": access_token, "openid": openid, "lang": "zh_CN"})
        return VerifiedProviderIdentity(subject=openid, display_name=str(profile.get("nickname") or "WeChat user"))


class QqOAuthProvider(OAuthProvider):
    authorization_endpoint = "https://graph.qq.com/oauth2.0/authorize"
    token_endpoint = "https://graph.qq.com/oauth2.0/token"
    me_endpoint = "https://graph.qq.com/oauth2.0/me"
    profile_endpoint = "https://graph.qq.com/user/get_user_info"

    def __init__(self, settings: PublicAuthSettings) -> None:
        self.client_id = settings.qq_client_id
        self.client_secret = settings.qq_client_secret

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("QQ sign-in is not configured")
        return self.authorization_endpoint + "?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri, "state": state, "scope": "get_user_info"}
        )

    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity:
        if not self.client_id or not self.client_secret:
            raise AuthMethodUnavailableError("QQ sign-in is not configured")
        token_payload = _text_get(self.token_endpoint, {"grant_type": "authorization_code", "client_id": self.client_id, "client_secret": self.client_secret, "code": code, "redirect_uri": redirect_uri})
        token = urllib.parse.parse_qs(token_payload).get("access_token", [""])[0]
        if not token:
            raise AuthProviderFailedError("QQ authorization failed")
        me = _json_or_jsonp(_text_get(self.me_endpoint, {"access_token": token, "fmt": "json"}))
        returned_client_id = str(me.get("client_id") or "")
        openid = str(me.get("openid") or "")
        if not openid or returned_client_id != self.client_id:
            raise AuthProviderFailedError("QQ did not return a valid identity")
        profile = _json_get(self.profile_endpoint, {"access_token": token, "oauth_consumer_key": self.client_id, "openid": openid})
        return VerifiedProviderIdentity(subject=openid, display_name=str(profile.get("nickname") or "QQ user"))


def provider_for(settings: PublicAuthSettings, provider: str) -> OAuthProvider:
    return {
        "google": GoogleOidcProvider,
        "wechat": WeChatOAuthProvider,
        "qq": QqOAuthProvider,
    }[provider](settings)


def _form_post(url: str, data: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("ascii"), headers={"Accept": "application/json"})
    return _read_json(request)


def _json_get(url: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    actual = url + ("?" + urllib.parse.urlencode(query) if query else "")
    return _read_json(urllib.request.Request(actual, headers={"Accept": "application/json"}))


def _text_get(url: str, query: dict[str, str]) -> str:
    request = urllib.request.Request(url + "?" + urllib.parse.urlencode(query), headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise AuthProviderUnavailableError("identity provider is unavailable") from exc
        raise AuthProviderFailedError("identity provider rejected the request") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthProviderUnavailableError("identity provider is unavailable") from exc


def _read_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise AuthProviderUnavailableError("identity provider is unavailable") from exc
        raise AuthProviderFailedError("identity provider rejected the request") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthProviderUnavailableError("identity provider is unavailable") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthProviderFailedError("identity provider returned an invalid response") from exc
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errcode"):
        raise AuthProviderFailedError("identity provider rejected the request")
    return payload


def _json_or_jsonp(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("callback"):
        stripped = stripped[stripped.find("(") + 1 : stripped.rfind(")")]
    try:
        payload = json.loads(stripped)
    except ValueError as exc:
        raise AuthProviderFailedError("identity provider returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise AuthProviderFailedError("identity provider returned an invalid response")
    return payload
