"""Infrastructure storage implementations.

Heavy storage backends are resolved only when their public exports are requested,
so importing a lightweight storage primitive does not require unrelated adapters.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "LLM_CACHE_ENVELOPE_VERSION",
    "LLMCacheCodecError",
    "LLMCacheEnvelopeCodec",
    "LLMCacheReadiness",
    "RedisLLMCache",
    "decode_llm_cache_encryption_key",
    "validate_llm_cache_namespace",
    "SQLiteHarnessNodeOutputResource",
]

_LAZY_EXPORTS = {
    "LLM_CACHE_ENVELOPE_VERSION": "infrastructure.storage.redis_llm_cache",
    "LLMCacheCodecError": "infrastructure.storage.redis_llm_cache",
    "LLMCacheEnvelopeCodec": "infrastructure.storage.redis_llm_cache",
    "LLMCacheReadiness": "infrastructure.storage.redis_llm_cache",
    "RedisLLMCache": "infrastructure.storage.redis_llm_cache",
    "decode_llm_cache_encryption_key": "infrastructure.storage.redis_llm_cache",
    "validate_llm_cache_namespace": "infrastructure.storage.redis_llm_cache",
    "SQLiteHarnessNodeOutputResource": "infrastructure.storage.harness",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
