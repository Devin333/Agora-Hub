## Context

NewsRoom 已经有几块可以复用的能力：`PlanCandidate` 和 `TaskPlanPolicy` 负责动态计划，`TaskPlanStageRunner` 负责 DAG readiness，`SubAgentRuntime` 负责单个子 Agent 的受控执行，`ChildAgentSupervisor` 负责有界的 child lifecycle，`AgentLoop` 负责主 Agent 的候选动作，`ToolExecutor`/`ToolBatchExecutor` 负责工具调用，durable event stream 负责 replay。它们目前没有形成一条统一的 Codex 式闭环。

当前动态 TaskPlan 虽然可以同时计算多个 ready task，但 `TaskPlanStageRunner` 仍在同步循环中逐个调用 worker；`AgentLoop` 的 `delegate` action 也只等待一个 child。这样主 Agent 无法在一次规划中真正 fan-out 多个独立任务，也无法以稳定的、可验证的结果包继续自己的下一轮推理。Research 动态分析路径同样通过一个 worker adapter 串行执行，没有接入 `ChildAgentSupervisor` 的 capacity、lease 和 recovery 语义。

本设计在现有 Harness 控制平面上补齐统一的 `fan-out -> execute -> verify -> fan-in` 合同。外层 Graph 仍然冻结，LLM 仍然只生成候选内容；Harness 负责计划接受、授权、并发、生命周期、验证、汇聚、重试/replan、持久化和恢复。设计必须兼容现有的单任务 worker，并且不引入第二套 workflow、queue 或 child lifecycle。为区分逻辑 join 范围和物理 capacity，本变更使用 `DispatchGroup` 与 `DispatchWave` 两层模型。

## Goals / Non-Goals

**Goals:**

- 让 parent Agent 或动态 TaskPlan 在一次受限候选中声明多个可并行的 child task，并由 Harness 统一调度。
- 在 stage policy、capability capacity、child capacity、concurrency reservation 和 side-effect fence 的交集内实现真实并行，而不是仅返回多个 ready task 后串行调用。
- 复用 `ChildAgentSupervisor`、`SubAgentRuntime`、`AgentRunner`、`ToolExecutor` 和现有 durable transcript/event/replay，不复制生命周期实现。
- 为每个 child attempt 保留独立身份、权限、预算、工具调用、transcript、result receipt 和 deterministic gate 证据。
- 在所有 required child 到达终态后按稳定顺序汇聚结果，并以一个带 group/wave/task identity 的结果包回传给 parent Agent。
- 对 partial failure、retry、bounded replan、cancel、lease reclaim 和 crash recovery 给出可重放的确定性语义。
- 让 Research dynamic analysis 在保持 `analysis_branch_refs`、`verify_claims`、quality gate 和 publication boundary 不变的前提下使用该能力。
- 保持现有单 child `delegate` 和单任务 worker 的兼容路径，并通过显式 serial adapter 降级。
- 通用 `AgentLoop` 的生产编排端口属于本变更交付范围；Research dynamic 是首个业务 opt-in，而不是通用能力的替代品。

**Non-Goals:**

- 不允许 LLM 修改外层 Graph、直接选择 worker implementation、授予 tool/memory 权限、决定 quality/pass、发布 artifact 或写入 active memory。
- 不在本变更中实现跨进程 exactly-once、分布式 scheduler、自动水平扩容或新的通用 workflow engine。本变更提供带幂等 receipt 的 at-least-once recovery；已确认的副作用不得重放，无法确认的非幂等副作用必须 fail closed。
- 不让 child Agent 之间通过隐藏 prompt 或未授权共享状态直接通信；交互只能通过 Harness 管理的 input/output refs。
- 不把部分未通过 gate 的 child 输出直接暴露为 Research 报告或 publication 输入。
- 不改变 static Research `ParallelAll` 的外层 Graph 语义，也不把动态 stage 变成可任意修改 Graph 的 agent。

## Decisions

### 1. 以 group/wave contract 连接规划、执行和汇聚

