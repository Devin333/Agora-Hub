## ADDED Requirements

### Requirement: Prompts require specific supporting citations

回答 prompt MUST 要求引用紧邻其支持的事实、每个引用独立支持对应表述，并仅在需要时引用。prompt MUST NOT 鼓励用无关引用、堆积多个引用或仅凭主题相似替代支持。现有文本回答及引用 marker 协议 MUST 保持，不引入复杂回答 JSON schema。

#### Scenario: A statement combines separately sourced facts
- **WHEN** 模型收到包含多个事实来源的回答请求
- **THEN** 实际发送的 prompt MUST 明确要求将事实与支持它的引用对应，不以一个无关引用笼统支持整段
- **AND** 指令 MUST NOT 规定无条件增加引用数量

### Requirement: Insufficient evidence and boolean answers follow the existing protocol

回答 prompt MUST 要求证据不足、冲突或无法支持结论时使用既有拒答协议。是否题在存在充分正向或反向证据时 MUST 被提示明确给出 Yes/No；仅未提及某事实 MUST NOT 被提示解释为 No。题型判断 MUST 来自问题或请求元数据，MUST NOT 读取 gold 答案。现有业务 outcome、投影和评分分母 MUST 保持。

#### Scenario: Absence is not a negative finding
- **WHEN** 问题要求 Yes/No，而提供文本没有相应正向或反向证据
- **THEN** 实际发送的 prompt MUST 要求遵循证据不足协议，MUST NOT 指示模型因缺失信息回答 No

#### Scenario: Explicit negative evidence
- **WHEN** 提供的证据明确支持问题的否定结论
- **THEN** prompt MUST 允许模型明确回答 No 并引用该证据，MUST NOT 将所有否定回答要求为拒答

### Requirement: Citation validation remains structural and deterministic

Harness MUST 保留现有确定性引用格式、marker 索引及实际 context 范围校验。新增约束 MUST 仅使用可验证的候选字段和既有职责合适的 gate；无可靠结构依据的语义约束 MUST 留在 prompt 和质量评估中。引用合法、原文存在、词汇重叠或模型自报支持 MUST NOT 被当作事实蕴含证明。

#### Scenario: Citation refers outside supplied context
- **WHEN** 候选引用 marker 指向未实际发送给模型的 context 项或非法索引
- **THEN** 既有确定性验证 MUST 判定该结构无效并给出原因，MUST NOT 因模型声称支持而通过

#### Scenario: Valid citation marker with uncertain semantics
- **WHEN** 引用 marker 合法，但其是否支持事实无法由现有结构字段验证
- **THEN** 结构通过 MUST NOT 被记录为语义支持已得到证明，也 MUST NOT 新增模型自报 support 的通过门槛

### Requirement: Harness retains bounded generation control

LLM MUST 只产生回答候选。结构验证失败后的重试、replan、停止和最终 outcome MUST 由现有 Harness 确定性逻辑控制，并受 `max_turns`、`max_replans` 和 retry budget 限制。phase transition 和失败原因 MUST 继续写入 durable transcript/event log，候选内容 MUST NOT 覆盖路由、门禁、预算或发布决定。

#### Scenario: Repeated invalid citations exhaust the budget
- **WHEN** 连续候选均未通过引用结构验证并达到配置的重试上限
- **THEN** Harness MUST 受控停止并保留既有失败语义及事件记录，MUST NOT 因模型请求再次尝试而无限运行

### Requirement: Prompt changes have separate evaluation identity

生成 prompt 的变化 MUST 可追踪到版本或内容身份。采用新提示生成的结果 MUST 作为新行为的独立比较，MUST NOT 合并为旧 prompt 运行的纯故障恢复。结构测试通过 MUST NOT 替代冻结样本上的语义质量评估或人工 judge 校准。

#### Scenario: New prompt is used on an old failed case
- **WHEN** 新提示下重新回答旧运行的一道系统失败题
- **THEN** 该结果 MUST 声明新的生成身份，MUST NOT 被标记为旧配置下答案未变的恢复
