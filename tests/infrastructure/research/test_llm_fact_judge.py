from __future__ import annotations

import asyncio
import json

import pytest

from backend.research.ports.fact_entailment_judge import (
    EntailmentLabel,
    EntailmentTarget,
    FactEntailmentJudgePort,
    FactEntailmentRequest,
    FactJudgeError,
    JudgedDimensionOutcome,
    JudgeTargetKind,
)
from framework.llm.clients.fake import FakeLLMClient
from infrastructure.research.llm_fact_judge import FACT_JUDGE_PROMPT_VERSION, LLMFactEntailmentJudge


def _request(kind: JudgeTargetKind = JudgeTargetKind.REFERENCE) -> FactEntailmentRequest:
    return FactEntailmentRequest(
        request_id="claim-1::reference",
        question="What accuracy does Model A reach?",
        claim_id="claim-1",
        claim_text="Model A reaches 95% accuracy under seed 7.",
        target_kind=kind,
        targets=(EntailmentTarget(target_id="ref-1", text="Model A reaches 95.3% accuracy.", supporting_text=("Table 2",)),),
    )


def _payload(label: str = "entailed", *, target_id: str = "ref-1", name: str | None = "seed") -> str:
    return json.dumps({"judgments": [{
        "target_id": target_id,
        "label": label,
        "dimensions": [
            {"dimension": "numeric_value", "name": "ignored", "outcome": "match"},
            {"dimension": "condition", "name": name, "outcome": "not_stated"},
        ],
        "rationale": " rounds to 95% ",
    }]})


def _judge(client) -> LLMFactEntailmentJudge:
    return LLMFactEntailmentJudge(client, provider="openai", model_id="gpt-5.4-mini")


def test_judge_satisfies_port_and_parses_reference_dimensions() -> None:
    client = FakeLLMClient([_payload()])
    judge = _judge(client)
    assert isinstance(judge, FactEntailmentJudgePort)
    assert judge.identity.prompt_version == FACT_JUDGE_PROMPT_VERSION
    judgment = asyncio.run(judge.judge(_request()))
    item = judgment.judgments[0]
    assert item.label is EntailmentLabel.ENTAILED
    assert item.dimensions[0].name is None
    assert item.dimensions[1].outcome is JudgedDimensionOutcome.NOT_STATED
    assert item.rationale == "rounds to 95%"
    sent = json.loads(client.requests[0].messages[1]["content"])
    assert sent["TARGET_KIND"] == "reference"
    assert sent["TARGETS"][0]["SUPPORTING_TEXT"] == ["Table 2"]
    assert client.requests[0].temperature == 0


def test_evidence_and_neutral_judgments_carry_no_dimensions() -> None:
    evidence = asyncio.run(_judge(FakeLLMClient([_payload()])).judge(_request(JudgeTargetKind.EVIDENCE)))
    neutral = asyncio.run(_judge(FakeLLMClient([_payload("neutral")])).judge(_request()))
    assert evidence.judgments[0].dimensions == ()
    assert neutral.judgments[0].dimensions == ()


def test_bad_output_is_a_port_error() -> None:
    with pytest.raises(FactJudgeError, match="strict JSON"):
        asyncio.run(_judge(FakeLLMClient(["{"])).judge(_request()))
    with pytest.raises(FactJudgeError, match="contract"):
        asyncio.run(_judge(FakeLLMClient([_payload(name=None)])).judge(_request()))
