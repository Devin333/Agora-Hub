"""What each model purpose sends at most, so connections are admitted against it.

Admission compares a connection's context window with the largest prompt and
reply one call of a purpose can produce. The numbers come from the constants
the purpose's own code enforces (passage caps, batch sizes, prompt limits,
reply budgets), not from a guess, so a call admitted here never needs its
window narrowed at call time.

Sizes are measured in characters. A token never takes more than one character,
so a character count is an upper bound on the tokens of any text, CJK included.
That makes every requirement a safe ceiling rather than an estimate.
"""

from __future__ import annotations

from backend.research.domain.personal_memory import MAX_CONTENT_LENGTH, MAX_SNAPSHOT_RECORDS
from backend.research.rag.retrieval.paper_answer_generator import build_answer_system_prompt
from backend.research.rag.retrieval.paper_evidence_summarizer import (
    DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS,
    SUMMARY_BATCH_MAX_INPUT_CHARS,
)
from backend.research.rag.retrieval.paper_section_navigator import (
    DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
    MAX_NAVIGATION_INPUT_CHARS,
)
from interfaces.composition.research_reader_answer import READER_ANSWER_MAX_OUTPUT_TOKENS
from interfaces.composition.research_settings import ResearchRuntimeSettings
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement

# Longest research question the desktop route accepts.
MAX_QUESTION_CHARS = 4_000
# JSON keys, ids, categories and revisions around each personal memory record.
_MEMORY_RECORD_JSON_CHARS = 512
# Quotes and backslashes are escaped in JSON, so their characters can double.
_JSON_ESCAPE_FACTOR = 2


# Measured from build_answer_system_prompt: the sentence framing the memory block
# (366 characters) plus the JSON wrapper around an empty record list (364).
_MEMORY_BLOCK_FIXED_CHARS = 366 + 364


def personal_memory_allowance_chars() -> int:
    """Worst case size of the personal memory block the answer prompt can carry."""
    per_record = MAX_CONTENT_LENGTH * _JSON_ESCAPE_FACTOR + _MEMORY_RECORD_JSON_CHARS
    return _MEMORY_BLOCK_FIXED_CHARS + MAX_SNAPSHOT_RECORDS * per_record


def answer_prompt_overhead_chars() -> int:
    """Characters of answer instructions that surround the research context."""
    return len(build_answer_system_prompt(""))


def model_purpose_requirements(settings: ResearchRuntimeSettings) -> dict[ModelPurpose, PurposeRequirement]:
    """Requirement for every purpose under the configured research settings.

    Requires ``settings.llm.max_input_tokens`` to be resolved: in the hosted
    profile it comes from the configured input budget, so every connection is
    checked against the same window the research pipeline plans for.
    """
    llm = settings.llm
    if llm.max_input_tokens is None:
        raise ValueError("model purpose requirements need a resolved input window")
    answer_input = (
        settings.rag.max_context_tokens
        + answer_prompt_overhead_chars()
        + personal_memory_allowance_chars()
        + MAX_QUESTION_CHARS
    )
    return {
        ModelPurpose.ANSWER: PurposeRequirement(
            input_tokens=answer_input,
            output_tokens=READER_ANSWER_MAX_OUTPUT_TOKENS,
        ),
        ModelPurpose.EVIDENCE_SUMMARY: PurposeRequirement(
            input_tokens=SUMMARY_BATCH_MAX_INPUT_CHARS,
            output_tokens=DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS,
        ),
        ModelPurpose.SECTION_NAVIGATION: PurposeRequirement(
            input_tokens=MAX_NAVIGATION_INPUT_CHARS,
            output_tokens=DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
        ),
        # Candidate generation is the one purpose whose replies must follow a
        # schema, so it alone needs a connection that returns JSON objects.
        ModelPurpose.CANDIDATE_GENERATION: PurposeRequirement(
            input_tokens=llm.max_input_tokens,
            output_tokens=llm.max_output_tokens,
            structured_output=True,
        ),
    }


__all__ = [
    "MAX_QUESTION_CHARS",
    "answer_prompt_overhead_chars",
    "model_purpose_requirements",
    "personal_memory_allowance_chars",
]
