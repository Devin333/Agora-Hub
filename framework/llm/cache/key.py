from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from framework.llm.cache.contracts import CacheContext, CacheDependencies, CacheScope
from framework.llm.models.request import LLMRequest
from framework.llm.structured_output import StructuredOutputCacheIdentity


DEFAULT_CACHE_NAMESPACE = "newsroom:llm-cache"
DEFAULT_CACHE_KEY_VERSION = "v3"
DEFAULT_CACHE_GENERATION = "v3"
_DOMAIN_PREFIX = b"newsroom-llm-cache\x00"
_DIAGNOSTIC_METADATA_KEYS = {
    "call_id",
    "correlation_id",
    "llm_call_id",
    "request_id",
    "run_id",
    "span_id",
    "trace_id",
}


class CacheCanonicalizationError(ValueError):
    pass


@dataclass(frozen=True)
class LLMCacheKey:
    namespace: str
    key_version: str
    scope_digest: str
    deployment_digest: str
    request_digest: str
    deployment_id: str
    provider: str
    model: str
    cache_generation: str
    structured_output_identity: StructuredOutputCacheIdentity | None = None

    @property
    def digest(self) -> str:
        return self.request_digest

    @classmethod
    def from_request(
        cls,
        *,
        provider: str,
        model: str,
        request: LLMRequest,
    ) -> LLMCacheKey:
        """Build a development-only compatibility key for `CachedLLMClient`."""
        factory = LLMCacheKeyFactory(
            secret=b"newsroom-development-cache-compatibility-only",
            key_version="dev-v3",
            cache_generation="dev-v3",
        )
        context = CacheContext(
            scope=CacheScope(
                tenant_id="development",
                project_id="development",
                policy_scope="development",
            ),
            dependencies=CacheDependencies({"compatibility": "v1"}),
        )
        return factory.build(
            request=request,
            context=context,
            deployment_id=f"{provider}:{model}",
            provider=provider,
            model=model,
        )

    def to_string(self) -> str:
        return ":".join(
            (
                self.namespace.rstrip(":"),
                self.key_version,
                self.scope_digest,
                self.deployment_digest,
                self.request_digest,
            )
        )

    def short_digest(self, length: int = 12) -> str:
        bounded = max(6, min(int(length), 16))
        return self.request_digest[:bounded]


class LLMCacheKeyFactory:
    def __init__(
        self,
        *,
        secret: str | bytes,
        namespace: str = DEFAULT_CACHE_NAMESPACE,
        key_version: str = DEFAULT_CACHE_KEY_VERSION,
        cache_generation: str = DEFAULT_CACHE_GENERATION,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(secret_bytes) < 16:
            raise ValueError("LLM cache key secret must contain at least 16 bytes")
        self._secret = secret_bytes
        self.namespace = _validate_identifier(namespace, field="namespace", allow_colon=True)
        self.key_version = _validate_identifier(key_version, field="key_version")
        self.cache_generation = _validate_identifier(
            cache_generation,
            field="cache_generation",
        )

    def build(
        self,
        *,
        request: LLMRequest,
        context: CacheContext,
        deployment_id: str,
        provider: str,
        model: str,
        prepared_identity: Mapping[str, Any] | None = None,
    ) -> LLMCacheKey:
        if context.scope is None or not context.scope.complete:
            raise CacheCanonicalizationError("complete cache scope is required")
        deployment_id = _required_text(deployment_id, field="deployment_id")
        provider = _required_text(provider, field="provider")
        model = _required_text(model, field="model")

        scope_payload = context.scope.to_key_payload()
        deployment_payload = {
            "cache_generation": self.cache_generation,
            "deployment_id": deployment_id,
            "model": model,
            "provider": provider,
        }
        request_payload = _request_semantic_payload(
            request=request,
            context=context,
            deployment=deployment_payload,
            key_version=self.key_version,
            prepared_identity=prepared_identity,
        )
        structured_output_identity = StructuredOutputCacheIdentity.from_request(request)
        scope_digest = self._hmac("scope", scope_payload)[:16]
        deployment_digest = self._hmac("deployment", deployment_payload)[:16]
        request_digest = self._hmac("request", request_payload)
        return LLMCacheKey(
            namespace=self.namespace,
            key_version=self.key_version,
            scope_digest=scope_digest,
            deployment_digest=deployment_digest,
            request_digest=request_digest,
            deployment_id=deployment_id,
            provider=provider,
            model=model,
            cache_generation=self.cache_generation,
            structured_output_identity=structured_output_identity,
        )

    def _hmac(self, domain: str, payload: Any) -> str:
        message = _DOMAIN_PREFIX + domain.encode("ascii") + b"\x00" + canonical_json_bytes(payload)
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _canonical_value(value, path="$")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _request_semantic_payload(
    *,
    request: LLMRequest,
    context: CacheContext,
    deployment: dict[str, str],
    key_version: str,
    prepared_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request_metadata = {
        str(key): value
        for key, value in request.metadata.items()
        if key != "llm_cache" and str(key) not in _DIAGNOSTIC_METADATA_KEYS
    }
    explicit_semantic = dict(context.semantic_metadata)
    conflicting = sorted(set(request_metadata).intersection(explicit_semantic))
    if conflicting:
        raise CacheCanonicalizationError(
            "semantic metadata duplicates request metadata keys: " + ", ".join(conflicting)
        )
    semantic_metadata = {**request_metadata, **explicit_semantic}
    execution_identity = request.execution_identity
    return {
        "canonical_schema": "newsroom.llm-cache-request.v3",
        "cache_key_version": key_version,
        "deployment": deployment,
        "messages": request.messages,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "tools": request.tools,
        "response_format": request.response_format,
        "output_schema": request.output_schema,
        "output_schema_name": request.output_schema_name,
        "structured_output_identity": (
            StructuredOutputCacheIdentity.from_request(request).to_dict()
            if StructuredOutputCacheIdentity.from_request(request) is not None
            else None
        ),
        "dependencies": context.dependencies.to_key_payload(),
        "semantic_metadata": semantic_metadata,
        "deterministic_seed": context.deterministic_seed,
        "prepared_identity": dict(prepared_identity or {}),
        "graph_stage_identity": (
            {
                "run_id": execution_identity.run_id,
                "graph_id": execution_identity.graph_id,
                "graph_version": execution_identity.graph_version,
                "graph_ref": execution_identity.graph_ref,
                "graph_checksum": execution_identity.graph_checksum,
                "node_id": execution_identity.node_id,
                "node_instance_id": execution_identity.node_instance_id,
            }
            if execution_identity is not None
            else None
        ),
    }


def _canonical_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CacheCanonicalizationError(f"{path}: non-finite numbers are unsupported")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise CacheCanonicalizationError(f"{path}: mapping keys must be strings")
            normalized[key] = _canonical_value(child, path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonical_value(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    raise CacheCanonicalizationError(
        f"{path}: unsupported canonical value type {type(value).__name__}"
    )


def _validate_identifier(value: str, *, field: str, allow_colon: bool = False) -> str:
    value = _required_text(value, field=field)
    if len(value) > 96:
        raise ValueError(f"{field} must be at most 96 characters")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if allow_colon:
        allowed.add(":")
    if any(character not in allowed for character in value):
        raise ValueError(f"{field} contains unsupported characters")
    normalized = value.rstrip(":") if allow_colon else value
    if not normalized:
        raise ValueError(f"{field} must contain a non-colon character")
    return normalized


def _required_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
