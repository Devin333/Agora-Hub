from __future__ import annotations

from collections.abc import Iterable

from framework.llm.context.normalization import (
    CanonicalLLMRequestNormalizer,
    LLMRequestNormalizerRegistry,
)
from framework.llm.context.openai import (
    OPENAI_CHAT_NORMALIZER_REVISION,
    OpenAICompatibleRequestNormalizer,
)
from framework.llm.context.preflight import LLMRequestPreparer
from framework.llm.context.profile import ModelContextProfile
from framework.llm.context.tokens import LLMTokenCounterRegistry


def build_default_request_preparer(
    profiles: Iterable[ModelContextProfile],
    *,
    token_counters: LLMTokenCounterRegistry | None = None,
    forbid_provider_auto_truncation: bool = True,
) -> LLMRequestPreparer:
    normalizers = LLMRequestNormalizerRegistry()
    registered: set[tuple[str, str]] = set()
    for profile in profiles:
        key = (profile.provider.casefold(), profile.normalizer_revision.casefold())
        if key in registered:
            continue
        normalizer = _known_normalizer(profile.normalizer_revision)
        if normalizer is None:
            continue
        normalizers.register(
            provider=profile.provider,
            revision=profile.normalizer_revision,
            normalizer=normalizer,
        )
        registered.add(key)
    return LLMRequestPreparer(
        normalizers=normalizers,
        token_counters=token_counters or LLMTokenCounterRegistry(),
        forbid_provider_auto_truncation=forbid_provider_auto_truncation,
    )


def _known_normalizer(revision: str):
    if revision == OPENAI_CHAT_NORMALIZER_REVISION:
        return OpenAICompatibleRequestNormalizer()
    if revision == CanonicalLLMRequestNormalizer().revision:
        return CanonicalLLMRequestNormalizer()
    return None
