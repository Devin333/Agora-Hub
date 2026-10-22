## ADDED Requirements

### Requirement: Multi-type evidence resolution

证据类型解析 MUST 返回片段支持的全部类型，主类型排在第一位。覆盖判定 MUST 使用所有已接受证据的类型并集。

#### Scenario: Passage with two roles
- **WHEN** 片段的 `section_role` 为 `["experiment", "method"]`
- **THEN** `resolve_types` MUST 返回 `("experiment", "method")`
- **AND** 要求 `method` 的覆盖检查 MUST 通过

#### Scenario: Types survive serialization
- **WHEN** 内容解析出多类型的候选经 `to_evidence_pack` 和 `from_evidence_pack` 往返
- **THEN** `evidence_types` MUST 保持不变

#### Scenario: Unresolved candidates do not inherit stale types
- **WHEN** pack 的 `evidence_type_source` 不是 `content_resolved`
- **THEN** 候选的 `evidence_types` MUST 只包含请求的类型

### Requirement: Advisory evidence-type coverage

`evidence_type_coverage` 为 `advisory` 时，如果来源校验中失败的只有证据类型覆盖，并且已有被接受的证据、生成已启用、存在回答 worker，那么在重新规划预算用完后，session MUST 进入上下文组装并调用回答模型，同时 MUST 记录 `rag_evidence_type_gap_accepted` 事件。未满足其中任一条件时，行为 MUST 与 `required` 一致。

#### Scenario: Type gap over accepted evidence
- **WHEN** 策略为 `advisory`，所需类型为 `limitation`，已接受证据只有 `method`
- **THEN** 回答模型 MUST 被调用
- **AND** 指标 `evidence_type_gap_accepted` MUST 为 true

#### Scenario: Required mode is unchanged
- **WHEN** 未设置 `evidence_type_coverage`
- **THEN** 同样的类型缺口 MUST 以 `insufficient_evidence` 结束，且 MUST NOT 调用回答模型

#### Scenario: Other gates still block
- **WHEN** 策略为 `advisory`，但 `rag_lineage` 检查失败
- **THEN** session MUST NOT 调用回答模型

#### Scenario: Exhausted queries
- **WHEN** 策略为 `advisory`，查询因去重而用尽，且已接受证据只差类型
- **THEN** 回答模型 MUST 被调用

#### Scenario: Unknown mode
- **WHEN** `evidence_type_coverage` 不是 `required` 或 `advisory`
- **THEN** MUST 抛出校验错误

### Requirement: Paper Q&A scope

只有论文问答 session（`PaperRAGSession`）MUST 使用 `advisory`。`ResearchRAGPolicyBuilder` 的默认值和 Harness 框架的默认值 MUST 为 `required`。

#### Scenario: Default builder
- **WHEN** 调用 `build_session_spec` 时未指定 `evidence_type_coverage`
- **THEN** `source_policy["evidence_type_coverage"]` MUST 为 `required`

#### Scenario: Paper Q&A
- **WHEN** `PaperRAGSession.run` 构建 session spec
- **THEN** `source_policy["evidence_type_coverage"]` MUST 为 `advisory`
