## ADDED Requirements

### Requirement: Shared context budget

回答生成 MUST 让所有被选中的片段共用一个总字符预算（`max_context_chars`），MUST NOT 对每个片段单独设固定上限。总长不超过预算时，每个片段 MUST 原样发送。

#### Scenario: Everything fits
- **WHEN** 被选中片段渲染后的总长不超过 `max_context_chars`
- **THEN** 每个片段发给模型的文本 MUST 与完整渲染结果一致
- **AND** `context_budget.truncated_chunk_ids` MUST 为空

#### Scenario: Primary evidence funded first
- **WHEN** 总长超出预算
- **THEN** 主证据片段 MUST 先于解释上下文片段获得预算
- **AND** 每个被选中片段 MUST 至少获得 `min(长度, 400)` 字

#### Scenario: Budget below the floor
- **WHEN** `max_context_chars` 小于 `max_context_chunks × 400`
- **THEN** `AnswerGenerator` 构造时 MUST 抛出 `ValueError`

### Requirement: Relevance-based trimming

必须截断某个片段时，渲染 MUST 保留固定行（标题、定位信息），然后保留与问题共有实义词最多的句子，并按原文顺序输出。MUST NOT 使用固定的开头、结尾或首次匹配窗口，MUST NOT 注入问题中没有的泛化词。省略处 MUST 用 `[…]` 标记。

#### Scenario: Answer sentence in the middle of a passage
- **WHEN** 答案句位于一个超出预算的片段中部，前后都是包含 result、table、accuracy 等泛化词的句子
- **THEN** 截断后的文本 MUST 包含答案句
- **AND** MUST NOT 超过该片段的预算

#### Scenario: Audited real-corpus cases
- **WHEN** 以默认预算渲染 single_019/023/026/027/028 的 gold 片段
- **THEN** 发给模型的 prompt MUST 包含各自的答案句

### Requirement: Truncation is recorded

每次回答生成 MUST 在 `context_metadata.context_budget` 中记录策略、总预算，以及每个片段的原长、预算、保留长度和是否被截断；MUST 在 `context_metadata.prompt_chars` 中记录实际发出的 prompt 字符数。

#### Scenario: Truncated passage
- **WHEN** 某个片段被截断
- **THEN** 该片段的 chunk_id MUST 出现在 `truncated_chunk_ids` 中
- **AND** 其 `kept_chars` MUST 不超过 `budget_chars`，且小于 `original_chars`