新增 versioned `ParallelDispatchRequest` 和 `ParallelDispatchResult` 作为 Harness 内部端口合同。request 为一个已接受的 plan version 创建一个 `DispatchGroup`，至少包含 `run_id`、graph identity、`stage_id`、`plan_id`、`plan_version`、完整且稳定排序的逻辑 task ids、required output roles、`join_policy`、`group_deadline`、`max_waves`、`max_parallelism`、总预算 envelope 和 correlation id。group membership 在 admission 后不可变，其中可以包含尚未 ready 的依赖任务。每个实际派发的 `DispatchWave` 至少包含 `group_id`、当前 ready 的有序 task instance ids、wave ordinal、effective parallelism、逐任务 budget/capacity reservation 和 idempotency key。result 以 `group_id` 为最终 join 范围，至少包含每个 task 的终态/result ref/checksum/attempt、所有 wave 的完成证据、join 状态、aggregate ref/checksum、诊断和 recovery/degraded 信息。

`DispatchGroup` 覆盖一个 plan version 的完整逻辑 join 范围，负责 dependency closure、required-role、aggregate 和 parent continuation；`DispatchWave` 只表示在当前 readiness 与 capacity 下实际启动的一批 child。依赖尚未满足或 capacity 不足时，后续 wave 必须复用同一个 group，不能隐式创建新的 join 范围。`wait_all` 的终态条件是 group 内全部必要 task 到达终态；role-complete aggregation 只能在 group join 后执行。

LLM 产生的是 `PlanCandidate` 或 `delegate_batch` candidate，不是上述已授权的 dispatch request。Harness 在接受 immutable `ValidatedTaskPlan` 后生成 group/wave request，重新解析 capability、tool/memory allowlist 和 budget；因此候选中的并发提示只能是意图，不能扩大 policy 上限或成为授权。

选择 group/wave contract 而不是让各 worker 自行汇报，是为了把 admission、child identity、join 和 replay 放在同一控制面。单任务 worker 通过 `SerialTaskExecutorAdapter` 包装为一个只有一项 task 的 group/wave，保持已有 `TaskRequest -> result` 接口。

candidate durable dedup key 固定为 `run_id + stage_id + parent_turn_id + action_correlation_id`，与 `candidate_checksum` 一起持久化。相同 key/checksum 复用原 accepted plan/group/submission/terminal observation，冲突 checksum 返回 `CANDIDATE_IDEMPOTENCY_CONFLICT` 且不得执行新 payload。`group_id` 来自 accepted plan identity 与 dedup key 的稳定 hash；新 plan version 的 replan 必须使用新的 correlation/dedup identity。

当前 admission 增量使用 `agora.harness-dispatch-group/v2`，分别计算稳定 group identity 与 immutable snapshot checksum。snapshot 锁定完整 accepted plan checksum、policy ref/checksum、父 Graph execution identity、静态 admission policy、membership 与预算 envelope；实时 available concurrency 和 pool reserved 不进入 immutable identity。配置 capacity demand 时必须覆盖整个 accepted plan，不能只绑定本轮 selected tasks。durable event sequence 是 admission revision，store 在同一 CAS 保护的 canonical prefix 上检查唯一 group、连续有界 wave ordinal 和 single active wave，不新增独立可写的 group 状态源。重启先从 verified replay 恢复 group 与完整 waves，再协调未结束的 spawn；持久化 wave terminal 后才开放下一 wave。本增量尚未完成跨重启绝对 deadline、统一 RefAuthority、生产多池容量 ledger 或 capacity-wait READY；原有完整合同保持不变。

所有 input/result/planning refs 和 memory namespace 统一通过 `RefAuthority`，校验 run、stage、tenant/owner、读写权限、artifact type、source checksum 和 pinned allowlist。跨作用域默认拒绝；共享必须由 policy 声明只读范围，candidate 不能自行授权，child 不得读取 sibling private refs。

### 2. Harness 作为唯一 fan-out/fan-in coordinator

新增或扩展 `TaskPlanBatchCoordinator`，由 `TaskPlanStageRunner` 和 `AgentLoop` 共同调用的 Harness-owned port。其顺序固定为：

