from __future__ import annotations

import json

import scripts.desktop_app.cli as cli_module
from scripts.desktop_app.protocol import MAX_PROTOCOL_BYTES, CommandResponse, project_operation
from scripts.local_desktop.common import OperationResult


def test_operation_projection_uses_real_core_fields_and_redacts_secrets() -> None:
    result = OperationResult(
        True,
        "start",
        "READY",
        "desktop_started",
        "ready",
        {"nested": {"api_key": "do-not-leak"}, "origin": "http://127.0.0.1:3120"},
    )

    payload = project_operation(
        result,
        command="start",
        canonical_origin="http://127.0.0.1:3120",
        api_base_url="http://127.0.0.1:8120",
        instance_key="local-desktop:http://127.0.0.1:3120",
    ).to_dict()

    assert payload["ok"] is True
    assert payload["command"] == "start"
    assert payload["state"] == "READY"
    assert payload["code"] == "desktop_started"
    assert payload["data"]["nested"]["api_key"] == "***REDACTED***"
    assert "action" not in payload
    assert "reason_code" not in payload


def test_protocol_emitter_falls_back_to_one_bounded_json_line(capfd) -> None:
    response = CommandResponse(
        True,
        "inventory",
        "READY",
        "desktop_inventory_resolved",
        "resolved",
        {
            "canonical_origin": "http://127.0.0.1:3120",
            "api_base_url": "http://127.0.0.1:8120",
            "instance_key": "local-desktop:http://127.0.0.1:3120",
            "items_a": [f"safe-a-{index}-" + ("x" * 2000) for index in range(100)],
            "items_b": [f"safe-b-{index}-" + ("x" * 2000) for index in range(100)],
        },
    )

    cli_module._emit(response)

    captured = capfd.readouterr()
    encoded = captured.out.encode("utf-8")
    assert captured.err == ""
    assert encoded.count(b"\n") == 1
    assert len(encoded) <= MAX_PROTOCOL_BYTES + 1
    payload = json.loads(captured.out)
    assert payload["data"]["truncated"] is True
    assert payload["data"]["instance_key"] == "local-desktop:http://127.0.0.1:3120"


def test_protocol_restores_only_anchored_confirmation_and_public_credential_bool() -> None:
    token = "desktop-config:" + "a" * 32 + ":" + "b" * 16
    response = CommandResponse(
        True,
        "config-preview",
        "CONFIRMATION_REQUIRED",
        "desktop_runtime_config_preview_ready",
        "preview",
        {
            "confirmation_token": token,
            "other_token": token,
            "configuration": {"research": {"llm": {"credential_configured": True, "credential_ref": "must-redact"}}},
        },
    ).to_dict()

    assert response["data"]["confirmation_token"] == token
    assert response["data"]["other_token"] == "***REDACTED***"
    llm = response["data"]["configuration"]["research"]["llm"]
    assert llm["credential_configured"] is True
    assert llm["credential_ref"] == "***REDACTED***"


def test_protocol_does_not_restore_malformed_confirmation_token() -> None:
    payload = CommandResponse(True, "config-preview", "READY", "ok", "ok", {"confirmation_token": "desktop-config:not-safe"}).to_dict()
    assert payload["data"]["confirmation_token"] == "***REDACTED***"


def test_protocol_preserves_valid_public_config_urls_above_legacy_string_limit() -> None:
    long_url = "https://api.example.test/" + "a" * 1050
    response = CommandResponse(
        True,
        "config-preview",
        "CONFIRMATION_REQUIRED",
        "desktop_runtime_config_preview_ready",
        "preview",
        {
            "configuration": {
                "schema": "newsroom.desktop-runtime-settings/v1",
                "revision": 1,
                "research": {
                    "llm": {"base_url": long_url, "credential_configured": None},
                    "source": {"api_url": long_url},
                },
            }
        },
    ).to_dict()

    projected = response["data"]["configuration"]["research"]
    assert projected["llm"]["base_url"] == long_url
    assert projected["source"]["api_url"] == long_url
    assert projected["llm"]["credential_configured"] is None
    assert "<truncated>" not in json.dumps(projected)


def test_clear_protocol_restores_only_bounded_internal_handoff_fields() -> None:
    preview_id = "p" * 43
    operation_id = "a" * 32
    capability = "c" * 64
    preview = CommandResponse(True, "data-clear-preview", "CONFIRMATION_REQUIRED", "ok", "ok", {"previewId": preview_id}).to_dict()
    apply = CommandResponse(True, "data-clear-apply", "PREPARED", "ok", "ok", {"operationId": operation_id, "finalizerCapability": capability, "exitRequired": True}).to_dict()

    assert preview["data"]["previewId"] == preview_id
    assert apply["data"]["operationId"] == operation_id
    assert apply["data"]["finalizerCapability"] == capability
    assert apply["data"]["exitRequired"] is True
