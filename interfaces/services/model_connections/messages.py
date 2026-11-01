"""User-facing text for a model call that has no usable connection."""

from __future__ import annotations

from typing import Any

from framework.llm.clients.endpoint import LLMCredentialUnavailableError
from interfaces.services.model_connections.purposes import purpose_label

# The connection exists but its key must be entered again.
_KEY_AGAIN_CODES = frozenset({"model_connection_key_rotated", "model_connection_key_missing"})


def connection_gap_message(error: LLMCredentialUnavailableError) -> str:
    label = purpose_label(error.purpose) if error.purpose else ""
    # "the connection assigned to <purpose>", or the account's connections in general.
    connection = f"“{label}”所用的模型连接" if label else "模型连接"
    if error.code == "model_connection_key_rotated":
        return f"服务的加密密钥已更换，请在设置里重新填写{connection}的 API Key。"
    if error.code in _KEY_AGAIN_CODES:
        return f"请在设置里重新填写{connection}的 API Key。"
    if error.code == "model_connection_withdrawn":
        return f"{connection}的模型已被管理员下线，请在设置里改用其他模型。"
    if error.code == "model_connection_window_too_small":
        return f"{connection}的上下文窗口不够大，请在设置里换一个窗口更大的模型。"
    if error.code == "model_connection_structured_output_required":
        return f"{connection}不能返回 JSON 对象，请在设置里换一个支持结构化输出的模型。"
    if label:
        return f"请先在设置里为“{label}”配置可用的模型连接。"
    return "请先在设置里配置可用的模型连接。"


def connection_gap_details(error: LLMCredentialUnavailableError) -> dict[str, Any]:
    if not error.purpose:
        return {}
    return {"purpose": error.purpose, "purposeLabel": purpose_label(error.purpose)}


__all__ = ["connection_gap_details", "connection_gap_message"]