1. 从 immutable plan 计算完整 dependency closure、required output roles 和稳定 task order，为该 plan version 创建一个 `DispatchGroup`，再计算当前 ready set。
2. 在 durable stream 中提交 group admission，原子记录 group membership、join scope、总预算 envelope、policy checksum 和边界；group admission 只锁定上限，不重复消费逐任务预算。没有 admission 记录不得启动 child。
3. 从 group 的当前 ready set 中按 capacity 创建一个 `DispatchWave`，原子记录 wave ordinal、选中的 task instances，以及每个 task 的 budget/capacity reservation；reservation ledger 使用 `RESERVED -> CONSUMED | RELEASED`，重试、取消、失败和 recovery 必须幂等结算。超出 capacity 或依赖未满足的 task 保持 durable PENDING/READY，等待后续 wave。
4. 按 plan stable task order 做 deterministic first-fit packing，每个 task 的全部 capability pool、resource key、concurrency slot 和预算必须 all-or-nothing reservation；不能满足的 task 留在 READY，继续检查后续 task。多池预留证据进入 wave checksum。先 durable 提交 admission、ledger 和逐 attempt spawn intent，再通过 `spawn_operation_key` 调用 `ChildAgentSupervisor`；保存 confirmed/unknown receipt 后核对全部 selected task 的 spawn 状态，只有 child 可追踪时才记录 dispatch。
5. 复用 `SubAgentRuntime` 完成 context/tool/memory/result gate 和 durable receipt；每个 child 独立完成或进入失败/取消/不确定终态。wave 结束后，若 group 仍有 ready task 且未达到 group 边界，继续创建下一 wave。
6. 对 group 内已终态的 child 做 deterministic join。join 顺序使用 plan 中的稳定 task order，不使用完成时间、线程返回顺序或 queue 顺序。
7. 只有 group join 通过 required role、output conflict、schema 和 aggregate gate 后，才向 parent Agent 或下游 Graph 发布一个 `ParallelDispatchResult`。

选择 coordinator 而不是直接在 `TaskPlanStageRunner` 中加入 `ThreadPoolExecutor`，是为了复用已有 capacity、lease、heartbeat、cancel、receipt 和 recovery 语义，避免出现两个互不相容的 child lifecycle。

### 3. 并发资格由 policy 和资源共同决定

每个 task 在 dispatch 前必须满足：依赖结果已经 durable success、输入 refs 可解析、capability binding 唯一且 pinned、预算已 reservation、工具和 memory namespace 在 allowlist 内，并且 task 的 side-effect class 允许并发。side-effect class 使用 policy 固定枚举：`READ_ONLY`、`EXTERNAL_IDEMPOTENT`、`MUTATING_SERIAL`、`FENCED_MUTATION`；资源冲突由 policy/capability binding 提供的 `resource_conflict_key` 判定，不能由 candidate 自行声明。只读分析任务可并行；副作用按下述 resource-key/receipt/fence 规则判断，不把所有外部写入一概全局串行化。`max_parallelism` 只能被 Harness 在授权上限内计算，不能被 candidate 或 child 提高。

policy 必须提供按 capability/resource pool 区分的 `CapabilityCapacity`，至少包含 pool identity、capacity、currently reserved、owner scope、reservation key/version 和有效期。task demand 可以涉及多个 pool；单一 capability 标量不能代表异构 capacity。所有 pool 与 supervisor capacity、stage limit、可用 concurrency reservation 共同约束 admission。缺失或过期信息 fail closed，多资源预留失败不得留下部分 reservation；wave terminal/recovery 按 key 幂等结算。

`READ_ONLY` 可共享资源 key；`EXTERNAL_IDEMPOTENT` 在同 key 下默认串行，只有 policy 明确允许且有幂等 receipt 才可并发；`MUTATING_SERIAL` 只序列化同 key，除非 capability 被标记为全局串行；`FENCED_MUTATION` 按 key 持有包含 owner、generation、TTL、续租和释放历史的 fence。不同 key 可按独立 fence 并发，不能把所有 mutation 隐式降为全局串行。fence 丢失或 generation/TTL 不确定时进入 `INDETERMINATE`/`HALTED`，禁止自动重试。

当两个或以上 task 独立、通过并发资格检查、`effective_parallelism >= 2` 且未启用 `serial_fallback` 时，coordinator **MUST** 并发启动最多 `effective_parallelism` 个 child attempt。只有 side-effect fence、有效 capacity 为 1 或显式 `serial_fallback` 才允许串行，并且必须记录稳定的降级原因。

当 adapter 不支持 wave 或运行环境未配置 supervisor 时，Harness 只能在明确启用的 `serial_fallback` policy 下逐个执行，并记录 `DEGRADED_SERIAL` 及原因；当 policy 要求真实并行时必须 fail closed。不得静默把并行请求变成串行，也不得在同一个 group 中途无事件地改变并行策略。

