## Why

Agora 已有 Graph Harness、Research、个人记忆和桌面工作台，但用户的持续研究意图尚未形成一个可授权、可暂停、可恢复、可追溯的长期任务闭环。本变更在论文专用桌面产品之后增加长期研究助手与例行任务，让用户授权助手定时追踪论文，而不是依赖永远运行的 LLM 对话或另建一套执行引擎。

## What Changes

- 增加 owner/workspace 范围内的 `AssistantProfile`，关联已有 Conversation、Research Run、个人记忆与产物；长期身份与一次有界执行分离，不重复建立资料库、聊天存储或运行控制器。
- 增加默认未启用、由用户明确确认的 `Routine` 与版本化授权：确认研究主题、真实数据源、时间与时区、预算、工具、记忆及远程数据处理范围后，才允许后台创建 Run。
- 定义编辑、暂停、恢复、撤销、归档、手动单次执行与授权过期的语义；执行范围变更必须重新确认，模型和外部材料只能提出候选，不能授权自己。
- 复用现有 scheduler、queue、Graph Harness、Wait/approval、durable events 和 publication authority，补齐本机持久化 occurrence、触发去重、并发隔离、预算预留、崩溃恢复与未知副作用处理；不假定当前 Redis 默认路径已经满足桌面交付。
- 首个完整业务场景为定时论文追踪：真实收集、按稳定论文标识去重、基于获准上下文生成带来源的推荐、确定性验证、保存报告与关联资料，并区分无更新、部分失败和执行失败。
- 在既有工作台增加助手设置、例行任务、执行记录和应用内结果收件箱；仅在有新推荐、重要失败或需要用户操作时提醒，普通无变化运行保持安静。
- 以独立 feature gate 分阶段交付。本次仅形成 OpenSpec 设计与待办，不实现或启用后台执行，不将其他 active change 的代码存在视为部署验收完成。

## Capabilities

### New Capabilities

- `research-assistant-identity`: 长期助手身份与既有 owner、workspace、conversation、run、memory 的关联和权限边界。
- `research-routine-lifecycle`: Routine 候选、显式授权、不可变执行版本、生命周期、触发配置及单次运行合同。
- `research-routine-execution`: 本机持久化调度、occurrence 幂等、预算与授权准入、Harness 执行、取消、恢复和无副作用 replay。
- `research-paper-watch`: 真实论文追踪、来源覆盖、去重、候选推荐、确定性质量门与产物发布。
- `research-routine-workspace`: 工作台状态、低噪声结果收件箱、用户控制、备份恢复和渐进发布验收。

### Modified Capabilities

无。本变更新增 opt-in 产品层合同，不放宽现有 Harness、Research、owner、memory 或 publication 要求，不修改通用 scheduler 对其他调用方的行为。

`paper-agent-desktop-app` 的首版排除后台自主行动和电脑操作，其既有交付范围保持不变。本变更是后续独立启用的论文例行任务能力；用户对一个明确版本的持续任务授权可允许该版本内的定时运行，但不构成通用自主行动许可。

## Impact

- 业务与接口：预计在 `backend/research/domain`、`backend/research/application`、`backend/research/ports` 增加助手与 Routine 合同，在 `interfaces/services`、API 和 composition 中接入已有 owner、workspace、Research 与个人记忆服务；接口不得直达 executor/store。
- 运行时与存储：复用 `framework/workers/scheduler`、queue、`framework/harness` 和 `framework/events` 的中立合同，必要时补充本机事务适配；持久化由 `infrastructure` 实现，路径与生命周期由既有 local desktop runtime 管理。不默认增加 Redis、PostgreSQL、云端 worker 或第二个调度守护进程。
- 产品：扩展既有 `frontend/src/features/paper-agent-app` 与桌面 capability/运行状态投影，不另起通用 Agent 控制台。
- 前置依赖：`paper-agent-desktop-app`、`research-local-desktop-readiness` 的正式 owner、数据与生命周期合同；`durable-event-runtime` 的交付/replay 资格；现有 Harness、Research 和 personal memory 的受测生产路径。动态并行只在 `harness-runtime-execution-and-parallel-agent-orchestration` 自身资格通过后另行启用，首期不依赖动态 fan-out。
- 明确不包含：电脑操作、任意 shell/browser 自动化、任意任务模板、新闻/邮件/日历/购物、公开发布、云端代跑、开机自启、跨设备同步、Agent 市场、活动 skill 自动修改或未授权收费调用。
