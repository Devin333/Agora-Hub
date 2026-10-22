"""Environment selectors for Research language models.

Production composition (``ResearchRuntimeSettings``) and evaluation builders both
resolve the paper-answer model from these keys, in this precedence, so an evaluation
run generates with the same model the desktop Research route uses.

Evaluation judges use their own selectors so the judging model never silently
follows the generic ``OPENAI_MODEL`` or the answer model being judged.
"""

from __future__ import annotations

from collections.abc import Mapping

RESEARCH_ANSWER_BASE_URL_KEYS: tuple[str, ...] = (
    "NEWS_RESEARCH_LLM_BASE_URL",
    "NEWS_LLM_BASE_URL",
    "OPENAI_BASE_URL",
)
RESEARCH_ANSWER_MODEL_KEYS: tuple[str, ...] = (
    "NEWS_RESEARCH_LLM_MODEL",
    "NEWS_LLM_MODEL",
    "OPENAI_MODEL",
)
RESEARCH_ANSWER_API_KEY_ENV_KEYS: tuple[str, ...] = (
    "NEWS_RESEARCH_LLM_API_KEY_ENV",
    "NEWS_LLM_API_KEY_ENV",
)

EVAL_JUDGE_BASE_URL_KEYS: tuple[str, ...] = (
    "NEWS_RAG_EVAL_JUDGE_BASE_URL",
    "OPENAI_BASE_URL",
)
EVAL_JUDGE_MODEL_KEYS: tuple[str, ...] = ("NEWS_RAG_EVAL_JUDGE_MODEL",)
EVAL_JUDGE_API_KEY_ENV_KEYS: tuple[str, ...] = ("NEWS_RAG_EVAL_JUDGE_API_KEY_ENV",)


def first_configured(values: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first non-blank value among ``names``, or None."""
    for name in names:
        text = str(values.get(name) or "").strip()
        if text:
            return text
    return None


__all__ = [
    "EVAL_JUDGE_API_KEY_ENV_KEYS",
    "EVAL_JUDGE_BASE_URL_KEYS",
    "EVAL_JUDGE_MODEL_KEYS",
    "RESEARCH_ANSWER_API_KEY_ENV_KEYS",
    "RESEARCH_ANSWER_BASE_URL_KEYS",
    "RESEARCH_ANSWER_MODEL_KEYS",
    "first_configured",
]
