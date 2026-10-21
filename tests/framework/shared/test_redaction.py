from __future__ import annotations

import re
import subprocess
import sys
from itertools import product
from pathlib import Path
from textwrap import dedent

import pytest

from framework.shared import (
    REDACTED_VALUE,
    RedactionRule,
    Redactor,
    contains_redacted_value,
    redact_sensitive_values,
)


def test_redact_sensitive_values_handles_nested_structures() -> None:
    payload = redact_sensitive_values(
        {
            "api_key": "secret",
            "nested": [{"password": "hidden"}, {"safe": "visible"}],
            "message": "Bearer abcdef1234567890",
        }
    )

    assert payload == {
        "api_key": REDACTED_VALUE,
        "nested": [{"password": REDACTED_VALUE}, {"safe": "visible"}],
        "message": REDACTED_VALUE,
    }
    assert contains_redacted_value(payload) is True


def test_redactor_supports_custom_rules_and_dsn_strings() -> None:
    redactor = Redactor([RedactionRule(key_tokens=("auth",), replacement="[hidden]")])
    dsn = "postgresql://user:pass@localhost/news"

    assert redactor.redact({"authorization": "secret"}) == {"authorization": "[hidden]"}
    assert redactor.redact({"message": f"connect {dsn}"}) == {
        "message": "connect postgresql://[hidden]@localhost/news"
    }


def test_redaction_rule_matches_case_insensitive_key_tokens() -> None:
    rule = RedactionRule(key_tokens=("api_key",))

    assert rule.matches_key("X-API-Key")
    assert not rule.matches_key("topic")


def test_secret_pattern_preserves_normal_text_and_requires_token_boundary() -> None:
    payload = redact_sensitive_values(
        {
            "task": "task-plan-helper",
            "risk": "risk-assessment",
            "title": "desk-lamp-holder",
            "message": "prefixsk-1234567890abcdef and sk-1234567890abcdef suffix",
        }
    )

    assert payload["task"] == "task-plan-helper"
    assert payload["risk"] == "risk-assessment"
    assert payload["title"] == "desk-lamp-holder"
    assert payload["message"] == "prefixsk-1234567890abcdef and ***REDACTED*** suffix"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("postgresql://user:pass@host/db", "postgresql://[hidden]@host/db"),
        ("X+1.-://user:pass:more@host", "X+1.-://[hidden]@host"),
        ("1http://user:pass@host", "1http://[hidden]@host"),
        ("invalid!http://user:pass@host", "invalid!http://[hidden]@host"),
        ("1://user:pass@host", "1://user:pass@host"),
        ("+.-://user:pass@host", "+.-://user:pass@host"),
        ("http!://user:pass@host", "http!://user:pass@host"),
        ("http:///user:pass@host", "http:///user:pass@host"),
        ("http://:pass@host", "http://:pass@host"),
        ("http://user:@host", "http://user:@host"),
        ("http://user:pass/other@host", "http://user:pass/other@host"),
        ("http://user:pass?other@host", "http://user:pass?other@host"),
        ("http://user:pass#other@host", "http://user:pass#other@host"),
        ("http://user:pass\u2003other@host", "http://user:pass\u2003other@host"),
        ("http://user\u2003name:pass@host", "http://user\u2003name:pass@host"),
        ("http://user:pass", "http://user:pass"),
        (r"http://u\ser:pa\ss@host", "http://[hidden]@host"),
        ("http://user:pass@@host", "http://[hidden]@@host"),
        (
            "first http://u:p@host; next ftp://a:b@server/path end",
            "first http://[hidden]@host; next ftp://[hidden]@server/path end",
        ),
        (
            "http://bad:password/next://user:pass@host",
            "http://bad:password/next://[hidden]@host",
        ),
    ],
)
def test_uri_credentials_preserve_redaction_grammar(value: str, expected: str) -> None:
    redactor = Redactor([RedactionRule(replacement="[hidden]")])

    assert redactor.redact(value) == expected


@pytest.mark.parametrize("letter", ["i", "I", "\u0130", "\u0131", "\u017f", "\u212a"])
def test_uri_scheme_preserves_unicode_ignorecase_letters(letter: str) -> None:
    assert redact_sensitive_values(f"{letter}://user:pass@host") == (
        f"{letter}://{REDACTED_VALUE}@host"
    )


def test_uri_credentials_match_legacy_regex_semantic_oracle() -> None:
    # Keep the old grammar as a small-input oracle, never as a production scan.
    oracle = re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s:@?#]+):([^@\s/?#]+)@", re.IGNORECASE)
    redactor = Redactor([RedactionRule(replacement="[hidden]")])
    fragments = (
        "http://",
        "1://",
        "a:///",
        "X+1.-://",
        "\u0130://",
        "user:pass@host",
        "u:p:q@host",
        "u\\x:p\\y@host",
        "1+.-",
        "!",
        "\u2003",
        "/?#",
        "@",
        "",
    )
    for parts in product(fragments, repeat=3):
        value = "".join(parts)
        expected = oracle.sub(lambda match: f"{match.group(1)}[hidden]@", value)
        assert redactor.redact(value) == expected, repr(value)


def test_uri_redaction_adversarial_inputs_finish_in_bounded_subprocess() -> None:
    # A process timeout catches pathological complexity without microsecond limits.
    # Include :// inputs too: a no-delimiter fast path alone is not sufficient.
    script = dedent(
        """
        from framework.shared import REDACTED_VALUE, redact_sensitive_values

        unchanged = [
            "x" * (48 * 1024),
            "x" * (256 * 1024),
            "x" * (256 * 1024) + ":///",
            "9" * (256 * 1024) + "://user:password@host",
            "+.-" * (64 * 1024) + "://user:password@host",
            "http!://user:password@host " * 4096,
            "a:///" * (32 * 1024),
            ("a" * 64 + ":///") * 4096,
            "a://" + "u" * (128 * 1024) + ":" + "p" * (128 * 1024),
            "a://u:" + "p:" * (128 * 1024),
        ]
        for value in unchanged:
            assert redact_sensitive_values(value) == value

        value = "9" * (128 * 1024) + "a://" + "u" * (128 * 1024)
        value += ":" + "p:" * (128 * 1024) + "@host/path"
        assert redact_sensitive_values(value) == (
            "9" * (128 * 1024) + "a://" + REDACTED_VALUE + "@host/path"
        )
        value = "x" * (48 * 1024) + " http://user:pass@host " + "x" * (48 * 1024)
        assert redact_sensitive_values(value) == value.replace("user:pass", REDACTED_VALUE)
        value = "a://u:p@host " * 10000
        assert redact_sensitive_values(value) == ("a://" + REDACTED_VALUE + "@host ") * 10000
        print("bounded URI redaction and semantic checks passed")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "bounded URI redaction and semantic checks passed" in result.stdout