### 4. Parent Agent 只接收一次受控 join observation

`AgentLoop` 新增对 `delegate_batch` candidate 的解析和校验，但不负责线程、队列、重试、路由或质量判断。`AgentOrchestrationPort.submit(candidate)` 先返回 durable submission identity、group identity、dedup 状态和 bounded wait 信息；terminal observation 与 submission receipt 是不同合同。等待 capacity、在线 recovery 或 join 超过 bounded wait 时返回 `PENDING`，不能追加成功 observation 或启动下一轮 parent reasoning。Harness 通过 durable continuation 唤醒同一个 parent turn，以 `observation_id + observation_version` 幂等追加 terminal observation；重复读取保留同一 checksum。progress 只进入 inspection/metrics。

terminal observation 包括 group/wave 状态、稳定 task summaries、output refs/checksums、gate diagnostics、budget usage 和可安全展示的 failure/recovery 信息。summary 只能投影 durable、已通过 gate 的结构化 result fields、typed status 和 deterministic diagnostics，projection/replay 禁止 LLM 摘要。固定排序、字段选择、脱敏、UTF-8 截断、`summary_truncated` 和 projection version 进入 observation checksum。`ParentObservationLimits` 使用唯一 `max_observation_bytes` 字段，超限保留 identity、terminal outcome、checksum 和 continuation；详细内容只能以 checksum-bound artifact ref 表示。原始 hidden prompt、兄弟 child 私有 transcript、未授权工具结果和控制字段不进入 observation。

现有单 child `delegate` 通过一条兼容 adapter 转换为单 task group/wave；因此旧 AgentSpec 和旧 worker executor 可以继续使用。parent Agent 是否继续、重试还是结束，仍然由自身下一轮候选和 Harness 的 loop budget 共同约束，不能由 child 输出直接改变 workflow control。

### 5. 工具调用分为规划观察和 child 执行

需要外部事实的 planner 可以请求 policy 允许的、默认只读的 planning observation。流程固定为 `PlanningObservationRequest -> PlanningObservationReceipt -> PlanCandidate(source_observation_refs) -> candidate validation`。receipt 先绑定 `run_id`、`stage_id`、`planner_turn_id`、`policy_checksum` 和独立的 planning correlation id；candidate 生成后只能通过不可变 source ref 关联。每个 planning turn 必须受 `max_planning_tool_calls`、`planning_timeout` 和 `planning_budget` 限制。该调用通过 Harness 控制的 `ToolExecutor` 完成，持久化 tool receipt 和 checksum 后再进入 candidate validation。planner 不能调用副作用工具，也不能把工具返回值当作授权或 quality verdict。

child Agent 的工具权限继续由 resolved `SubAgentSpec` 和 capability binding 计算，工具调用归属于对应 child attempt，并复用 `ToolExecutor`/`ToolBatchExecutor` 的预算、allowlist、receipt 和 idempotency。工具结果只能成为 child candidate 的证据，不能绕过 task gate 或直接写入 parent state。

### 6. Failure、join 和 replan 使用显式策略

每个 group 固定一个由 policy 提供的 join policy。`wait_all` 要求 group 内所有必要 task（包括后续 wave）到达终态后再聚合，适用于 Research 的角色完整性；`fail_fast` 在不可恢复失败时关闭 group admission、取消尚未完成的 sibling，并保留已完成结果和取消证据。两种策略都必须显式记录，不能依赖实现默认值。每个 group 必须同时具有 `join_deadline`、`max_join_wait_seconds` 和 `max_waves`；超过边界时进入 typed timeout/halt，不得无限轮询。

可重试失败只创建有界的新 attempt；达到 `max_task_attempts` 后，Harness 只能执行明确的 patch 操作：`ADD_REPLACEMENT_TASK` 以终态失败的 logical task 为 target，新增 task 必须有新的 logical id/task instance；`SKIP_PENDING_TASK` 和 `UPDATE_PENDING_DEPENDENCY` 只能作用于尚未 admission 的 pending task。每次 replan 必须创建新的 immutable `plan_version`、新的 `DispatchGroup` 和新的 correlation id；旧 group 只保留审计证据，迟到结果不得写入新 projection。已验证的 sibling result refs 可按 policy 显式复用，但不得重新挂接旧 task instance。运行中、已完成、required role、policy、gate 和 outer Graph 不可被 patch 修改。缺少 required role、结果冲突、证据不可验证或 crash 后状态不确定时，必须进入 typed failure/halt，而不是猜测或选择一个“先完成”的结果。

