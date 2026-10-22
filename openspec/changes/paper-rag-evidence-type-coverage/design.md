## Context

`BoundedRAGSessionController` 每一轮检索结束后，都会用 `RAGEvidenceCoverageGate` 检查 `spec.goal.required_evidence_types` 是否都已有对应证据。所需类型由 `AskPaperUseCase._required_evidence_types(intent)` 根据问题意图推出，候选片段的类型则由 `MetadataKeyEvidenceTypeResolver` 根据 `chunk_type` 和 `section_role` 推出。覆盖检查失败时，session 会先重新规划一次；重新规划后仍然失败，就以 `INSUFFICIENT_EVIDENCE` 结束，不调用回答模型。

## Goals / Non-Goals

**Goals**
- 片段支持的每一种证据类型都参与覆盖判定。
- 论文问答中，证据已被接受、只差类型时，交给回答模型判断。
- 放宽只针对类型这一项，lineage、来源质量、记忆相关性等检查不受影响。

**Non-Goals**
- 不调整意图分类，也不修改"意图 → 证据类型"映射表。在启发式之上再叠加启发式，只会换一批题出现错配。
- 不改变 Harness 框架的默认行为，也不改变论文分析路径。
- 不处理"是否题"（如 "Does this paper report X?"）中模型用 "No…" 作答、而没有使用拒答前缀的问题。这属于题库和协议的设计问题，见 Risks。

## Decisions

### D1 多类型解析

协议 `EvidenceTypeResolver` 新增方法 `resolve_types(metadata) -> tuple[str, ...]`。`MetadataKeyEvidenceTypeResolver` 先按映射表中 key 的顺序、再按 value 的顺序收集所有能映射上的类型并去重；`resolve()` 返回其中第一个。

`EvidenceCandidate` 新增字段 `evidence_types`，`__post_init__` 会保证主类型 `evidence_type` 排在第一位。该字段随 `to_evidence_pack`、`from_evidence_pack` 和 `to_dict` 一起序列化与还原。只有在 `evidence_type_source == "content_resolved"` 时才读取 pack 里的类型列表，避免把过时的类型带进来。

新增 `present_evidence_types()`，返回所有证据类型的并集。覆盖 gate、session 的 gap report 和 `bounded_document_rag` 的 gap 投影都使用这个函数。`bounded_document_rag` 投影后的 `ResearchEvidenceItem` 只保留主类型，因此需要由调用方先用投影前的候选算出类型并集，再传给 gap 投影。

### D2 advisory 覆盖策略

`source_policy["evidence_type_coverage"]` 只接受 `required` 或 `advisory`，其他取值会抛出 `HarnessValidationError`，默认值为 `required`。

`_evidence_type_gap_decision()` 只有在以下条件全部满足时，才返回 `ASSEMBLE_CONTEXT`：

- 策略为 `advisory`；
- 生成已启用，且存在 `answer_worker`；
- 已有被接受的证据；
- 在 `rag_evidence_coverage`、`rag_lineage`、`rag_source_quality`、`rag_memory_relevance` 这几项检查中，失败的只有 `rag_evidence_coverage`。

这个判定在两个位置调用：

1. **主循环**：重新规划预算用完之后。重新规划依然先执行，以保留"再检索一次"的机会。
2. **去重穷尽路径**：去重检查 gate 判定查询已经用尽时。这里用已接受的证据重新执行一次来源校验，再交给同一个判定函数。

满足条件时会记录 `rag_evidence_type_gap_accepted` 事件，内容包括缺失类型、已有类型、已接受证据 ID 和当前策略。这个事件会进入 replay 的决策时间线，并作为指标 `evidence_type_gap_accepted` 输出。

防止胡编并不依赖这个 gate，而是由以下几道检查保证：

- 回答模型的拒答协议；
- 引用完整性 gate；
- claim 覆盖 gate。

### D3 作用范围

`ResearchRAGPolicyBuilder.build_session_spec(evidence_type_coverage=...)` 默认使用 `required`，只有 `PaperRAGSession.run` 显式传入 `advisory`。

## Risks / Trade-offs

**应拒答题拒答率下降（100% → 81.8%，9/11 正确）**，原因不止一种：

- **abstain_002（癌症临床试验）**：改动前在类型 gate 处就被拦下，从未调用模型。现在进入了模型，网关返回了一段内容拒答（"this service does not support biological or life-science research"）。这段文本没有任何引用，被 claim 覆盖 gate 判为 `halted`。这是由 B 方案引起的，结果的处理方式也是正确的：没有引用的回答被拦下，没有被当作有效答案输出。
- **abstain_009 / abstain_011**：两次运行的上下文完全一致，也都没有走 B 路径。区别在于，模型这次用 "No. The passages … do not …" 作答，而不是使用拒答前缀，因此被判为错误。回答内容本身是正确的。这是模型输出的不稳定，再加上"是否题"和拒答协议之间存在歧义，与本次改动无关。

**单次运行的波动**：single_007 前后两次运行的上下文相同，这次给出了一个并非 gold 的引用作答。样本只有 42 题，单次运行无法排除这类波动。

## Migration Plan

先跑单测、真实语料回归，再用真实模型重跑业务题库，然后跑 smoke，最后提交。回滚方式是还原这个提交。不涉及持久化数据，`evidence_types` 是新增字段，旧 pack 在缺少该字段时会回退为只含主类型。
