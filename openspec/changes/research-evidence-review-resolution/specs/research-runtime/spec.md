## ADDED Requirements

### Requirement: Opt In Claim Review Preserves Harness Gate Authority

启用 claim review 的 Research Graph SHALL 在最终 claim 与报告质量验收前声明评审准备、诊断和必要的有界 repair/wait 路径，所有 gate MUST 在启动前解析到精确版本。LLM/人工只提交观察或候选，Harness MUST 独占路由、预算、状态转移和正式发布控制。现有引用、作用域、质量与 terminal publication 护栏 MUST 保留。

#### Scenario: A valid diagnostic reports missing evidence
- **WHEN** 诊断节点 gate 接纳了结构完整且绑定正确的缺证据观察
- **THEN** 接纳仅证明诊断可用，不能将 claim 标为通过
- **AND** Harness 按冻结策略补查、等待或终结，不能通过普通后继越过失败的 ClaimEvidenceGate 或报告质量门

#### Scenario: A machine reviewer proposes a next action
- **WHEN** EXECUTE 返回补查、改句或发布建议
- **THEN** VERIFY 只校验已记录输入且不调用模型，Harness 按声明的有限路径与剩余预算决定下一步

### Requirement: Reviewed Reports Require Complete Objective Coverage

启用 enforce 的整份正式报告 SHALL 覆盖冻结目标的全部必要项，报告内所有待核验断言 SHALL 具有当前绑定下的支持记录，且无相关开放实质争议、失效规则或检查异常。单条支持 MUST NOT 代替报告和 controller-terminal 发布验收。本阶段 MUST NOT 增加受限正式报告放行路径。

#### Scenario: A required experiment is disputed after other analyses succeed
- **WHEN** 结构和方法已有支持，而必要实验仍有争议或证据不足
- **THEN** 有效内部材料得以保留，整份报告不得发布正式 reader、paper card 或 artifact 引用

#### Scenario: A candidate removes the unresolved required claim
- **WHEN** 为避开失败而删去实验句子或收窄为无关陈述
- **THEN** 必要目标仍未覆盖并阻止完整完成，只有用户明确改变目标版本后才能按新范围重评

#### Scenario: Downstream consumers read a revised or disputed result
- **WHEN** 阅读器、卡片、检索索引、memory 或下游 Agent 消费 claim
- **THEN** 消费必须保留其版本和当前资格，不得从摘要重建被隔离的肯定结论或将候选冒充正式成果

### Requirement: Review And Publication Share A Durable Ordering Boundary

评审接纳、案卷关闭和发布授权 SHALL 使用持久 revision、幂等身份与可排序的提交边界。发布 MUST 重检当前 claim/case/规则可用性，外部提交结果未知 MUST 先按既有 terminal intent/receipt 对账，不能盲重试。

#### Scenario: A challenge races with publication
- **WHEN** 实质异议与正式提交并发
- **THEN** 异议先被持久接纳时旧发布授权失效；正式提交先完成时创建发布后复核并标记产物
- **AND** 不能仅以发布前一次读取规避竞态，也不能把已提交成果伪称未发布

#### Scenario: A stale attempt finishes after the case closes
- **WHEN** 旧 worker 或迟到意见试图更新已关闭或被新修订替代的案卷
- **THEN** 当前状态和发布资格不变，旧结果只能被记录为原身份下的历史或关联重评输入

### Requirement: Review Recovery And Replay Preserve Historical Decisions

评审过程的 phase、案卷转移、证据接纳和 resolution SHALL 写入 durable events/transcript；记录提交失败 MUST NOT 推进状态。恢复 SHALL 对账已提交结果和未决意图，replay SHALL 仅使用冻结版本和记录，产生零外部调用及副作用。

#### Scenario: A run is replayed after rules have changed
- **WHEN** 历史 run 在新规则发布后被回放
- **THEN** 还原原决定和绑定，不调用 LLM、抓取来源、通知人工、发布规则或提交成果
- **AND** 按新版本重评必须另建关联 run

### Requirement: Claim Review Composition Is Explicit And Fails Closed

评审 SHALL 提供默认关闭、shadow 和 enforce 三种明确模式，生产模式 MUST 使用真实来源、真实持久化与明确评审适配。enforce SHALL 在准入时检查精确规则包、评审能力、预算和等待依赖，缺失 MUST 拒绝而非静默降级。shadow MUST 有授权和资源界限且无生产发布授权。

#### Scenario: An enforce dependency is missing
- **WHEN** 规则包、持久化、要求的评审能力或必要额度未配置
- **THEN** 预检明确失败，不调用不完整主链并伪装成新版核验，不换用 fake 或临时内存实现

#### Scenario: Shadow results disagree with current production
- **WHEN** 影子评审发现分歧
- **THEN** 只产生隔离的诊断和评测记录，不自动发人工请求、不改变生产发布或授予新权限

#### Scenario: The feature is disabled during active reviews
- **WHEN** 新准入被关闭或发生回滚
- **THEN** 活动运行按冻结策略结束或明确取消，已有争议和产物隔离继续有效，不静默按旧规则发布
- **AND** 历史未评审报告保持原合同标记，不回填为语义已核验
