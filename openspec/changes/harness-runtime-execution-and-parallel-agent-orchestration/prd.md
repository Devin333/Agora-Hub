# Harness Runtime Execution And Parallel Agent Orchestration PRD

## 1. 文档信息

| 字段 | 内容 |
| --- | --- |
| 产品/能力 | Harness Runtime Execution Safety and Parallel Agent Orchestration |
| OpenSpec change | `harness-runtime-execution-and-parallel-agent-orchestration` |
| 来源 | 合并 `harness-runtime-execution-safety` 与 `harness-codex-style-parallel-agent-orchestration` |
| 状态 | Draft for implementation |
| 当前基线 | Execution Safety 23/28；Parallel Orchestration 23/46 |
| 目标使用者 | Harness、AgentLoop、Research、平台运维和发布评审人员 |
| 产品原则 | `LLM as worker, Harness as control plane` |

本 PRD 是两个前置 change 的合并产品合同。它保留已有代码和证据的历史，不把已经完成的本地实现重新定义为未完成；未完成项统一在本 change 的任务、规格和发布证据中收口。

## 2. 一句话概述

为 Harness 建立从执行能力准入、Child Agent 生命周期、durable runtime event，到并行 group/wave 编排、结果验证、父任务继续和 Research fan-out 的单一生产运行合同，并通过受控 rollout 证明它不会产生未经授权、重复或无法回放的执行。

## 3. 背景与现状

当前仓库已经具备执行 profile、provider registry、ChildAgentSupervisor、canonical durable event、TaskPlan、group/wave schema、capacity reservation、Research Graph factory 和部分并行 admission 实现。两个旧 change 的实现进度分别为 `23/28` 和 `23/46`，但剩余任务分散在两个 owner 中：

- Execution Safety 还缺进程重启、工具超时、child 丢失、取消不确定性和外部副作用去重场景；还缺真实 production caller scan、完整 smoke 和部署 provider capability/rollback 证据。
- Parallel Orchestration 还缺真实 `SubAgentRuntime`/`AgentRunner`/ToolRuntime child 接线、结果 authority、多 wave join、retry/replan、父任务 durable continuation、Research fan-out parity 和 G1-G5 rollout evidence。
- 如果只完成其中一边，系统可能在没有真实执行能力证明时启动并行 child，或者具备 sandbox contract 却无法在 restart/redelivery 后安全继续。

## 4. 产品目标

### 4.1 必须达到

1. 所有外部进程、解析器、工具和 Child Agent 都通过 Harness-owned execution capability admission，缺少 provider capability 时 fail closed。
2. 每个已启动 attempt 都有带身份、状态、终止确认、原因、输出引用和 capability profile 的 durable terminal receipt。
3. Child 的 spawn、status、wait、cancel、close、heartbeat、lease、reclaim 和 restart recovery 使用同一个 Harness supervisor 和 operation identity。
4. Turn、tool、approval、context、worker 和 child lifecycle facts 进入 canonical durable event，projection/replay 不调用 live dependency。
5. 一个 `DispatchGroup` 固定一个 plan version 的 join 范围，capacity、dependency、retry 和 replacement 只产生有界 wave/新 plan，不产生隐式第二套 join authority。
6. 每个 child result 在被 aggregation 或 parent continuation 使用前，经过 deterministic identity、schema、artifact、transcript、budget、receipt 和 quality gate 验证。
7. Parent 只消费 durable gated observation；`PENDING`、redelivery、restart 和 terminal checksum 都可幂等处理。
8. Dynamic Research 并行分支保持现有 evidence、claim verification、quality、reader/card、artifact 和 publication gates；任何 required role 不完整都不能发布成功成果。
9. 默认状态保持 `FEATURE_DISABLED`，只允许显式 allowlisted rollout；依赖、质量、部署能力或 rollback 证据缺失时不得声明 production-ready。

### 4.2 成功定义

当系统能够在真实 composition 中完成一次受控的多 child Research run，持久记录 admission、wave、receipt、verification、join、continuation、recovery 和 rollback 证据；当离线 replay 与在线已接受历史 checksum 一致且没有 live worker/tool 调用；当缺 provider、错误 identity、gate failure、取消不确定或发布依赖缺失时都能返回有界、可诊断、不可越权的结果，本 PRD 达到发布标准。

## 5. 范围

### 5.1 包含

- `ExecutionEnvironment` profile、provider capability、filesystem/network/environment/argv/timeout/cancellation/termination contract。
- Harness-owned child supervisor、lease/heartbeat、restart recovery、indeterminate/quarantine 和 no-duplicate-side-effect handling。
- Canonical runtime event schema、redaction、cursor、projection、replay 和 operator read model。
- `DispatchGroup`、`DispatchWave`、TaskPlan admission、multi-pool reservation、join、retry、replacement、fail-fast、reclaim 和 conflict handling。
- Real `SubAgentRuntime`/`AgentRunner`/ToolRuntime composition、result verifier、durable parent submission/continuation 和 legacy single-child adapter。
- Dynamic Research structure/contribution/experiment fan-out、field-level golden parity、existing quality/publication successors 和 rollout states。
- Focused tests、compile/test/smoke、strict validation、caller inventory、deployment capability evidence、telemetry 和 rollback rehearsal。

