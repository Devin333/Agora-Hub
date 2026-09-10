# Task 2.5 recovery acceptance

## 当前状态与范围

集中七文件 recovery batch4 已通过：**166 passed in 200.19s，exit 0**。
日志为 [task-2-5-recovery-batch4.log](../../../outputs/task-2-5-recovery-batch4.log)。
主控已确认五个 changed Python 文件在该批次执行前后 SHA256 一致。

完整 required smoke 已通过，exit 0，使用同一批冻结源码：
[task-2-5-final-smoke.log](../../../outputs/task-2-5-final-smoke.log)。
pytest 为 **3577 passed、23 deselected、31 warnings，3759.44 秒**。
compile 通过；离线 AgentLoop succeeded、network_calls=0；source validation
is_valid=true、error_count=0、warning_count=0。五个 Python 文件执行后哈希一致。
task 2.5 的本项验收完成，checklist 已勾选；不代表完整 G2–G5 或生产发布验收。
本矩阵补录后，strict OpenSpec validation 与 `git diff --check` 已通过。
最终文档与 checklist 更新后，提交前再次执行 strict validation 和 diff 检查。

task 2.3 已在 `ab9d65ac` 验收，task 2.5 是当前推进项。
本增量只恢复原 admitted wave 和 attempt 的事实，不创建新 attempt 或 replacement
plan，不扩展完整 child production composition、工具执行、join/replan 或 parent
continuation。已有后续任务的局部实现不能替代本项 crash 矩阵的证据。

## 最终实现说明

合同依据是 PRD section 8、online recovery/offline replay 边界及对应 design/spec。
以下实现说明综合主控当前源码审查与已通过的集中测试；文档整理未重新执行测试。

- `reconcile_spawn_intents` 在 live status 查询前核对完整 intent set。
  `_reconcile_spawn_status` 先持久化 `RECOVERY_STATUS_READ`，再读取 supervisor
  status、校验 child/attempt identity，记录 reconciliation 或 halt。
  缺 receipt、缺 supervisor 内存 operation record 都不构成“从未启动”的证明。
- confirmed receipt 保持原 operation、child 和 attempt identity；缺失 dispatch
  时仅恢复原 wave 的 transition。部分 confirmed/unknown 的 batch 逐 task 对账，
  unknown 不触发 confirmed sibling 重启，也不能提前整体 dispatch。
- unknown 使用 `child_runtime_indeterminate` 的 pending-wave 处理，保留原 admitted
  wave 和未结算 reservation。`TASK_GROUP_INDETERMINATE` durable event 提交成功后
  才更新内存 group 状态，避免事件写入失败后只有内存提前进入终态、后续恢复被短路。
- 在线恢复中的 supervisor `wait`（包括其内部 status 读取）和 `close` 有独立的
  operation intent/outcome 审计：`RECOVERY_OPERATION_INTENT`、
  `RECOVERY_OPERATION_RECONCILED`、`RECOVERY_OPERATION_HALTED`。
  缺 audit sink 或 intent 持久化失败时拒绝调用；成功记录必须携带已验证的 typed
  terminal receipt，包含精确 identity、checksum 和 termination confirmation。
- `close` 已成功但 outcome 发布失败时，后续恢复可以核验 CLOSED handle 上保留的
  原 terminal receipt，再幂等完成恢复。live validator 与 replay reducer 使用相同
  边界；replay 固定每个 operation 首个 terminal receipt checksum，拒绝替换凭据，
  也拒绝 status/operation audit families 复用同一个 recovery id。
- event schema 与完整 replay 已注册新增审计事件，要求完整 attempt correlation。
  offline replay 只消费 durable history，不用 live supervisor 查询补历史。

## 12 项验收矩阵

下列函数均来自既有日志中的真实 test node 或主控已核对的当前源码。
batch4 使用 `-q`，其日志只有总数，不逐项列出函数；因此“166 passed”证明整个选定
批次通过，具体行为以本表的源码审查和对应断言为准。第 10 行的 lifecycle 测试
由完整 smoke 覆盖并通过；第 12 行明确区分纯 reducer、只读 queue recovery 和完整系统演练。

表中 `audit` 指
[tests/framework/harness/task_plan/test_spawn_recovery_audit.py](../../../tests/framework/harness/task_plan/test_spawn_recovery_audit.py)，
`admission` 指
[tests/framework/harness/task_plan/test_spawn_admission.py](../../../tests/framework/harness/task_plan/test_spawn_admission.py)，
`schema` 指
[tests/framework/harness/agent_loop/test_orchestration_event_schema.py](../../../tests/framework/harness/agent_loop/test_orchestration_event_schema.py)。
行号定位为本次冻结源码的审查位置。

