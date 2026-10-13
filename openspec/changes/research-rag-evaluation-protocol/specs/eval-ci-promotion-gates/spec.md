## ADDED Requirements

### Requirement: Fixture gates prove regression contracts rather than product quality

既有 deterministic CI gate SHALL 保持无网络固定样例及原有约束测试用途，并将 corpus/adapter/answer 的 fixture 或合成身份写入报告。其 promotion-oriented 工件 MUST NOT 单独证明 production RAG 质量、候选实测收益或正式发布资格。

#### Scenario: Synthetic answers achieve perfect offline metrics
- **WHEN** 根据 gold 构造的回答在既有 CI 中全部通过
- **THEN** CI 可以证明离线评分与回归合同通过
- **AND** 不把这些成绩作为生产端到端成功率或候选推广依据

### Requirement: Formal acceptance requires complete applicable evidence

正式质量门禁 SHALL 验证冻结数据与保留集身份、生产 adapter provenance、模式适用性、完整计数、已审核阈值、有效覆盖、人工/校准要求及配置可比性。必需阶段 skipped/error/未决 SHALL 阻止正式 pass；失败不能通过降低断言、删除题目或重复选最好输出解决。

#### Scenario: Required live stage did not execute
- **WHEN** 正式验收缺少凭据、完整语料或预算而没有执行必需 live 阶段
- **THEN** 验收返回非零并给出 inconclusive 及缺失原因
- **AND** 不用离线 gate 通过或诊断命令 exit 0 代替该证据

#### Scenario: A required metric regresses against its approved policy
- **WHEN** 候选在可比的成对实验中违反预声明的退化/成本/覆盖门槛
- **THEN** gate 失败并保留受影响 case、指标与配置
- **AND** 不因另一个平均分提升而忽略必需约束

### Requirement: Evaluation qualification does not grant publication authority

eval gate SHALL 只产生既有发布/审批流程消费的证据与确定性资格判断。LLM judge、标注导入和 benchmark runner MUST NOT 直接修改 production profile、写 active skill、发布 artifact 或决定 workflow routing。

#### Scenario: A candidate passes all formal evaluation checks
- **WHEN** 候选的正式报告为 pass
- **THEN** 系统输出可审计资格工件
- **AND** 后续 promotion/publication 仍由既有 Harness/人工审批流程决定，本次评测不产生这些副作用

### Requirement: Ordinary CI verifies protocol boundaries without live services

普通 push/PR CI SHALL 继续使用 fixture，验证数据冻结、blind input、anchor 映射、三轨身份、评分 error/NA/分母、阈值方向、比较与报告合同。fake 服务 SHALL 仅在测试中模拟生产协议；通过这些测试 MUST NOT 被记成真实业务测量。

#### Scenario: CI verifies the formal adapter contract with a fake provider
- **WHEN** 无网络测试运行一个符合生产接口的 fake provider
- **THEN** 测试可以验证 gate、trace、成本字段和错误处理规则
- **AND** 工件身份保持 fixture，实际 production 验收仍需另行完成
