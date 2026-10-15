## ADDED Requirements

### Requirement: Public Research requests do not select execution mechanisms

合格 release 的统一分析入口 SHALL 接收任务目标、单论文引用、材料范围、质量与资源约束，由服务端完成执行准入。公开请求 MUST NOT 选择 static/dynamic、planner、agent 数量或内部 Graph/policy/model/capability binding；外部覆盖字段 MUST 被明确拒绝。部署资格 SHALL 与用户请求分离。对应 PRD FR-01、FR-10。

#### Scenario: A user submits a supported analysis goal
- **WHEN** 用户提交合格 scope 内、输入完整的论文分析请求
- **THEN** application service SHALL 在服务端发布策略内接纳运行，不要求用户选择执行机制
- **AND** interface SHALL 通过 application service 访问业务能力，不直接驱动 executor 或 store

#### Scenario: A request attempts to override an internal route
- **WHEN** 外部请求包含 `dynamic_analysis`、`dynamic_task_plan`、内部策略覆盖或未经允许的 binding 字段
- **THEN** 请求 SHALL 收到明确的 validation error
- **AND** 系统 MUST NOT 静默忽略该覆盖后继续、创建兼容别名或扩大可执行能力

### Requirement: Execution intent remains a bounded semantic candidate

Research SHALL 将执行任务意图与搜索 `GuidedIntent` 区分。意图候选 SHALL 表达目标、论文、交付物、约束、输入引用、缺失信息和有限建议动作；真实授权、材料修订、能力、质量、预算与 bindings SHALL 由服务端解析验证。模型候选、confidence 或自由文本 MUST NOT 直接成为 routing 或完成判断。对应 PRD FR-02。

#### Scenario: A known structured request is normalized
- **WHEN** 已知结构化单论文分析请求足以确定标准合同
- **THEN** 系统 SHALL 使用确定性规范化，不额外调用语义分类模型或动态 planner
- **AND** 原有 worker、材料和 gate 调用 SHALL 保留

#### Scenario: Natural language introduces additional task constraints
- **WHEN** 请求含需要理解的自然语言交付约束
- **THEN** 系统 SHALL 使用一个有界的结构化候选过程，并校验候选与请求、论文和能力范围的一致性
- **AND** 系统 MUST NOT 再启动独立复杂度 agent 重复理解相同目标，或仅依据字数、关键词、自报 confidence 决定增加执行权限

#### Scenario: Essential information or capability is unavailable
- **WHEN** 缺少必要论文/目标信息，或任务超出已验收能力
- **THEN** 系统 SHALL 分别进入有界必要澄清或明确的 unsupported/unavailable 处理
- **AND** 系统 MUST NOT 将缺失信息或能力误当作增加 agent 即可解决的复杂任务

### Requirement: Initial work selection minimizes unnecessary coordination

Harness SHALL 根据已接纳任务、固定合同覆盖、材料与能力条件选择已声明的工作方式。标准任务 SHALL 复用既定 worker，不增加无必要的 planner 或子 agent；确有合法额外工作的任务 SHALL 能在首次分析前申请 bounded AgentLoop 或 TaskPlan，而非必须先完整运行另一条路线。对应 PRD FR-03、FR-04。

#### Scenario: The existing analysis contract covers the request
- **WHEN** 请求交付物由既有标准分析合同覆盖且输入合格
- **THEN** 系统 SHALL 采用既定分析步骤且无自适应专用 planner/语义分类调用
- **AND** 自适应功能 MUST NOT 额外 spawn 子 agent 或跳过任何必需输出、证据与 gate

#### Scenario: A supported goal requires local investigation before analysis
- **WHEN** 已接纳目标和材料条件表明需要模板未覆盖的同论文核验，且对应能力已注册、有预算
- **THEN** Harness SHALL 能接纳已声明的局部探索或任务分解
- **AND** 系统 MUST NOT 强制先完成整个固定分析路径再无条件从头重跑

#### Scenario: A proposal introduces unnecessary fan-out
- **WHEN** 分解候选不能说明独立输入输出、验收、恢复或并行收益，或不存在合格并行能力
- **THEN** Harness SHALL 拒绝不必要的 fan-out，或在已声明且合格的串行/单 worker 合同内处理
- **AND** 系统 MUST NOT 将“复杂”标签本身视为多 agent 授权

### Requirement: Adaptive work stays inside one immutable execution contract

