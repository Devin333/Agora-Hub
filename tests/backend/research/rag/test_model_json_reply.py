from __future__ import annotations

import pytest

from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.rag.retrieval.model_json_reply import as_int, parse_json_object_reply


@pytest.mark.parametrize("text", ['{"a": 1}', '  ```json\n{"a": 1}\n```  ', '```\n{"a": 1}\n```'])
def test_parses_bare_or_fenced_objects(text: str) -> None:
    assert parse_json_object_reply(text, purpose="test") == {"a": 1}


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '"text"', '```json\n{"a": 1}\n``` trailing'])
def test_rejects_anything_else_as_malformed_output(text: str) -> None:
    with pytest.raises(PaperAnswerModelError) as excinfo:
        parse_json_object_reply(text, purpose="test")

    assert excinfo.value.code == "malformed_output"
    assert "test reply" in str(excinfo.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3), (3.0, 3), (" 7 ", 7), ("-2", -2), (True, None), (2.5, None), ("x", None), (None, None)],
)
def test_as_int_accepts_only_integral_scalars(value, expected) -> None:
    assert as_int(value) == expected