| 编号 | Crash / 不一致场景 | 真实测试函数或待补定位 | 已核对的证据与边界 |
| --- | --- | --- | --- |
| 1 | Admission transaction 失败 | `admission::test_durable_admission_failure_exposes_no_wave_intent_or_child`，参数 `TASK_READY` / `TASK_WAVE_ADMITTED` / `TASK_ATTEMPT_SPAWN_INTENT` | 零 worker，无 wave/intent；ledger version 0、零 records/counters；attempt 0，逻辑 READY 顺序保留。 |
| 2 | Intent 已提交、receipt 缺失 | `audit::test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts` | SQLite reopen + fresh supervisor 后产生 2 unknown、2 status、2 halt；零 confirmed/dispatch，2 reservation 保持 RESERVED。 |
| 3 | Child 已启动、receipt 写入失败 | `audit::test_stage_recovers_canonical_admission_and_checkpoints_without_new_children[receipt]` | 重新创建 coordinator/runner，复用原 supervisor 恢复原 child；不新增 child。该 fixture 的 event/artifact store 为 fake，不能单独证明数据库 reopen。 |
| 4 | Confirmed receipts 已有、dispatch 缺失 | `audit::test_stage_recovers_canonical_admission_and_checkpoints_without_new_children[before-dispatch]`；`audit::test_sqlite_reopen_with_same_supervisor_recovers_confirmed_wave_once` | 原 wave 的 admitted/dispatched/completed 各一次；spawn_batch 总数 1，原 attempt 1，2 CONSUMED reservations、2 ledger records。 |
| 5 | 部分 confirmed / unknown | `audit::test_partial_confirmed_and_unknown_recovery_does_not_dispatch_or_respawn` | 两次 status 查询得到 1 confirmed、1 unknown；保留 1 active child，worker 调用数不变，spawn trap 未触发，无 dispatch，replay reservations 均 RESERVED。 |
| 6 | 重复 recovery | `audit::test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts`；`audit::test_fresh_supervisors_reaudit_missing_state_without_spawning_or_dispatching` | 同 coordinator 重复恢复不新增 history/status/spawn；两个 fresh coordinator 各重新查询两个 task，形成 4 个独立 recovery id 与对应 halt，但只保留 2 unknown receipts / 2 attempt records，reservations 均 RESERVED。 |
| 7 | RUNNING wave 恢复 | `audit::test_stage_recovers_canonical_admission_and_checkpoints_without_new_children[after-dispatch]`；`audit::test_restart_finishes_active_wave_before_join_without_spawning_again` | 恢复已有 active wave，不重新 spawn。same-supervisor SQLite 测试另核对原 wave/attempt 与一次消费；这里不代表 task 2.9 完整 join 验收。 |
| 8 | Supervisor 丢失 operation state | `audit::test_confirmed_receipts_are_not_rewritten_when_fresh_supervisor_loses_state`；`audit::test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts` | 既有 confirmed 不被 fresh supervisor 的 missing 状态覆盖；缺 receipt 时按 unknown 停止。未证明 fresh supervisor 能重新获得原活动进程。 |
| 9 | Identity / checksum / terminal evidence 冲突 | `schema::test_recovery_schema_requires_complete_attempt_correlation`；`schema::test_recovery_operation_success_schema_requires_terminal_evidence`；`audit::test_recovery_wait_requires_confirmed_terminal_receipt_before_success_audit`；`audit::test_recovery_wait_rejects_forged_operation_identity_before_success_audit`；`audit::test_replay_rejects_recovery_receipt_replacement_after_first_confirmation`；`audit::test_replay_rejects_reused_recovery_id_across_status_and_live_operation` | schema 验证必需身份及 terminal evidence；运行时拒绝未确认 termination 与错误 identity；replay 拒绝重新签名的 receipt replacement 和跨 audit family recovery id 冲突，不能用 schema 检查替代这些运行时断言。 |
| 10 | Reservation / admission 不一致 | `admission::test_replay_rejects_non_atomic_intent_history[admission]`；`admission::test_replay_rejects_tampered_spawn_budget_reservation`；`admission::test_replay_rejects_self_consistent_budget_not_backed_by_ledger`；`test_task_lifecycle_contract::test_admission_redelivery_rejects_missing_budget_reservation_without_inserting_it` | batch4 覆盖移除 wave admission 的非法 intent 历史、篡改 reservation checksum、重新签名但 ledger version/allocation 不符的 reservation；均拒绝。另一个已定位的 lifecycle 测试构造 admitted task 缺 budget reservation，断言 reservation_missing 且不补写 ledger；该文件不在 batch4，必须以本轮完整 smoke 的最终结果验收。 |
| 11 | Status / audit / indeterminate event 持久化失败 | `audit::test_indeterminate_event_write_failure_retries_same_coordinator_without_duplicate_status`；`audit::test_recovery_audit_write_failures_preserve_retryable_receipt_boundary`；`audit::test_recovery_wait_audit_failure_blocks_live_call_until_retry`；`audit::test_online_recovery_without_audit_sink_does_not_wait_or_close`；`audit::test_closed_child_recovery_retries_after_close_outcome_audit_failure`；`audit::test_supervisor_identity_conflict_is_not_unknown_receipt` | indeterminate event 故意失败两次后仍可重试；status intent、confirmed receipt、reconciled 与 dispatch 写入故障均覆盖；wait intent 失败/缺 sink 阻止调用；close outcome 故障后重用原 receipt；status identity conflict 记录 halt，不降格成 unknown。 |
| 12 | Offline replay | `audit::test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts`；`audit::test_receipt_write_failure_recovery_is_audited_idempotent_and_replayable`；`test_task_plan_recovery::test_checkpoint_roundtrip_and_missing_queue_projection_recovery_are_offline` | supervisor status/spawn 与 worker invocation traps、重复 checksum/projection 相等，以及 checkpoint recovery 的 worker.calls == 0 已覆盖。纯 `TaskPlanReplayReducer.replay` 接收 plan/event/result/patch/receipt 数据，没有注入 supervisor/tool/LLM/queue 执行端口；`TaskPlanRecoveryService` 的可选 queue projection reader 是另一个显式只读边界。尚未做全部生产 live adapters 同时安装 trap 的完整系统演练，不宣称该层面的验证。 |