### 5.2 不包含

- 不替换 Graph compiler、durable event owner、side-effect authority 或 Research quality gate。
- 不引入跨进程分布式 scheduler、无界 queue、自动扩缩容、跨 run 公平调度或 exactly-once 传输承诺。
- 不把 worker、LLM、MCP metadata、candidate metadata 或 parent model 变成 routing、quality、authorization、memory/skill mutation 或 publication authority。
- 不默认启用 parallel Research，不因 feature flag 存在而宣称 production qualification。
- 不复制通用 Context compaction、已有 deterministic RAG planner、已有 RAG evaluator 或已有 heartbeat/lease/stall detector。

## 6. 关键状态与边界

### 6.1 Execution attempt

`ExecutionRequest` 必须绑定 Graph/activity/attempt identity、profile、provider capability、roots、environment、argv、timeout、cancel policy、budget 和 approval evidence。`ExecutionReceipt` 必须记录 terminal status、termination confirmation、reason code、output refs、checksum 和 exact attempt identity。无法确认终止的带副作用 attempt 进入 `INDETERMINATE`，不能自动重试。

### 6.2 Parallel group

`DispatchGroup` 是一个 accepted plan version 的固定 logical join scope；`DispatchWave` 是该 group 在当前 readiness、capacity 和 reservation 下的实际执行批次。旧 group 的迟到 receipt 只能进入 quarantine。Group 只有在所有 required task 到达合法终态且 aggregation gate 通过后，才能产生 parent observation。

### 6.3 Parent continuation

提交返回 durable `submission_id`、`group_id`、dedup 状态和 bounded wait 信息。父 AgentLoop 在 group 未产生 terminal gated observation 前不得推进私有推理。重启或重复投递必须根据 observation identity、version 和 checksum 复用已接受结果，不得重新调用 child。

### 6.4 Research boundary

Child 只能获得已接受的 document/evidence refs、role objective、policy-approved tools 和 branch scope；不得复制 parent 私有对话。Research 聚合必须保留 `analysis_branch_refs`、claim evidence、quality verdict、reader/card/artifact/publication references，并在 required role 缺失、结果不确定或 gate failure 时阻断 downstream success。

## 7. 验收门

合并迁移的逐项对应关系与冻结基线见 `evidence/merged-traceability-20260923.md`。旧 runtime change 的 context preflight 验收以及 `durable-event-runtime` 9.5、`harness-workflow-graph-runtime` 1.1 依赖仍由原 change 跟踪，当前均未验收；删除旧目录不会解除这些发布前置条件。

| Gate | 验收内容 |
| --- | --- |
| G1 Contract | schema、identity、dedup、ref authority、budget、event、replay 和 strict validation 通过 |
| G2 Coordinator | multi-pool packing、reservation、spawn reconciliation、multi-wave join、retry/replan 和 cancellation 通过 |
| G3 AgentLoop | durable submission、PENDING、same-turn continuation、redelivery、legacy compatibility 和 hidden-context isolation 通过 |
| G4 Research | structure/contribution/experiment fan-out、field-level golden parity、quality/publication preservation 和 failure history 通过 |
| G5 Release | default-off、dependency states、telemetry、provider capability、rollback、replay 和残余风险证据完整 |

任一 Gate 未通过，composition 只能保持 `FEATURE_DISABLED`、`DEPENDENCY_UNAVAILABLE` 或显式 `DEGRADED_SERIAL`，不能进入 `ENABLED_PARALLEL`。

## 8. Rollout 与回滚

先运行 fixture/replay 和 focused contract tests，再运行受控 generic AgentLoop，最后仅对 allowlisted Research scope 开启 parallel fan-out。回滚只关闭新请求准入或选择显式批准的 serial adapter；活动 group 保留原 policy、budget、bindings、receipts、reservations 和 history，按 cancel/reconcile/halt 规则结束。回滚不得关闭质量门、授权检查、transcript/artifact verification，也不得重放不确定副作用。

## 9. 交付与归档条件

- 新 change 的 proposal、design、specs、tasks、PRD、实现证据和 strict validation 全部通过。
- `python -m scripts.dev compile`、`python -m scripts.dev test`、`python -m scripts.dev smoke` 和范围匹配测试通过；失败必须修复根因。
- 部署 provider capability、rollback、golden parity、replay 和 production caller evidence 已记录。
- 旧 `harness-runtime-execution-safety` 与 `harness-codex-style-parallel-agent-orchestration` 目录已在合并规格校验后按用户要求删除，迁移提交为 `1f4a8ae2`；Git history 保留其来源。目录迁移不代表实现或发布验收完成，剩余工作由本 change 继续跟踪。