predecessor 不可恢复失败后，coordinator 按 stable DAG order 将未 admission 的直接和传递后继转为终态 `BLOCKED_DEPENDENCY`，记录 `TASK_BLOCKED_UPSTREAM_FAILURE` 并释放未消费 reservation，不创建 child。`wait_all` 将这些终态纳入 join，返回 `DEPENDENCY_BLOCKED`/`REQUIRED_ROLE_MISSING`。replacement replan 重新验证 dependency closure，不改写旧 group blocked task。`max_waves` 包括 initial/retry waves；耗尽时按稳定顺序关闭未 admission task，记录 `WAVE_LIMIT_EXCEEDED` 或相应 dependency failure，禁止永久等待。

版本化 `BudgetReservation` 包含 token/time/tool-call/optional-cost 上限、owner scope、reservation key、parent/attempt allocation 和 ledger version。对每种预算维度保持 `consumed + released + outstanding_reserved <= group_envelope`；wave admission 原子预留，硬上限耗尽停止执行并记录 `BUDGET_EXCEEDED`，按已消费和未消费部分结算。retry 使用新 attempt reservation，cancel/reclaim/reconcile 按 key 幂等结算；replan 不直接继承旧 group 未结算余额。

### 7. State machine and event contract

G1 task contract 使用 `TaskProjection`、`TaskInstance`、`TaskPlanProjection` v3，包含 `ADMITTED`、`CANCELLED`、`INDETERMINATE`、`QUARANTINED` 和既有 `BLOCKED_DEPENDENCY`；Graph-only `TaskInstance` 保留既有 deterministic ID 算法，但构造和反序列化均验证 task-instance/idempotency/fencing identity，不能用重算 envelope checksum 掩盖身份替换。旧 v2 只属于离线 migration 范围，不由 live reader 静默解释。task transition 不得改变 task definition、跳过 attempt 序号、替换 active attempt 或改写终态证据。`READY`、`ADMITTED`、`DISPATCHED`、`RUNNING` 必须持有已分配的 active attempt。成功状态只携带已提交 result；`PENDING`、`BLOCKED_DEPENDENCY`、`CANCELLED`、`INDETERMINATE`、`QUARANTINED` 不携带 active instance。所有失败终态共享 dependency failure 分类，关闭未 admission 的后继；只有 policy 尚允许的 `FAILED` retry 暂不传播终态。

真实 Stage 在 `TASK_WAVE_ADMITTED` 对应 projection 中将选中任务从 `READY` 转为 `ADMITTED`，与 wave、预算和 spawn intents 同批持久化；确认 dispatch 后再进入 `DISPATCHED` / `RUNNING`。scheduler 不把 `ADMITTED` 当作新 ready 候选，checkpoint/replay 保留其 active attempt；恢复交还既有 wave coordinator，不生成普通 queue message 或 queue reclaim。共享 task/group/wave transition validators 仅验证合同，retry policy、完整 cancellation/quarantine receipt history 和授权仍由各自后续任务的 owner 实现。

每个 `DispatchGroup` 使用 canonical 状态：`PLANNED -> ADMITTED -> DISPATCHING -> RUNNING -> JOINING -> SUCCEEDED | FAILED | CANCELLED | INDETERMINATE | HALTED`。尚有 READY task 时允许 `RUNNING -> DISPATCHING -> RUNNING`。`JOINING -> REPLAN_PENDING -> SUPERSEDED | FAILED | HALTED` 是 durable 状态分支，不是隐式诊断；`REPLAN_PENDING` 为非终态，不向 parent 暴露为最终 outcome。`DispatchWave` 使用 `PLANNED -> ADMITTED -> DISPATCHING -> RUNNING -> TERMINAL`，terminal outcome 为 `SUCCEEDED | PARTIAL_FAILED | FAILED | CANCELLED | INDETERMINATE | RECLAIMED | DEADLINE_EXCEEDED`。每个 transition 必须声明唯一 owner、规范 event name、幂等 key、允许后继、terminality 和 recovery 行为。同一 group 只允许一个 active wave admission transaction。

