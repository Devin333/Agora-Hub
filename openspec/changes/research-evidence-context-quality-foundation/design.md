## Context

Research 的目标运行路径仍是 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage`。当前代码已经具备输入合同领域模型、claim review resolver、确定性及 worker 辅助的 RAG 计划器、离线 RAG 评测和 Harness Graph 的确定性 VERIFY 边界。问题不在于缺少一个新的通用平台，而在于这些组件尚未在一个受控的单篇论文路径中组合，并且已有七份拟议 PRD 对同一信任边界提出了相互重叠的状态、评测和流程要求。

本设计把可立即验证的价值收敛为三件事：输入完整性、证据资格和可比较评测。查询候选、上下文续接和进度治理保留价值，但只以默认关闭或观察模式存在，避免在没有真实触发证据时建设第二套系统。

## Goals

- 在现有 Research 所有权边界内组合输入、证据和评测，不重建 Harness、事件或 RAG 平台。
- 让显式启用的动态单篇论文路径在 worker 调用前验证当前任务、约束、来源和状态。
- 让一个高价值 claim 类型的发布资格由确定性 Research resolver/gate 决定。
- 用固定离线案例证明任何优化不会降低约束保持、来源定位、证据资格和回退行为。
- 为检索候选与上下文连续性留下有边界、可审计、可回退的后续入口。

## Non-Goals

- 不覆盖所有模型调用、论文类型、RAG/OCR 路径或历史数据迁移。
- 不实现完整人工争议运营、规则/skill 发布、全局评测平台或持久化候选运行库。
- 不建立新 archive、CAS、claim state、长时记忆、并行事件流或独立控制器。
- 不把观察指标变成进度强制、自动路由或发布授权。
- 不为遗留 Workflow 重新建立兼容层、观察窗口或第二套 runtime。

## Decisions

### 1. 扩展现有评测，而不是新建评测平台

`backend/research/rag/evaluation` 是唯一的第一阶段评测入口。新增案例、报告字段和阈值配置必须扩展现有 evaluator、fixture 和 CI gate；不得新增平行 run store、dataset service 或 promotion service。离线案例需要同时覆盖正常路径和候选越权/回退路径，以保证评价的是业务控制边界，而不只是文本质量。

### 2. Research 拥有输入语义，框架只接收已准备的数据

`ResearchInputProfile` 的类别、状态、来源和限定条件属于 Research domain/application。显式启用的单篇论文 application service 在进入动态 AgentLoop 前准备和验证 profile；通用 `PromptBuilder` 只接收结构化、已经验证的输入投影，不解释论文业务语义。最终逻辑请求验证发生在 worker 调用之前，并且必须能指明丢失的目标、约束或来源。

### 3. 证据资格以一个确定性 resolver 为中心

V1 选择一个高价值 claim 类别，并把 claim revision、来源定位、条件、类型化观察和 resolver 输出绑定为一个可重放的决策对象。worker、parser 和人工评审只产生 observation；`ClaimReviewResolver` 和注册的 Research gate 是唯一可以把状态转换为发布资格的组件。任何候选中出现的 `passed`、route、publish 或 memory 意图均视为普通候选字段，不能获得控制权。

### 4. 采用分阶段、显式开关与触发门槛

P0 包含评测、输入和高价值 claim 的资格链。P1 的查询候选试验默认关闭，且需要成对基线比较。P2 的上下文连续性只在可审计轨迹证明窗口丢失、条件漂移或重复读取成本后才可实现。进度原因码保持 observation-only，任何强制治理都需另立 OpenSpec 变更。

### 5. 查询候选必须保持原始确定性路径

`WorkerRAGPlanner` 只能提出受限候选。验证器检查实体、时间、来源、语言、版本、度量、否定和其他硬约束；候选携带血缘和验证理由，但没有路由、批准、发布或写入字段。无效候选、worker 故障、预算耗尽和指标退化均必须回退到原始确定性 `RetrievalPlan`。

### 6. 上下文连续性是单 run 投影，不是新状态平台

一旦满足触发条件，连续性只保存当前 run 已接受的证据引用、适用条件和来源修订，并在真实模型窗口边界借助现有 context assembly、transcript/artifact 和 Harness 事件重组。它不创建跨 run 记忆、平行 claim 状态、全局缓存或新的 event archive。

### 7. 文档迁移采用删除重复源、保留历史与活跃所有者

七份重叠的 Research proposal 由本变更替代并从活跃 OpenSpec 目录删除。被 Graph-only 规范取代的旧 Workflow proposal 也删除。现有 Graph 运行时部署/回滚、durable event、模型上下文准入和工作区效率变更保留其独立所有权。历史版本控制记录保留审计来源，不添加兼容性占位文档。

## Risks / Trade-offs

| 风险 | 控制方式 |
| --- | --- |
| 只选一个 claim 类型会遗漏其他高风险结论 | 明确标记范围外 claim 为未由 V1 核验，不把局部覆盖表述为全量语义验证 |
| 输入投影改变现有动态提示词行为 | 仅在显式 profile 下启用，先跑固定案例，未启用调用方保持原路径 |
| 候选查询看似提升召回但破坏约束 | 默认关闭，成对评测、确定性验证和原始计划回退均为发布前置条件 |
| 过早实现上下文状态导致重复系统 | 先记录触发证据；没有可审计问题不开始实现 |
| 删除旧 PRD 使历史决策不易追踪 | 在新 PRD 中列明被替代项，依赖版本控制历史；不保留会被误读为仍活跃的兼容文档 |
| `durable-event-runtime` 的旧兼容链与 Graph-only 方针冲突 | 只保留当前 Graph runtime 的回滚、重放、安全与投影重建证据；移除 Workflow 兼容观察/签名链作为删除前置条件 |

## Migration Plan

1. 建立本变更的 PRD、设计、规格和任务，并明确 P0/P1/P2 边界。
2. 更新仍指向删除的 `harness-workflow-graph-runtime` 的测试元数据，使其指向 canonical `harness-graph` 合同。
3. 将 `durable-event-runtime` 的未完成部署资格任务收缩为当前 Graph runtime 的回滚与重放证据，不保留已废弃 Workflow 兼容观察/治理签名链。
4. 删除列出的七份 Research proposal 和三份 obsolete Workflow-era change 目录。
5. 在实际代码实施时按 P0 -> P1 -> P2 顺序执行；每个开关从 disabled 开始，并在对应离线/集成证据通过后单独扩大范围。

## Rollback

本次是文档和元数据迁移，不改变生产运行时。若新的收敛边界不成立，使用版本控制恢复已删除的本地 OpenSpec 文档，并以新的 OpenSpec 变更记录修订原因。未来 P0/P1/P2 的每个运行时开关均须独立具备默认关闭和确定性回退行为，不能依赖文档迁移本身回滚。

## Open Questions

- V1 高价值 claim 的第一种具体业务类别由哪个现有案例覆盖最完整：数值结果、对比结论还是实验条件？
- 固定对照集的通过阈值和比较窗口应由当前 baseline 的哪一次可重复运行冻结？
- 是否已有可复用的生产轨迹指标足以判断“重复读取成本”或“条件漂移”，否则先补观察字段后再决定 P2？