验收必须同时审查物理调用计数、durable identity/event、wave/attempt/ledger 不变量；
单一返回状态或 test 名称不能替代这些证据。

## SQLite、artifact 与 supervisor 连续性

`test_sqlite_reopen_with_same_supervisor_recovers_confirmed_wave_once` 使用真实 SQLite
event store 的同一数据库重新打开，然后重新创建 store/coordinator/runner；artifact store
仍为 fake，supervisor 仍是原对象。其断言为 spawn_batch 总数 1，recovery status
2 次加 wait 内部 status 2 次，wait 2 次、close 2 次；4 组 operation intent/outcome
identity 对应，并保留原 wave、attempt、reservation/ledger 计数。

`test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts`
另覆盖 fresh supervisor 的 operation state 丢失，证明缺 receipt 时保守停止及离线
重建稳定；不证明 supervisor 进程退出后能重新连接原 child、恢复 durable artifact，
或实现跨完整进程重启的成功续跑。

这两类 fixture 分别证明 event-store reopen 和 supervisor state loss 的边界。
它们共同提供有用的 crash recovery 证据，但不能称为全套 durable-artifact /
supervisor-process restart 演练。

## 验证结果与失败根因

本轮集中运行先收齐修改、固定五个 Python 文件，再执行七文件 batch4；执行后五个
hash 均保持一致。覆盖原五个 recovery 模块
`test_spawn_recovery_audit`、`test_spawn_admission`、
`test_spawn_receipt_history`、`test_task_plan_recovery`、
`test_recovery_evidence_contract`，加 orchestration event-schema 与 parallel
lifecycle replay。批次总结果和 full smoke 状态分开记录：

| 检查 | 当前结果 | 证据 |
| --- | --- | --- |
| 集中 recovery batch4 | 166 passed，200.19 秒，exit 0 | `outputs/task-2-5-recovery-batch4.log` |
| 五个 changed Python 文件冻结 | batch4 执行前后 SHA256 一致；full smoke 继续使用该批源码 | 主控执行记录；不沿用首轮失败批次的旧 hash |
| Strict OpenSpec / diff whitespace | 矩阵补录后均通过，exit 0 | 主控本轮执行记录 |
| Required full smoke | exit 0；3577 passed、23 deselected、31 warnings，3759.44 秒；compile、offline AgentLoop、source validation 通过 | `outputs/task-2-5-final-smoke.log` |
| Task 2.5 本项验收 | 本矩阵与完整 smoke 已核对；checklist 已勾选，按精确路径提交 | 不扩展到后续任务或生产发布 |

失败历史保留原日志，以下仅说明修复根因，均不是当前仍在运行的批次：

| 历史批次 | 当时结果 | 根因与后续处理 |
| --- | --- | --- |
| Batch 1 | 85 passed、3 failed，154.93 秒 | 测试误将 DISPATCHING 预期为 ADMITTED、错误读取 StoredEvent 外层 reason_code、status 计数忽略 wait 内部读取。审查进一步发现 wait/close 缺逐调用 recovery audit，随后补齐生产审计与负例，不能仅改计数掩盖缺口。日志：`outputs/task-2-5-recovery-batch.log`。 |
| Batch 2 | 89 passed、11 failed、66 setup errors，53.07 秒 | operation schema 的 required 数组重复声明 child_id，导致 JSON Schema catalog 初始化失败。修复字段构造来源，保留各 operation schema 的必需 child_id。日志：`outputs/task-2-5-recovery-batch2.log`。 |
| Batch 3 | 153 passed、13 failed，199.11 秒 | 新 fixture 的 graph_ref 不符合 graph_id@version；attempt 断言读取外层 nullable task_instance_id。修复为 canonical group admission 的 Graph identity，以及 details.history_record 中的精确 attempt identity。日志：`outputs/task-2-5-recovery-batch3.log`。 |

另外修复了 indeterminate event 写入前提前改变内存终态的问题，并对 close 已成功、
outcome 持久化失败后的原 terminal receipt 重用补充验证。以上收集后的源码与测试已在
batch4 通过，旧文档中的“实现进行中”“新增测试尚未运行”“smoke 尚未开始”不再代表
当前状态。完整 gate 的最终结果已在上表记录；warnings 为 FastAPI/Starlette
弃用提示，命令另有 PyMuPDF API 弃用提示，不属于测试失败。
