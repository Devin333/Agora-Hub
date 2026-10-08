# Research 证据可信性与上下文质量基础 PRD

- **变更**：`research-evidence-context-quality-foundation`
- **状态**：Proposed
- **日期**：2026-10-08
- **产品范围**：单篇论文 Research 的显式启用路径

## 1. 背景与问题

Research 已有输入分类、claim review、确定性 RAG 计划、worker 候选计划和离线评测的组成部分，但它们尚未构成一条由应用层统一拥有的可信路径。七份拟议 PRD 从不同切面描述了输入、评审、查询改写、上下文和进度；若分别实现，会重复建立状态、评测和控制逻辑，也会在核心问题尚未被量化前扩大运行面。

本 PRD 的第一目标不是生成更长的回答，而是让一次论文分析能够证明：

- 用户当前任务和硬约束没有在上下文组装中丢失或被旧材料覆盖；
- 重要结论能够区分作者原文、模型候选和已核验结论；
- 被标记为已核验的高价值 claim 有可定位的来源、适用条件和确定性资格判断；
- 任何检索候选、模型输出或人工观察只能提供候选信息，不能决定路由、质量通过、记忆写入或发布；
- 后续优化只在基线证明其不降低上述属性后才可启用。

## 2. 用户与结果

主要用户是需要回答论文事实、数值、比较或方法结论的 Research 使用者。其需要的是带来源定位和适用条件的可信回答，而不是把未验证的模型归纳、作者声明和已确认事实混成同一层级。

本 PRD 完成的可观察结果：

1. 显式启用的单篇论文路径会接收一个经验证的 `ResearchInputProfile`，且最终送入模型的逻辑任务仍包含当前目标和受保护约束。
2. 一个高价值 claim 类型会经过来源定位、条件绑定、候选观察和 `ClaimReviewResolver` 的确定性资格判断；未达到资格的内容不能作为已核验事实发布。
3. 现有评测入口能对固定案例产出与基线可比较的输入完整性、约束保持、证据/引用和回退指标。
4. 查询候选与上下文连续性均保持默认关闭；只有满足明确定义的评测或运行触发条件，才可进行有限、可回退的试验。

## 3. 当前可复用基础

| 基础 | 当前职责 | 本 PRD 的使用方式 |
| --- | --- | --- |
| `ResearchInputProfile` 与输入领域对象 | 分类当前任务、论文材料、既有结论、工具观察和必要历史 | 接入显式启用的动态 AgentLoop，而非创建第二套消息模型 |
| `ClaimReviewResolver` 与 claim review 领域对象 | 解析评审观察和结果 | 作为高价值 claim 发布资格的确定性中心 |
| `WorkerRAGPlanner` 与确定性 planner | worker 提出检索候选，确定性路径可回退 | 仅用于受限候选试验，原始查询仍可执行 |
| `backend/research/rag/evaluation` | 离线 fixture、评测和 CI gate | 扩展固定对照案例和比较报告，不新建评测平台或运行库 |
| Harness Graph、事件和 artifact 机制 | 有界执行、确定性 VERIFY、留痕和发布 | 复用现有执行边界，不新增平行控制器或 claim 状态系统 |

## 4. 第一阶段：必须实现的质量核心

### 4.1 评测基线

在现有 Research/RAG 评测入口中加入版本化的固定对照案例和可比较报告。案例至少覆盖：当前任务和约束保留、来源定位、支持/反驳/证据不足/不可用、确定性回退，以及候选输出试图越权的拒绝路径。报告必须保留案例版本、代码版本、配置、指标和失败原因。

第一阶段不建立新的全局评测平台、通用运行数据库、模型晋级系统或覆盖所有历史 PRD 的大规模 Golden Set。案例规模和阈值由实现前的基线审查确定，并作为版本化配置提交。

### 4.2 受控输入合同接入

仅为显式启用的动态单篇论文分析路径接入 `ResearchInputProfile`：

- 只允许一个 `CURRENT_TASK`；
- 输入必须保留类别、来源、来源版本/修订、状态和限定条件；
- `VERIFIED_CONCLUSION` 必须带有合格的核验引用，模型候选不得伪装成已核验结论；
- 组装前后均验证任务、硬约束、来源关联和状态边界；
- 合同不完整或最终逻辑任务失真时，必须在 worker 调用前失败，或走现有未启用路径；不得静默降级为不受控提示词。

此阶段不要求将所有 provider 模板、历史 RAG/OCR、工具调用或非 Research AgentLoop 迁移到新合同。

### 4.3 高价值 claim 的证据资格

选择一个可定义范围的高价值 claim 类型作为 V1，例如带数值、单位、百分比、比较条件或实验设置的论文结论。每个候选 claim 必须绑定：

- 精确来源定位；
- 适用条件、比较对象和版本/时间条件；
- 类型化观察，而非只有自然语言 “通过/不通过”；
- 由 Research 所有的确定性 resolver 生成的资格状态。

