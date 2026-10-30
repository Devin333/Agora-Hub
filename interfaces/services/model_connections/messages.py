"""User-facing text for a model call that has no usable connection."""

from __future__ import annotations

from typing import Any

from framework.llm.clients.endpoint import LLMCredentialUnavailableError
from interfaces.services.model_connections.purposes import purpose_label

KEY_ROTATED_CODES = frozenset({"model_connection_key_rotated", "model_credential_key_rotated"})


def connection_gap_message(error: LLMCredentialUnavailableError) -> str:
    label = purpose_label(error.purpose) if error.purpose else ""
    if error.code in KEY_ROTATED_CODES:
        if label:
            return f"服务的加密密钥已更换，请在设置里重新填写“{label}”所用模型连接的 API Key。"
        return "服务的加密密钥已更换，请在设置里重新填写模型连接的 API Key。"
    if label:
        return f"请先在设置里为“{label}”配置可用的模型连接。"
    return "请先在设置里配置可用的模型连接。"


def connection_gap_details(error: LLMCredentialUnavailableError) -> dict[str, Any]:
    if not error.purpose:
        return {}
    return {"purpose": error.purpose, "purposeLabel": purpose_label(error.purpose)}


__all__ = ["KEY_ROTATED_CODES", "connection_gap_details", "connection_gap_message"]
