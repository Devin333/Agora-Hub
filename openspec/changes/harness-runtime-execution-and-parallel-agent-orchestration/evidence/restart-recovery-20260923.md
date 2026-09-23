# Child Restart And Cancellation Evidence

日期：2026-09-23

## Durable path

`DurableChildAgentEventLog` 使用现有 `TransactionalStateRuntimePort` / `TransactionalStateReaderPort` 和 checksum/CAS 保存完整 child lifecycle history；它没有建立第二个持久化 authority。`ChildAgentSupervisor` 可以从该 reader 重建 committed handles、terminal receipts、budget reservation 和 operation identity。

这里复用的是 canonical backend 的 transactional state 边界；尚不能据此声称 lifecycle facts 已接入 canonical event stream、cursor 和投影，也不能把 CAS revision 当作资源 owner fencing。

## 验证

```text
E:/Anaconda3/python.exe -m pytest tests/framework/harness/subagents tests/framework/execution_environment -q
161 passed, 2 skipped

E:/Anaconda3/python.exe -m pytest tests/framework/harness/task_plan/test_parallel_orchestration.py -q
46 passed
```

新增 SQLite 跨对象重开场景验证：

- 已提交 child result 在 parent/supervisor 重开后直接复用，恢复 worker invocation 为 0；
- cancellation 无法确认 termination 时持久化 `LOST` 与 `termination_confirmed=False`，恢复后继续占用 capacity，replacement 被阻断；
- 已完成 worker 的 admission identity 重放不重新调用 worker；该测试的副作用计数使用内存列表，未覆盖外部副作用提交中断；
- 同一 operation identity 的 parent/child/task/attempt/tools/memory/budget/transcript/lease 漂移被拒绝。

Docker provider 真实 integration 另见 [docker-qualification-20260923.md](docker-qualification-20260923.md)，其 5 个显式 opt-in 场景与 execution-environment 回归合计 `37 passed, 2 skipped`。

## 边界

这些证据支持本地 supervisor 对象重建、committed result 复用和取消不确定性保留。`interfaces/composition/research.py` 当前创建的生产 supervisor 尚未注入 `DurableChildAgentEventLog`；测试也没有杀死并重启独立进程、验证外部副作用提交窗口或证明生产恢复路径完成。因此任务 2.2/2.4 保持未完成。

任务 2.1 同样保持未完成：Docker 的本地五项测试没有覆盖所有资源策略及主动取消路径。它们不证明 dynamic Research golden parity、生产部署镜像供应链、多机恢复或 rollback release gate。

组合测试曾出现 `test_fresh_supervisors_reaudit_missing_state_without_spawning_or_dispatching` 调用计数变化。已定位为 fixture 同步缺口：receipt append 抛错前整个 wave 已提交，异常返回时已接纳的 worker 仍可能尚未进入 `invoke`。测试现在通过公开 `supervisor.wait()` 等待已接纳任务结束，确认两个初始任务各调用一次，再检查恢复前后调用快照不变；同构的 indeterminate event write failure 场景使用相同同步方式。

本轮同时修复了 `cancel` 的 operation identity 校验：读取任何终态 receipt、发取消事件或调用 worker 前，必须验证 operation 属于目标 child。覆盖 sibling operation 活跃、已完成、未知 operation，以及正确 operation 幂等取消，错误请求均不能改变任一 child 或触发 worker cancel。

```text
E:/Anaconda3/python.exe -m pytest tests/framework/harness/subagents -q
131 passed in 3.03s

E:/Anaconda3/python.exe -m pytest tests/framework/harness/task_plan/test_spawn_recovery_audit.py tests/framework/harness/task_plan/test_parallel_orchestration.py tests/framework/harness/task_plan/test_spawn_receipt_history.py -q
96 passed, 2 warnings in 180.67s
```

本轮修改后的完整 smoke 已结束：`3676 passed, 23 deselected`，离线 AgentLoop 与 source validation 均成功，整条命令退出码 0，详见 [qualification-20260922.md](qualification-20260922.md)。这些结果不补足上述生产恢复与独立进程证据缺口。

## 下一批接线前置条件

代码核查确认 Research composition 使用一个跨 run 共享的 supervisor 来限制总容量。生产接线必须保留这一范围，并明确跨重启稳定的 admission scope、业务 tenant/run/operation 绑定以及该资源的排他控制权，不能简单给每个 run 创建独立 supervisor 或给多个进程注入同一个无 owner 约束的全局日志。

`recover()` 当前会清空 futures 等内存索引，需限制在开放准入前；无法解析的 spawn 记录当前可能被忽略，不能以此推断容量已释放。未确认终止的 active/LOST 记录必须继续保留占用。全局持久日志的事件/字节上限还需保证终态可写，不能通过截断旧历史或换 key 解除占用。

TaskPlan 的 capacity CAS 和 transcript/result recovery 各自保留原有职责。下一批独立进程测试应在 durable barrier 指定的 spawn、transcript、terminal receipt 和 TaskPlan result 提交窗口中断进程；已验证的结果须零重调用复用，不确定外部副作用须保持阻断或由原资源 owner reconciliation。当前证据没有执行这些场景。
