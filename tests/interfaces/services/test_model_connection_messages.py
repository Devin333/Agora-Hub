from __future__ import annotations

import pytest

from framework.llm.clients import LLMCredentialUnavailableError
from interfaces.services.model_connections.messages import connection_gap_details, connection_gap_message


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("model_connection_required", "请先在设置里为“回答”配置可用的模型连接。"),
        ("model_connection_key_rotated", "服务的加密密钥已更换，请在设置里重新填写“回答”所用的模型连接的 API Key。"),
        ("model_connection_key_missing", "请在设置里重新填写“回答”所用的模型连接的 API Key。"),
        ("model_connection_withdrawn", "“回答”所用的模型连接的模型已被管理员下线，请在设置里改用其他模型。"),
        ("model_connection_window_too_small", "“回答”所用的模型连接的上下文窗口不够大，请在设置里换一个窗口更大的模型。"),
        ("model_connection_structured_output_required", "“回答”所用的模型连接不能返回 JSON 对象，请在设置里换一个支持结构化输出的模型。"),
    ),
)
def test_every_connection_gap_names_the_purpose_and_the_fix(code: str, expected: str) -> None:
    error = LLMCredentialUnavailableError(code, "internal", purpose="answer")

    assert connection_gap_message(error) == expected
    assert connection_gap_details(error) == {"purpose": "answer", "purposeLabel": "回答"}


def test_a_gap_without_a_purpose_still_points_to_the_settings() -> None:
    error = LLMCredentialUnavailableError("model_connection_key_rotated", "internal")

    assert connection_gap_message(error) == "服务的加密密钥已更换，请在设置里重新填写模型连接的 API Key。"
    assert connection_gap_details(error) == {}
