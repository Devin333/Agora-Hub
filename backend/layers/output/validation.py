"""Checks shared by every output-layer writer before data is stored."""

from __future__ import annotations

import math
from typing import Any

from framework.shared.json import ensure_strict_json


def optional_finite(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def optional_score(value: Any, field_name: str) -> float | None:
    """Report scores are fractions in [0, 1]; NaN would poison every average."""

    score = optional_finite(value, field_name)
    if score is not None and not 0.0 <= score <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return score


def ensure_storable_json(value: Any, field_name: str) -> None:
    """Strict JSON readers (Postgres JSONB, browsers) reject NaN and Infinity."""

    ensure_strict_json(value, field_name)
