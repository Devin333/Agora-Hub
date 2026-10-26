"""Parse a structured JSON reply from a paper answer model transport.

Retrieval-side model helpers (evidence summarizer, section navigator) ask for one
JSON object and accept it bare or inside a single Markdown code fence. Anything
else is ``malformed_output`` so the caller can retry or degrade explicitly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from backend.research.ports.paper_answer_model import PaperAnswerModelError

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_json_object_reply(text: str, *, purpose: str) -> Mapping[str, Any]:
    stripped = str(text or "").strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PaperAnswerModelError(
            "malformed_output", f"{purpose} reply is not JSON",
            details={"output_excerpt": stripped},
        ) from exc
    if not isinstance(payload, Mapping):
        raise PaperAnswerModelError(
            "malformed_output", f"{purpose} reply is not a JSON object",
            details={"output_excerpt": stripped},
        )
    return payload


def as_int(value: Any) -> int | None:
    """Integer value of a JSON scalar, accepting integral floats and digit strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


__all__ = ["as_int", "parse_json_object_reply"]
