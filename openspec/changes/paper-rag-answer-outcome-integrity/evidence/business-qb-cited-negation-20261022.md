# 是否题"明确否定 + 引用"规则重新打分（2026-10-22）

- 数据：`.newsroom/eval/business_question_bank_v1_live_20261022T202201Z/answers.jsonl`（未入库）。仅重新打分，没有重新调用模型。
- 规则：期望拒答的是否题，回答以 "No" 开头且至少引用一个片段，判为拒答正确（`cited_negation`）。

| 题 | 结果 | 旧规则 | 新规则 | 依据 |
|---|---|---|---|---|
| abstain_009 | answered："No. The provided passage … does not disclose a private API key. [1]" | 错 | 对 | cited_negation |
| abstain_011 | answered："No. The passages discuss … not restaurant menus … [1][2][3]" | 错 | 对 | cited_negation |
| abstain_002 | halted（网关内容拒答，无引用） | 系统失败 | 系统失败 | 不计入分母 |
| 其余 9 题 | abstained | 对 | 对 | refusal |

拒答准确率：9/11 → 11/11。

## 已知限制：网关对生命科学类问题的内容拒答

当前网关会对生命科学和生物医学类问题直接返回拒答文本，例如 "this service does not support biological or life-science research"。这段文本不带引用，会被 claim 覆盖 gate 判为 `halted`，计入系统失败。这一限制目前无法规避。阶段 1 在 QASPER 上评测时，需要把生物医学类论文单独分层统计，或在报告中注明这类论文的系统失败来自网关。
