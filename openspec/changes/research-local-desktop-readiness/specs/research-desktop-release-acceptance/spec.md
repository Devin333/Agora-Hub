## ADDED Requirements

### Requirement: Implementation precedes execution checks
The delivery SHALL 完成任务 1–10 的约定生产代码及必要测试代码后，再集中执行测试、typecheck、lint、build、compile 和 smoke；编写规格期间 SHALL 只做文档校验。实现阶段的静态审查 SHALL 不被记作运行测试通过。

#### Scenario: A feature group finishes before the remaining code
- **WHEN** 一个实现分组已完成但约定实现范围仍有未完成项
- **THEN** 继续完成其他代码与测试代码，不提前反复运行各组/全量测试或将其标为已验收

### Requirement: Complete final candidate validation
The delivery SHALL 对最终候选运行 frontend test/typecheck/lint/build、backend compile/full test/smoke、相关合同/架构检查和 strict OpenSpec validation。失败 SHALL 修复根因，不跳过相关测试、削弱断言或借用旧版本通过记录。

#### Scenario: A defect changes shared runtime code
- **WHEN** 集中验证发现缺陷并修复影响了共享运行路径
- **THEN** 重跑受影响验证并保证最终 full test/smoke 证据覆盖最终候选源码和配置

#### Scenario: Required validation cannot execute
- **WHEN** 依赖缺失或环境问题使必需检查无法运行
- **THEN** 清单保留未完成并记录原因，不能以 exit-code skip 或占位日志宣称通过

### Requirement: Real desktop and recovery acceptance
The delivery SHALL 执行 PRD AC-01–AC-17，包含真实账号、实际来源搜索、PDF 导入、真实 provider 分析/问答、证据报告、服务重启、Windows 生命周期、备份/版本恢复及电脑重启后的继续使用。成功 live 路径 SHALL 不拦截 API 或以 fixture 替代；自动负例 SHALL 明确标识测试环境。

#### Scenario: Real research flow succeeds
- **WHEN** 使用真实来源和已配置 provider 完成单论文流程
- **THEN** 记录 browser、request/run/source/artifact/quality 身份并证明保存、引用和再次打开成立

#### Scenario: Only mocked UI tests pass
- **WHEN** 组件或 Playwright route mock 测试通过但实际服务流程未运行
- **THEN** live acceptance 保持未完成，不宣布本机可用交付完成

#### Scenario: Machine restart has not been performed
- **WHEN** 只重启服务而未完成人工电脑重启检查
- **THEN** AC-15 保持未完成，工具不得为关闭任务擅自重启电脑

### Requirement: Candidate bound redacted evidence
The delivery SHALL 将检查和验收证据绑定 Git revision/source digest、release/build、依赖、配置身份、环境、时间、命令/退出码及日志引用。证据 SHALL 脱敏且可核对，不包含会话、密钥和用户论文全文；部署 SHALL 使用同一已验收构建。

#### Scenario: Evidence belongs to a different build
- **WHEN** 实际部署构建、源码或依赖与证据身份不匹配
- **THEN** 不能据该证据宣布交付完成，须重新验证受影响路径和候选一致性

### Requirement: Owned checklist closure and usable handoff
The delivery SHALL 保留各 active change 的任务所有权，只有满足对应条件的同版本证据才可核销原 `research-workspace-efficiency` 5.2/5.3。其他变更的部署资格和实验准入 SHALL 不被本地验收自动替代。用户 SHALL 获得中文使用文档、桌面入口、配置/诊断/停止/备份/恢复方法和实际能力限制。

#### Scenario: Desktop acceptance also satisfies workspace checks
- **WHEN** 同一候选的真实浏览器证据逐项满足原 workspace 5.2/5.3
- **THEN** 两份任务可引用同一证据并分别更新，不删除原任务历史

#### Scenario: Local use works while experimental prerequisites remain open
- **WHEN** 本机默认主链通过验收但并行、候选检索或上下文实验缺少原 owner 准入
- **THEN** 这些能力保持既定关闭/受控状态，原部署/触发任务保持原状态