每个 run SHALL 冻结 Graph/version/checksum、policy、quality profile、允许的 exact capability/binding 集合及授权范围。自适应 SHALL 复用现有 Graph controls、AgentLoop 和 TaskPlan owner；候选只能选择或填充已声明的阶段执行空间。Harness MUST 在 schema、依赖、角色、refs、权限、版本及预算验证通过并持久接纳后派发，且 MUST NOT 新建竞争性控制器或运行时替换 Graph。对应 PRD FR-04、FR-06。

#### Scenario: A local task proposal is valid
- **WHEN** 候选任务仅使用声明范围内的真实 inputs、能力与输出角色，且所有接纳条件满足
- **THEN** Harness SHALL 持久保存接纳与资源预留，再通过原 execution owner 派发
- **AND** 所有步骤 SHALL 继续服从有界 `PLAN -> EXECUTE -> VERIFY` 及其 deterministic gates

#### Scenario: A model proposes a graph mutation or privileged task
- **WHEN** 候选要求替换 Graph、调用未注册工具、跨 tenant/run 引用，或增加 quality/publication/memory-promotion 权限
- **THEN** Harness SHALL 在派发前拒绝候选并保存拒绝原因
- **AND** Graph identity、授权、质量与 active skill/policy SHALL 保持不变

### Requirement: Runtime adaptation requires accepted and repairable evidence

局部升级 SHALL 由已接纳的结构化执行事实、证据资格/覆盖检查、声明 gate 或 capability precondition 驱动，并关联 observation refs、目标、输入 revision 与合格修复能力。模型自评、todo、UI 进度和日志文本 MUST NOT 具有路由权。相同失败指纹没有新增可接纳证据时 MUST NOT 反复升级。对应 PRD FR-05。

#### Scenario: A recoverable evidence gap is detected
- **WHEN** 已声明检查确认同论文证据缺口、存在授权修复能力且预算足够
- **THEN** Harness SHALL 能接纳声明范围内的补证或局部 replan，记录触发事实和失败指纹
- **AND** 系统 SHALL 仅重做受影响且需要再验证的工作，复用仍有效的已接纳 refs

#### Scenario: A failure has already consumed its permitted repair
- **WHEN** 目标、gate、输入修订及已尝试修复类别形成相同失败指纹，且没有新增可接纳事实
- **THEN** 系统 SHALL 停止重复升级并记录原因，或仅执行原 retry policy 已允许且未耗尽的重试
- **AND** 切换 worker、loop 或 TaskPlan MUST NOT 重置 attempt、turn、replan 或成本额度

#### Scenario: The failure is not a complexity signal
- **WHEN** 发生权限拒绝、取消、预算耗尽、不可用能力、状态/identity 损坏或边界篡改
- **THEN** 系统 SHALL 按原错误合同暂停、终止或进入已授权恢复处理
- **AND** 系统 MUST NOT 以更复杂执行方式绕过原失败，或通过重新分类获得新授权

### Requirement: Quality and source authority are invariant across strategies

同一请求的所有执行方式 SHALL 满足相同 quality profile、必需角色、证据资格和发布条件。补证 SHALL 限于同论文、同源修订的授权材料，生成带来源、修订、校验和及血缘的新 refs；既有 accepted refs MUST NOT 被原地篡改。固定 source/evidence、分析报告、质量与终端发布主链 SHALL 保持，只有顶层 authority 能发布。对应 PRD FR-06。

#### Scenario: A lower-overhead path completes analysis
- **WHEN** 既定 worker 路径完成候选分析
- **THEN** 系统 SHALL 执行与该请求 loop / TaskPlan 路径相同的适用质量门和终端发布检查
- **AND** `analysis.structure`、`analysis.contribution`、`analysis.experiments` 等必需角色 MUST NOT 因成本偏好缺失

#### Scenario: Supplementary evidence changes an analysis dependency
- **WHEN** 同源修订内的补证形成合法新 refs 并影响已有结论
- **THEN** 系统 SHALL 记录引用血缘并使受影响结果重新接受验证
- **AND** 系统 MUST NOT 修改原 accepted checksum、继续使用已失效结果，或把模型推测标为已核验事实

#### Scenario: A child reports success while a top-level gate fails
- **WHEN** 子任务或模型报告完成，但必需角色、证据或顶层 gate 未通过
- **THEN** run SHALL 保持未合格状态，按受控修复或停止规则处理
- **AND** 诊断候选只能按既有隔离规则保存，MUST NOT 进入正式报告/发布目录

### Requirement: All adaptive work consumes a shared bounded request budget

