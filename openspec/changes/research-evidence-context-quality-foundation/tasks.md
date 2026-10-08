## 1. 文档收敛与遗留清理

- [x] 1.1 记录本 PRD 对七份 Research proposal 的替代边界、P0/P1/P2 实施顺序及明确非目标。
- [x] 1.2 将仍引用已废弃 Workflow-era Graph proposal 的测试元数据迁移到 canonical `harness-graph` 合同。
- [x] 1.3 从活跃 OpenSpec 工作区删除七份被合并的 Research proposal 和三份 obsolete Workflow-era change 目录，不保留兼容占位文档。
- [x] 1.4 将 `durable-event-runtime` 的未完成部署资格任务收缩为当前 Graph runtime 的回滚、重放、安全与投影重建证据，删除 Workflow 兼容观察/签名链前置条件。

## 2. 评测基线

- [ ] 2.1 清点 `backend/research/rag/evaluation`、现有 fixture 和 CI gate 的可复用入口，定义版本化固定案例与阈值配置，不新增平行 evaluator、run store 或 promotion service。
- [ ] 2.2 为输入完整性、硬约束保持、来源定位、claim 资格、确定性回退和 worker 越权拒绝添加离线案例及结构化失败诊断。
- [ ] 2.3 扩展现有评测报告，记录案例/代码/配置版本、每案例和聚合指标、基线比较、回退原因及通过/失败阈值。
- [ ] 2.4 让固定案例在无外部服务条件下可重复运行，并把关键阈值失败接入现有 CI gate。

## 3. 受控输入合同

- [ ] 3.1 在 Research application 层为显式启用的动态单篇论文路径准备 `ResearchInputProfile`，确保恰有一个 `CURRENT_TASK`，并保留类别、来源、修订、状态和限定条件。
- [ ] 3.2 在 worker 调用前验证 `VERIFIED_CONCLUSION` 的资格引用、模型候选的状态边界、输入范围及来源关联；不合格 profile 必须拒绝或回到现有未启用路径。
- [ ] 3.3 将已验证输入投影接入当前动态 AgentLoop/`PromptBuilder` 边界，验证最终逻辑任务仍包含当前目标和所有受保护硬约束。
- [ ] 3.4 添加单元、应用集成和架构测试，证明通用框架不承担 Research 语义，也没有引入禁止的 legacy Research 依赖。

## 4. 高价值 Claim 的证据资格

- [ ] 4.1 基于现有案例选择并记录一个 V1 高价值 claim 类型，以及必需的来源定位、条件、单位/百分比/比较对象等字段。
- [ ] 4.2 将 claim revision、类型化 observation、来源/条件绑定和 `ClaimReviewResolver` 输出接入 Research 应用路径，保持 worker、parser 和人工意见为 candidate-only。
- [ ] 4.3 注册精确版本的 Research deterministic gate，使 `supported`、`contradicted`、`insufficient`、`unavailable` 映射到可测试的报告资格，且只有合格输出可进入发布候选。
- [ ] 4.4 覆盖支持、反驳、证据不足、来源不可用、条件冲突、重复 observation 和候选越权等回归案例。

## 5. 默认关闭的检索候选试验

- [ ] 5.1 在现有 `WorkerRAGPlanner` 周围增加显式 disabled-by-default experiment 配置；保留原始查询和确定性 `RetrievalPlan` 作为可执行基线。
- [ ] 5.2 限制候选数量，验证实体、时间、来源、语言、版本、度量、否定和其他硬约束，并记录候选血缘、接纳/拒绝理由及回退原因。
- [ ] 5.3 对无效候选、worker 故障、预算耗尽和验证失败实现确定性原始计划回退；候选不得携带路由、质量、记忆、工具或发布控制权。
- [ ] 5.4 用固定对照集完成成对基线比较；只有无约束/证据/引用/回退退化且收益达到冻结阈值时，才可提出有限启用申请。

## 6. 触发式上下文连续性与进度观测

- [ ] 6.1 记录稳定的进度原因码和指标，包括 `insufficient_verified_progress`，并证明其不影响路由、发布或自动停机。
- [ ] 6.2 定义并收集窗口丢失、条件漂移或重复读取成本的可审计触发证据；没有满足触发条件时不得开始连续性实现。
- [ ] 6.3 触发后，仅为单个 run 实现已接受证据引用、条件和来源修订的工作投影，并在真实模型窗口边界用现有 context assembly/transcript/artifact/event 重组。
- [ ] 6.4 覆盖窗口边界重组、来源修订、条件保留、重放和默认关闭行为；不得创建跨 run 记忆、平行 archive、CAS、claim-state 或全局缓存系统。

## 7. 集成验证与发布控制

- [ ] 7.1 运行本变更的严格 OpenSpec 校验、Research 定向单元/集成/架构测试、固定评测基线及与改动范围匹配的 smoke 检查。
- [ ] 7.2 验证 Harness 保持 `PLAN -> EXECUTE -> VERIFY` 有界状态机，所有新增资格判断均为 Research-owned deterministic gate，且 phase transition 可重放和审查。
- [ ] 7.3 记录 feature flag 默认状态、基线版本、阈值、失败回退、部署范围和回滚步骤；任何扩大候选检索或上下文连续性范围的决定必须引用对应评测/触发证据。