| Group transition | Canonical event | 唯一 owner | Recovery / terminal rule |
| --- | --- | --- | --- |
| `PLANNED -> ADMITTED` | `TASK_GROUP_ADMITTED` | `TaskPlanBatchCoordinator` | 使用 group id + plan version + correlation id 去重；admission 未完成不得 spawn child。 |
| `ADMITTED -> DISPATCHING -> RUNNING` | `TASK_WAVE_ADMITTED`、`TASK_WAVE_DISPATCHED` | `TaskPlanBatchCoordinator` | wave 以 group id + wave ordinal 去重；缺失 dispatch 仅可补建同一 wave。 |
| `RUNNING -> JOINING` | `TASK_GROUP_JOIN_WAITING` | `TaskPlanBatchCoordinator` | 只在必要 task 已终态、达到 deadline 或 group 关闭后进入。 |
| `JOINING -> SUCCEEDED` | `TASK_GROUP_JOINED` | deterministic aggregator | 仅在 required role、schema、gate 和 aggregate 全部通过后终态。 |
| `JOINING -> REPLAN_PENDING` | `TASK_GROUP_REPLAN_PENDING` | replan coordinator | 非终态且不向 parent 发布；只能继续到 `SUPERSEDED`、`FAILED` 或 `HALTED`。 |
| `JOINING/REPLAN_PENDING -> FAILED/INDETERMINATE/HALTED` | `TASK_GROUP_FAILED`、`TASK_GROUP_INDETERMINATE`、`TASK_GROUP_HALTED` | `TaskPlanBatchCoordinator` | 终态后禁止新 wave；迟到 receipt 仅进入 quarantine/audit。取消请求只能在 active state 关闭 admission 后进入 `CANCELLED`。 |
| `REPLAN_PENDING -> SUPERSEDED` | `TASK_GROUP_SUPERSEDED` | replan coordinator | 仅在新 plan version/group 被接受后发生；旧 group 保留证据，不能接纳迟到结果。 |

`TASK_GROUP_*` 与 `TASK_WAVE_*` 的 payload 必须包含 `run_id`、`stage_id`、`plan_id`、`plan_version`、`group_id`、适用时的 `wave_id`、`task_instance_id`、attempt、correlation id 和 idempotency key。child/tool receipt 可以有自己的生命周期事件，但必须携带可回溯到同一 group/wave 的因果引用。

`fail_fast` 的关闭顺序固定为：记录失败 -> 关闭 group admission -> 发出 sibling cancel -> 等待 cancel receipt 或 lease expiry -> 将迟到成功 receipt quarantine 到旧 group -> 记录单一 group terminal event。已完成 sibling 结果只可进入诊断包，不得在 group 已关闭后改变 aggregate。

### 8. Durable event 是执行和 replay 的事实源

至少记录 `TASK_GROUP_ADMITTED`、`TASK_WAVE_ADMITTED`、`TASK_WAVE_DISPATCHED`、`TASK_GROUP_JOIN_WAITING`、`TASK_GROUP_JOINED` 以及每个 child 已有的 spawn/start/receipt/result/terminal/cancel/retry/reclaim 事件。每个 event 按 event family 携带适用的 group/wave/plan/task/attempt correlation 和幂等 key；candidate/planning 事件不得填充尚不存在的 group 或 attempt 身份。checkpoint 必须包含 group/wave identity、budget reservation/release、join policy、每个 attempt 的 transcript/output refs 和 checksums、aggregate projection 及 stream sequence。`ToolRuntime` 不新增独立事件事实源，planning/child tool receipt 通过 adapter 关联到相同 attempt/group stream。

在线 recovery 先读取 intent、supervisor operation status、receipt、result artifact 和 ledger，再补写缺失 transition。intent 存在但 receipt 缺失不是未启动的证据；unknown spawn 进入 `SPAWN_UNKNOWN`，不能盲目重复 spawn。confirmed child 不重复启动，缺 dispatch event 时复用 receipt 补事件；reservation 存在但 admission 缺失时按同一 key 补 admission 或 halt ledger conflict，不重复扣费。完全相同的 receipt 重投幂等复用；同 identity 不同 checksum/body 才是冲突，必须拒绝且保留审计。