解释、planner、worker、tool、child、重试、compaction、澄清和恢复 SHALL 归属同一请求血缘，并复用既有预算责任方。总 turn、replan、模型/工具调用、token、任务数、并发和时间 SHALL 受服务端上限约束；派发 SHALL 原子预留，执行 SHALL 以 receipts 结算。扩充资源 SHALL 需要可追溯授权 revision，不能通过模式或目标表述变化静默续期。对应 PRD FR-07。

#### Scenario: Parallel work competes for the remaining budget
- **WHEN** 多个 child 或 repair 同时申请剩余资源
- **THEN** budget owner SHALL 原子分配不超过可用余额的额度
- **AND** 不能获得额度的工作 MUST NOT 派发，已消耗的理解和规划成本 SHALL 计入总额

#### Scenario: Recovery encounters an indeterminate external call
- **WHEN** crash 后无法确认已预留调用是否执行或结算
- **THEN** 系统 SHALL 保守保留占用并通过既有 receipt/幂等/恢复机制核对，或 fail closed
- **AND** 系统 MUST NOT 因重启或升级无条件重发、重复退款或获得新预算

### Requirement: Adaptive decisions are durable and user explanations are factual

策略建议、接纳/拒绝、升级、停止和全部 Harness phase transitions SHALL 使用既有 canonical event / transcript。决定 SHALL 记录其理由、输入/observation refs、版本、预算、attempt/plan revision 与结果 refs，并先于派发持久化。用户界面 SHALL 投影业务进度、简洁原因、缺口与资源消耗，复用取消/必要授权流程，不泄露隐藏推理或无权上下文。对应 PRD FR-08。

#### Scenario: Additional work is accepted
- **WHEN** Harness 接纳一次同论文补证或分解
- **THEN** durable record SHALL 可证明接纳依据、预算与派发先后关系
- **AND** 用户 SHALL 能看到事实性说明，例如“部分结论缺少可核验引用，正在补充证据”，而不是内部推理或策略选择题

#### Scenario: The user cancels during analysis
- **WHEN** 用户使用既有取消入口取消 run
- **THEN** 系统 SHALL 保持原取消合同并阻止新的未授权规划/派发/发布
- **AND** 自适应状态与 UI MUST NOT 把取消伪装为完成或自动改用另一条执行路线

### Requirement: Recovery preserves accepted choices and idempotency

恢复 SHALL 使用已冻结 Graph/policy/bindings、已接纳策略、计划、预算、有效 refs 和 pending child 状态。系统 MUST NOT 重新猜测复杂度或依据当前默认值重新路由；必要版本不可用时 SHALL fail closed。重复提交、事件和 child 乱序 SHALL 不改变聚合语义、重复派发/结算或发布。对应 PRD FR-09。

#### Scenario: A process restarts after acceptance and before dispatch
- **WHEN** 决策已持久接纳但对应工作未完成派发
- **THEN** 系统 SHALL 从原决定和预留状态恢复，按既有幂等合同继续
- **AND** 恢复 MUST NOT 重新调用意图/规划模型来替换原决定

#### Scenario: Old receipts arrive after a newer plan revision
- **WHEN** child receipt 重复、乱序或归属于已失效的 plan revision
- **THEN** 系统 SHALL 按 frozen identity、revision 与 receipt authority 去重、归档或拒绝
- **AND** 旧结果 MUST NOT 替换新聚合、重复释放额度或触发发布

### Requirement: Release rollback cannot weaken an active run

自动准入 SHALL 仅使用完成依赖和产品验收的 task/quality/model/capability profiles。release 配置变更 SHALL 只改变新准入；活动 run SHALL 保留原合同。普通 business run MUST NOT 更新 active policy、路由器、阈值、quality gate 或 skill。对应 PRD FR-10。

#### Scenario: A dependency is not qualified for automatic execution
- **WHEN** 需要的能力或上游 release gate 尚未通过
- **THEN** 该 scope SHALL 不进入自动候选集合，并给出明确 disabled/unavailable 状态
- **AND** 系统 MUST NOT 以自适应为名开启未经验收的并行或隐藏替代路径

#### Scenario: A rollout is disabled while a run is active
- **WHEN** 运营关闭某个自适应 release profile
- **THEN** 新准入 SHALL 依新的发布策略处理，活动 run SHALL 按原冻结合同完成、取消或安全终止
- **AND** rollback MUST NOT 降低质量门、改写 Graph identity、扩大权限或把活动 run 静默迁移到新模式
