## Why

上下文预算修复之后，业务题库中仍有 6 道应回答的单篇题以 `insufficient_evidence` 结束，模型一次都没有被调用。逐题审计发现，这 6 道题的 gold 片段都已经被检索到，也通过了证据校验，真正拦下它们的是证据类型覆盖 gate：系统根据问题意图推出一个要求的证据类型，gold 片段被判成了另一种类型，两者对不上。

根因有两层：

1. **类型解析只取第一个角色**。`MetadataKeyEvidenceTypeResolver.resolve` 遇到第一个匹配就返回，同时具有 experiment 和 method 角色的片段只会被判成 experiment。评测语料 35 个片段中有 16 个带多个角色。single_029、single_030 就是因此被拦下的。
2. **"意图 → 证据类型"是关键词启发式**。例如问题里出现 "result"，就要求 experiment 类型；而"实验跑了几次"这类问题，答案其实写在 Training Details 一节。single_011、012、014、021 属于这种情况。在模型看到证据之前，就因为一个启发式规则判定"证据不足"，造成了过度拒答。

## What Changes

- 类型解析器新增 `resolve_types()`，返回片段支持的全部证据类型，主类型排在第一位。`EvidenceCandidate.evidence_types` 记录这些类型，覆盖判定取所有已接受证据的类型并集。
- `RAGExecutionPolicy.evidence_type_coverage` 新增两种取值：`required`（默认，行为与现在一致）和 `advisory`。在 `advisory` 下，满足以下全部条件时，session 会把上下文交给回答模型，由它按拒答协议判断证据是否充分，并记录 `rag_evidence_type_gap_accepted` 事件：
  - 只有证据类型缺口；
  - 已有被接受的证据；
  - 其余 gate 都通过；
  - 生成已启用，且存在回答 worker。
- 只有 `PaperRAGSession`（论文问答）使用 `advisory`；`single_paper_runtime` 的论文分析路径和 Harness 框架的默认值保持 `required`。
- `source_verifier` 改为用 `dataclasses.replace` 复制候选，新增字段不会再被逐字段重建时丢掉。
- 指标 `evidence_type_gap_accepted` 写入 session 指标和评测逐题结果。

## Capabilities

### New Capabilities

- `paper-rag-evidence-type-coverage`：多类型证据解析，以及论文问答的证据类型覆盖策略。

### Modified Capabilities

- 无已归档的 spec 需要修改。

## Impact

- `framework/harness/rag/`：`evidence_typing.py`、`models.py`、`kernel_evidence_adapter.py`、`gates.py`、`policy.py`、`session.py`、`metrics.py`、`replay.py`、`source_verifier.py`
- `backend/research/services/rag_policy.py`、`backend/research/application/paper_rag_session.py`、`bounded_document_rag.py`
- 评测：`evidence_eval_runner.py`、`business_answer_eval.py`
- 用户可见：桌面问答中，证据已经找到但类型不符的问题会交给模型回答，不再直接判定为"证据不足"
