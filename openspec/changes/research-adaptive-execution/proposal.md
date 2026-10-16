## Why

用户应当提交研究目标、材料和约束，而不是选择 `static` / `dynamic` 或决定是否启动 planner / 多 agent。当前已有搜索意图理解、固定分析 Graph、受控 TaskPlan 和 AgentLoop，但尚未形成“自动选择合适工作量 → 根据实际执行反馈局部升级 → 在同一质量标准下交付”的产品闭环。

本变更把自适应限定在可验收的 Research 业务阶段，复用现有 Harness 控制权与运行资产，不建立第二套调度器，也不把模型自评视作完成证明。主产品合同见 `prd.md`；实验分析的受控多轮交付切片见 `analysis-agent-loop-prd.md`，其行为正式纳入 `research-controlled-analysis-loop` capability。

## What Changes

- 增加统一的 Research 自适应执行合同：用户表达目标、材料与预算约束，系统内部选择固定 worker、受限 AgentLoop 或受控 TaskPlan。
- 增加执行任务意图候选与服务端接纳策略；复用现有结构化候选能力，但不把面向搜索的 `GuidedIntent` 当作完整执行计划。
- 保留固定业务主链与粗粒度阶段；已知任务使用既定分析路径，不额外调用动态 planner。只有必要时才在已声明的分析/补证范围内规划、分解或委派。
- 补齐实验分析的真实受控多轮闭环：Graph-bound worker 复用现有 LLM，通过明确动作协议调用真实只读证据工具；新增片段经既有 authority 接纳、版本化并触发受影响结果再验证。保留其他必需分析角色，不新增 planner、递归委派或专用模型 checkpoint。
- 区分局部动作/输出校验、Harness VERIFY 与顶层发布；纳入真实调用/重试计量、无进展终止、scope 隔离和持久恢复，不把旧 worker 的 `final_output` 包装或空工具 registry 当作能力完成。
- 增加基于已接纳执行事实的有界升级：缺证、可修复候选失败等可申请补充工作；权限拒绝、状态损坏、取消和预算耗尽不得借升级绕过。
- 明确 Graph identity、授权、预算、质量、发布与回放不随策略变化；共用请求级总预算，记录所有接纳、拒绝、升级及停止原因。
- 解耦执行策略与质量 profile；用户要求与任务合同决定验收标准，不能因选择较省成本的策略降级。
- **BREAKING**：在完成依赖验收并启用服务端自适应 release 后，新用户请求不再通过 `dynamic_analysis` / `dynamic_task_plan` 等字段控制路线；外部策略覆盖被明确拒绝。运营 rollout 开关和已冻结运行的恢复不属于用户模式选择。
- 增加离线与受控真实模型的成对评测、成本/延迟/质量发布门，以及不改写活动 run 的回滚规则。首版不训练专用 router，不引入无边界自主联网、多论文通用研究或自动 skill 演进。

## Capabilities

### New Capabilities

- `research-adaptive-execution`: 定义任务意图、无用户模式开关的执行接纳、局部按需规划、反馈升级、预算与验收边界、用户可见状态及恢复行为。
- `research-controlled-analysis-loop`: 细化首版实验分析的真实动作与工具循环、证据接纳、三级验收、有限修复、共享预算、恢复隔离和诚实交付；不新增模型部署或竞争性运行时。
- `research-adaptive-execution-evaluation`: 定义固定/自适应/始终规划基线、留出评测、质量与成本目标、依赖资格、受控发布及回滚证据；补充实验分析两组专项评测与总体三组发布资格的严格分离。

### Modified Capabilities

- `research-dynamic-analysis-stage`: 将历史的调用者显式动态选择演进为发布资格满足后的服务端自动接纳；更新 Graph-only 名称，保留冻结 identity、固定业务阶段、必需输出角色和下游门。允许声明并校验任务意图及同论文修订内的补充证据引用，不授权跨阶段或任意来源扩展。

## Impact

- 产品入口：现有 Research 分析服务及 Desktop analyze 请求/进度投影；不重写搜索、导入、文档解析或整个 Reader QA 产品。
- 业务层：任务意图、执行候选、版本化策略、分析阶段组合、证据引用与质量 profile 的适配。
- 框架层：只扩展已有 Harness Graph/TaskPlan/AgentLoop 的候选接纳与既有事件合同；不得创建竞争性的 runtime、store、event log 或 publication authority。
- 组合与基础设施：exact capability/binding 注册、模型接入、既有持久化/预算/恢复适配和配置投影。
- 依赖：`harness-runtime-execution-and-parallel-agent-orchestration`、`research-evidence-context-quality-foundation` 的适用资格；`research-local-desktop-readiness` 和 `paper-agent-desktop-app` 继续拥有其运行管理与桌面壳职责。
- 本次只编写 PRD 与 OpenSpec 规划材料；所有实现任务均未完成，不改变生产默认行为，不调用收费模型。