online recovery 可执行已审计的 supervisor status/termination/reconcile，以及 policy 允许、幂等性可验证、deadline 内的新 attempt。每次调用记录 `RECOVERY_STATUS_READ`、`RECOVERY_RECONCILED`、`RECOVERY_RETRY_ADMITTED` 或 `RECOVERY_HALTED`，禁止 live LLM 重新规划和重复已确认或不确定的非幂等副作用。offline replay 只读 durable candidate/plan/patch/events/receipt/result history/aggregate/checkpoint，不调用 live LLM/source/RAG/tool/worker/supervisor/queue/publication。测试用调用即失败的 adapters 和零调用计数证明隔离。

每个 attempt 的 `spawn_operation_key = group_id + wave_id + task_instance_id + attempt`。`TASK_WAVE_ADMITTED`、reservation ledger 与 `TASK_ATTEMPT_SPAWN_INTENT` 必须同一事务或等价 durable batch 提交；supervisor 按 operation key 幂等接受并保存 `SPAWN_CONFIRMED`/`SPAWN_UNKNOWN` receipt。batch 部分成功时逐 task reconcile。完整 attempt history 保留 failed、rejected、cancelled、indeterminate、reclaimed、quarantined，accepted projection 不能替代 `result_history_for()`。checkpoint 包含 spawn intent/receipt、完整历史索引、ledger、aggregate/observation checksum 和 stream sequence。

TaskPlan 的原子 transition port 为每个 event 固定同序号 projection，并以提交前 projection checksum 做 CAS。选中 wave 的 `TASK_READY`、预算预留、`TASK_WAVE_ADMITTED` 和全部 intent 同批发布；未选中 task 不预扣 attempt budget。wave event 的 `budget_before_checksum`、`budget_after_checksum` 绑定账本前后状态，child envelope 的 `ledger_version` 指向该 attempt 的实际 reservation revision。已确认所有 child 后，wave dispatch 与 task dispatch/start facts 再提交。完整批次重投只校验原事件引用的历史 projection，不能用最新 projection 替代，也不能修复或覆盖缺失的历史 artifact。离线 replay 拒绝不完整或交错的 intent batch，以及与 ledger identity/allocation/revision 不符的预算凭据。

### 9. 通用 AgentLoop 生产交付与 Research opt-in 验证

本变更必须完成通用 `AgentLoop` production composition：`delegate_batch` candidate、`AgentOrchestrationPort`、joined observation、配置/availability diagnostics、入口 smoke 和旧单 child compatibility。Research dynamic 是首个业务 opt-in，用于验证固定角色和 publication boundary；它不能替代通用 AgentLoop 的生产接线。

第一生产接入点是现有 `build_dynamic_paper_analysis_graph_definition()` 的 `dynamic_analysis_stage`。该 stage 继续只接收 `document` 和 `evidence_pack`，必须得到 `analysis.structure`、`analysis.contribution`、`analysis.experiments` 三个角色并通过 deterministic role gates 与 aggregate，之后才写入 `analysis_branch_refs` 并进入固定的 `verify_claims` 与 quality gate。static workflow 仍为默认路径。

生产组合必须解析真实 `ChildAgentSupervisor`、worker registry、durable run/event store、artifact verifier 和 tool ports；缺失任何 required binding 都返回稳定 unavailable error。fake worker、FakeLLM 和 in-memory store 仅用于测试。

## Bounded policy defaults

除非 stage policy 明确覆盖，否则 group 使用以下有界默认值：`max_tasks_per_group=8`、`max_waves=16`、`max_parallelism=3`、`max_task_attempts=2`、`max_replans=2`、`max_group_runtime_seconds=900`、`max_join_wait_seconds=300`、`max_planning_tool_calls=3`、`planning_timeout_seconds=30`。`ParentObservationLimits` 默认使用 `max_task_summaries=8`、`max_summary_bytes=2048`、`max_diagnostics=16`、`max_refs=16`、`max_observation_bytes=16384`。`group_deadline` 取 stage deadline 与 `admitted_at + max_group_runtime_seconds` 的较早者；`max_join_wait_seconds` 只限制进入 `JOINING` 后的等待。`max_waves` 统计初次和 retry 的 wave admission，replan 创建新 group，但仍受 run-level `max_replans` 限制。缺少必需的 capacity、join、budget policy 时 fail closed；缺少 observation limit 时使用上述安全默认值，不能把 policy 中明确禁止的能力打开。

## Risks / Trade-offs

