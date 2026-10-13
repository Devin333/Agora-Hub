from __future__ import annotations

from pathlib import Path

import pytest

from framework.shared.local_desktop import (
    CapabilityProjection,
    CapabilityState,
    LocalDesktopConfigError,
    resolve_local_desktop_config,
    validate_lifecycle_timeout,
)


def test_local_desktop_config_precedence_and_environment_contract(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        root_env={
            "NEWSROOM_DEPLOYMENT_PROFILE": "local-desktop",
            "NEWSROOM_DESKTOP_FRONTEND_PORT": "3100",
            "NEWSROOM_CANONICAL_ORIGIN": "http://127.0.0.1:3100",
        },
        runtime_config={"frontend_port": 3200, "canonical_origin": "http://127.0.0.1:3200"},
        cli_overrides={"frontend_port": 3300, "canonical_origin": "http://127.0.0.1:3300"},
    )

    assert config.frontend_port == 3300
    assert config.canonical_origin == "http://127.0.0.1:3300"
    assert config.as_environment()["NEWSROOM_CANONICAL_ORIGIN"] == config.canonical_origin
    assert config.as_environment()["NEWSROOM_DESKTOP_CANONICAL_ORIGIN"] == config.canonical_origin
    assert config.as_environment()["NEWSROOM_DESKTOP_INSTANCE_KEY"] == config.instance_key
    assert config.as_environment()["NEWS_RESEARCH_PARSER_BACKENDS"] == "pymupdf"
    assert config.as_environment()["NEWSROOM_PDF_PARSER_CASCADE"] == "pymupdf"


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.2", "192.168.1.8", "::1", "localhost.example"])
def test_local_desktop_rejects_non_ipv4_loopback(tmp_path: Path, host: str) -> None:
    with pytest.raises(LocalDesktopConfigError, match="loopback") as raised:
        resolve_local_desktop_config(project_root=tmp_path, cli_overrides={"frontend_host": host})
    assert raised.value.reason_code == "desktop_bind_not_loopback"


def test_local_desktop_rejects_conflicting_origin_aliases(tmp_path: Path) -> None:
    with pytest.raises(LocalDesktopConfigError) as raised:
        resolve_local_desktop_config(
            project_root=tmp_path,
            root_env={
                "NEWSROOM_CANONICAL_ORIGIN": "http://127.0.0.1:3000",
                "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": "http://127.0.0.1:3001",
            },
        )
    assert raised.value.reason_code == "desktop_config_env_conflict"


def test_local_desktop_rejects_unknown_runtime_field(tmp_path: Path) -> None:
    with pytest.raises(LocalDesktopConfigError) as raised:
        resolve_local_desktop_config(project_root=tmp_path, runtime_config={"bind_all": True})
    assert raised.value.reason_code == "desktop_config_unknown_field"


def test_capability_projection_preserves_three_distinct_states() -> None:
    item = CapabilityProjection(
        "analysis",
        True,
        CapabilityState.YES,
        CapabilityState.YES,
        CapabilityState.UNKNOWN,
        reason_code="business_flow_not_verified",
    )
    assert item.to_dict() == {
        "capability_id": "analysis",
        "required": True,
        "configured": "yes",
        "reachable": "yes",
        "verified": "unknown",
        "available": False,
        "reason_code": "business_flow_not_verified",
        "checked_at": None,
        "remediation": [],
        "details": {},
    }


def test_capability_projects_to_strict_frontend_boolean_contract() -> None:
    item = CapabilityProjection("analysis", True, CapabilityState.YES, CapabilityState.UNKNOWN, CapabilityState.UNKNOWN, reason_code="not_checked")

    assert item.to_frontend_dict(label="Analysis") == {
        "id": "analysis",
        "label": "Analysis",
        "configured": True,
        "reachable": False,
        "verified": False,
        "reasonCode": "not_checked",
    }


def test_capability_cannot_be_verified_without_reachability() -> None:
    with pytest.raises(ValueError, match="reachable"):
        CapabilityProjection("analysis", True, CapabilityState.YES, CapabilityState.UNKNOWN, CapabilityState.YES)




def test_capability_redaction_preserves_non_secret_probe_budgets() -> None:
    item = CapabilityProjection(
        "llm",
        False,
        CapabilityState.YES,
        details={"max_input_tokens": 128, "max_output_tokens": 16, "max_cost_usd": 0.01, "session_token": "secret"},
    )

    details = item.to_dict()["details"]
    assert details["max_input_tokens"] == 128
    assert details["max_output_tokens"] == 16
    assert details["max_cost_usd"] == 0.01
    assert details["session_token"] == "***REDACTED***"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301, True])
def test_lifecycle_timeout_validator_rejects_non_positive_non_finite_and_unbounded_values(value) -> None:
    with pytest.raises(ValueError, match="finite number"):
        validate_lifecycle_timeout(value, field_name="readiness_timeout")


def test_lifecycle_timeout_validator_returns_normalized_float() -> None:
    assert validate_lifecycle_timeout("30", field_name="drain_timeout") == 30.0