模型、解析器和人工评审可以提交支持、反证、缺口或不可用观察，但不能自行批准发布。`supported`、`contradicted`、`insufficient` 和 `unavailable` 必须有明确且可测试的业务含义；后 3 者不得被渲染为已核验结论。

本阶段不交付完整争议工单、盲审队列、跨模型缓存、规则发布系统或面向读者的复杂申诉界面。

### 4.4 进度可观测性

记录稳定的 Research 原因码和指标，例如 `insufficient_verified_progress`。这些记录用于发现缺证、重复读取和条件漂移，不得在本 PRD 中成为自动停机、自动发布或模型路由的强制策略。

## 5. 后续阶段：有价值但不得整体提前开发

### 5.1 检索候选试验

查询改写/扩展只作为 `WorkerRAGPlanner` 的默认关闭试验：保留原始查询，限制候选数量，验证实体、时间、来源、语言、版本、度量和否定等硬约束，并记录候选来源、验证原因和回退原因。任何无效候选、超时或质量退化都必须执行确定性原始计划。

只有固定对照集的成对比较证明召回或任务完成度改善，且输入完整性、约束保持、证据/引用和回退指标没有退化时，才可批准有限启用。不得先建设通用查询重写平台、澄清状态机、跨任务缓存或持久化候选日志系统。

### 5.2 上下文连续性

上下文续接只在真实运行轨迹显示下列至少一种问题后进入实现：模型窗口边界导致已接受证据或条件丢失、条件漂移，或重复读取造成显著成本。实现仅维护单个 run 的工作投影，包含已接受证据引用、条件和来源修订，并在实际窗口边界重组。

不得为此建立平行 archive、CAS、事件流、长期记忆、跨 run claim 状态或全局缓存失效系统。已有 Harness transcript、artifact、context assembly 和事件机制是唯一可复用的留痕基础。

## 6. 明确不做

- 不新建平行的评测、运行、事件、状态、审批或发布平台。
- 不让 LLM/人工观察决定 workflow routing、quality pass/fail、memory write、tool authorization、skill promotion 或 publication。
- 不迁移全部模型调用、全部论文类型、全部 RAG/OCR 路径或历史文本到新输入合同。
- 不把进度原因码变成自动停止、强制重规划或发布策略。
- 不做默认开启的查询改写、无边界候选生成、跨 run 长期上下文或自动规则/skill 发布。
- 不恢复旧 Workflow 兼容层或以观察窗口、第二套运行时作为 Graph-only 架构删除的前置条件。

## 7. 依赖与实施顺序

1. 扩展现有离线评测，冻结可比较的基线和阈值配置。
2. 在显式启用的单篇论文路径接入输入合同和最终逻辑请求验证。
3. 接入高价值 claim 的证据资格判断，并以确定性 gate 控制发布资格。
4. 仅在第 1-3 步稳定后，运行默认关闭的检索候选实验。
5. 仅在真实轨迹满足触发条件后，实施单 run 上下文连续性。
6. 保持进度治理为观察模式，直到有足够的生产/评测证据支持单独的新变更。

## 8. 验收标准

- 固定离线案例可在无外部服务条件下复现，产出可比较的基线和失败诊断。
- 启用输入合同的路径拒绝多当前任务、错误状态继承、无核验引用的已确认结论，以及最终请求中的约束丢失。
- 高价值 claim 的发布资格由确定性 resolver 和已注册的 Research gate 产生；模型/人工提交的自然语言观察不能绕过该门。
- 检索候选实验默认关闭，保留原始计划，记录血缘和回退，且没有成对评测证据不能扩大启用范围。
- 上下文续接没有可审计触发证据时不得进入实现或发布阶段；启用后能重组已接受证据、条件和来源修订。
- 新代码不引入 `backend/research` 对 legacy `backend/boards/paper_radar`、`interfaces` 或 `infrastructure` 的禁止依赖，也不创建平行状态/控制系统。

## 9. PRD 迁移与删除

本 PRD 替代下列重叠的拟议 Research 变更，并将其目录从活跃 OpenSpec 工作区删除：

- `research-agent-input-contract`
- `research-context-state-rollover`
- `research-evidence-review-resolution`
- `research-semantic-verification-reading-latency`
- `research-progress-governance`
- `add-research-evaluation-harness`
- `paper-rag-hybrid-query-rewrite-v1`

同时删除已经被 canonical `harness-graph` / Graph-only 合同替代、或没有可执行 PRD 的旧变更目录：

- `harness-workflow-graph-runtime`
- `framework-runtime-audit-repair`
- `workflow-runtime-p2-window01-contract-closure`

仍在执行或仍有独立价值的 `harness-runtime-execution-and-parallel-agent-orchestration`、`durable-event-runtime`、`model-aware-llm-context-preflight` 和 `research-workspace-efficiency` 不在本次删除范围。历史版本控制记录继续是删除文档的审计来源。