- [并发放大预算和外部资源压力] -> group admission 和每个 wave admission 都执行 reservation，使用 stage/run/capability/child 四层上限；超限任务留在 READY，不创建 child。
- [并行 task 写入相同输出造成非确定性] -> validator 在 dispatch 前拒绝未声明的 output collision；需要合并的角色必须绑定 deterministic aggregator，禁止 last-writer-wins。
- [线程完成顺序影响报告] -> join 和 aggregate 按稳定 plan order，所有 result checksum 纳入 aggregate checksum。
- [child 或 supervisor 崩溃导致重复副作用] -> 复用 lease、attempt id、receipt 和 idempotency；未确认的副作用按既有 fail-closed/reclaim policy 处理，不自动重放不确定调用。
- [旧自定义 worker 不支持 wave] -> 提供显式 `SerialTaskExecutorAdapter`，并在 inspection/metrics 中公开 `DEGRADED_SERIAL` 原因；生产 policy 要求并行时拒绝隐式降级。
- [AgentLoop 暴露过多上下文] -> observation 只包含 security-projected summaries 和 artifact refs；原始 prompt、secret、兄弟 transcript 继续由 Harness boundary 隔离。
- [动态路径与 static Research 结果不一致] -> static 默认不变；dynamic 先使用独立 workflow id/version、golden event history、replay 和 publication regression 检查，完成 parity 后再考虑默认切换。

## Migration Plan

交付按 G1 Contract、G2 Coordinator、G3 AgentLoop、G4 Research、G5 Release 分 gate。G1 固定 schema/state/dedup/ref/budget/event/replay 契约；G2 验证 overlap、multi-pool packing、spawn crash 和 dependency block；G3 验证真实 production port、durable continuation、redaction 与 legacy golden fixtures；G4 验证 Research golden contract parity、static default、failure/publication boundary 与 offline replay；G5 单独验收 telemetry、alert、运行中 group recovery 和回滚演练。实现测试通过不等于授权启用默认路径。

1. 先落地 group/wave/result/event schema、状态机、validator、capacity/reservation contract 和 fake supervisor contract，补齐 admission、join、replay、budget 和 serial adapter 的单元测试；功能通过 feature flag 保持关闭。
2. 将 `TaskPlanStageRunner` 的 ready-task dispatch 改为调用 coordinator，验证多 wave 与 group join，再接入真实 `ChildAgentSupervisor`。
3. 完成通用 `AgentLoop` production composition：`delegate_batch` candidate、单 child compatibility adapter、parent join observation、配置/availability 和入口 smoke；验证旧 AgentRunner 测试不变。
4. 将 `ChildAgentSupervisor` 接入真实 Research dynamic composition，在 Research workflow 上运行并发上限、角色聚合、partial failure、retry/replan、cancel、lease recovery、crash recovery 和 offline replay 的 golden tests。
5. 仅在通用 AgentLoop smoke、Research publication parity、metrics 和 replay evidence 全部通过后开放 dynamic Research production opt-in；static workflow 继续保持默认。

回滚时只关闭 dynamic parallel feature flag 或切换到显式 serial adapter，不修改已经接受的 plan version。正在运行的 group 必须按其 pinned policy 完成恢复或进入可诊断 halt；不能把同一 run 静默改写成 static workflow 或重新生成 candidate。

Research parity 使用固定 golden inputs，逐字段比较 required role completeness、`analysis_branch_refs` 结构/归属/checksum 验证、claim evidence、quality verdict、reader/card/artifact 契约与失败时无 downstream success refs。允许差异必须在 fixture 显式列出，例如 duration、wave/attempt identity 和 timing；不要求逐字相同 LLM 文本。legacy compatibility 同样使用旧调用方 golden fixtures，覆盖 result/error/stop_reason/diagnostics/trace、取消和 recovery，而不是只比较字符串。

## Explicit scope decisions

- 本变更只保证单进程 bounded execution；跨进程 transport 是后续变更，但不得改变 group/wave、状态机和 receipt contract。
- Research dynamic 固定使用 `wait_all`；通用 AgentLoop 可由 policy 注册 `fail_fast`，但枚举必须由 registry/inspection 固定，LLM 不能自由选择。
- planning observation 没有 stage policy 明确 allowlist 时默认拒绝；ToolRuntime 行为不扩展，只通过 adapter 传递 attempt identity、budget、receipt 和 correlation。
