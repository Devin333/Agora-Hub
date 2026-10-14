## ADDED Requirements

### Requirement: Routine dispatch is gated by qualified local capabilities
Routine SHALL 默认不可后台执行。启用和每次准入 SHALL 验证可信 owner/delegation、有效 grant、持久化 store/queue、durable events、所需 provider、精确 Graph/gate 绑定及取消/恢复能力。缺少依赖 SHALL 保持阻断并报告可行动原因，SHALL NOT 静默使用内存队列、fake worker、未经授权的 provider 或云端 worker。

#### Scenario: User opts in before durable queue qualification
- **WHEN** 用户确认 Routine，但本机 durable queue 或必要 event contract 未通过资格检查
- **THEN** 系统不开始 source/model/tool 调用，显示具体阻断项
- **AND** 不把已存在的模块或通过的其他 change 任务当作该组合的发布证据

### Requirement: Harness remains the single execution authority
scheduler SHALL 只产生可信触发候选，application service SHALL 通过既有 Harness 提交入口创建有界 Research Run。Harness SHALL 独占路由、审批、`PLAN -> EXECUTE -> VERIFY`、retry/replan/halt、memory write authority 和 publication。LLM SHALL 只生成候选；接口 SHALL NOT 直接调用 executor/store，Research SHALL NOT 依赖 interfaces、concrete infrastructure 或 legacy paper_radar。

#### Scenario: A worker candidate requests bypassing a failed gate
- **WHEN** 模型要求直接发布推荐或禁用失败的 VERIFY gate
- **THEN** Harness 保持原 gate 与有界失败策略，不发布未获准产物
- **AND** phase transition 与失败原因写入 durable transcript

### Requirement: Occurrence admission and dispatch intent are durably idempotent
日历 occurrence SHALL 使用 owner/workspace、Routine ID、revision、restore epoch 与逻辑 slot 的稳定身份；手动 occurrence SHALL 使用对应授权的稳定 request ID。store SHALL 原子执行唯一性检查，并将 occurrence 与 dispatch outbox 在同一事务中持久化。跨 store 的 queue/event 投递 SHALL 使用稳定 ID、inbox 和幂等重投，不声称跨库原子事务。

#### Scenario: Process crashes after storing an occurrence before enqueue
- **WHEN** occurrence/outbox 事务已提交但 queue 投递尚未完成时进程崩溃
- **THEN** 重启继续投递同一 occurrence，不丢失已接受触发，也不生成第二个逻辑任务

#### Scenario: Two schedulers discover the same due slot
- **WHEN** 两个 tick 或实例并发插入同一个 slot，随后消息又被重复投递
- **THEN** 持久化唯一性约束只允许一个 occurrence，consumer 重复处理不会生成第二个 Harness Run

### Requirement: Run submission has a recoverable stable identity
每次 occurrence SHALL 绑定唯一的 Harness invocation key 与 Run。提交响应丢失后 SHALL 查询或幂等重投同一身份；恢复 SHALL 使用既有 durable checkpoint、receipt 和绑定版本，SHALL NOT 从头新建 Run 来猜测先前是否成功。Run 状态 SHALL 由 Harness 权威记录投影而非 Routine 的独立执行状态机产生。

#### Scenario: Harness accepted a run but its response was lost
- **WHEN** worker 重启时只知道已发出提交，尚未保存 `run_id`
- **THEN** 通过稳定 invocation key 找回或幂等确认原 Run，不重复收费或重新启动论文分析

### Requirement: Leases and concurrency limits fence stale workers
Routine SHALL 至多有一个活动 Run，包含等待审批的运行。并发领取 SHALL 通过 durable lease/fence 准入；失去 lease 或授权代际的 worker SHALL 无权开始新的副作用。执行期间后续 slot SHALL 至多合并为一个有界待执行 occurrence，并记录被合并/跳过的范围；其准入前 SHALL 再验时效和权限。

#### Scenario: A stale worker returns after lease reassignment
- **WHEN** 原 worker 失去 lease 后恢复，而另一个 worker 已获得新 fence
- **THEN** 旧 worker 不能发起 tool/model/source 调用或发布，返回结果进入既有受控核验路径
- **AND** 新 worker 不盲目重复旧 worker 结果未知的远端动作

#### Scenario: Several schedule slots arrive while awaiting approval
- **WHEN** 同一 Routine 的 Run 仍在等待审批且多次触发到期
- **THEN** 不创建并行 Run，积压最多保留一个有界合并候选
- **AND** 审批过期或授权失效时按原 Run policy 停止，而非以新触发绕过等待

### Requirement: Budgets are finite and shared across attempts
每次 Run SHALL 固定绑定 Graph/gate/tool/model policy 版本及有限的 `max_turns`、`max_replans`、retry、wall time、论文数量、调用数、token 和费用上限。准入 SHALL 原子预留 Routine 与 owner 周期共享额度，所有重试、恢复、补跑及子任务 SHALL 计入同一预算体系。修改 revision、重启或还原旧预算快照 SHALL NOT 自动重置已消费额度；无法安全估算或核对时 SHALL 阻断新的收费调用。

#### Scenario: Concurrent routines compete for the same remaining budget
- **WHEN** 同一 owner 的两个 Routine 同时申请超过剩余额度的预留
- **THEN** 原子预算检查拒绝超额准入，不由两次独立读取各自认为余额充足

#### Scenario: Retry follows budget exhaustion or an uncertain charge
- **WHEN** 一次调用耗尽额度或其远端收费结果无法确认，随后发生 retry、重启或 revision 更新
- **THEN** 新尝试不能获得重置预算，未知消费保留预留并进入对账或等待用户处理
- **AND** budget exhaustion 不被投影为 `no_change` 或成功完成

### Requirement: Current authorization is checked at effect boundaries
Harness SHALL 在 source/model/tool 调用、memory context 构建、publication 和新的内容提醒前验证当前 grant、scope、owner、expiry、epoch、fence 与预算。授权失效的持久化提交 SHALL 是撤销生效点；此后未经新的有效 authority 决定 SHALL NOT 获得后续动作许可。暂停、撤销与取消 SHALL 复用既有 control-plane authority，不能由模型批准。

#### Scenario: Permission is revoked between queue admission and publication
- **WHEN** Run 已入场，但用户在下一动作许可或报告发布前撤销授权
- **THEN** 后续动作不获准，未发布候选不成为成功产物或推荐提醒
- **AND** 已许可且已发出的不可逆请求被明确区分，不能声称其结果已撤回

### Requirement: Unknown effects require reconciliation rather than blind retries
系统 SHALL 区分可安全重试、已有可验证 receipt 以及结果未知的副作用。对于不具备可证明幂等性的远端请求，未知结果 SHALL 进入既有 `indeterminate`/reconciliation 或 halt，不重置 invocation identity、不提前释放预留。取消 SHALL 区分请求已接受、终止已确认和终止结果未知。

#### Scenario: Source or model response is lost after sending a request
- **WHEN** 请求已发出但进程退出前未能确认结果，provider 又无可验证的幂等/查询保证
- **THEN** 恢复显示未知结果并按 policy 对账或停止，不以“网络重试”重复发送并假定免费

#### Scenario: Cancellation cannot confirm remote termination
- **WHEN** 用户取消后系统无法确认已经发出的调用是否停止
- **THEN** 状态显示取消处理中或结果未知，并保留关联 receipt/预算记录
- **AND** 不显示已确认终止，也不因此允许另一个 Run 绕过并发安全检查

### Requirement: Canonical events support redacted side-effect-free replay
Routine 生命周期、授权变更、occurrence、准入、预算、Run 关联与结果 SHALL 使用既有 canonical durable event 合同记录，并在持久化/导出前脱敏。Harness phase transition SHALL 保持 durable transcript。Replay SHALL 只读取历史与已记录 outcome 重建投影，不调用 provider、触发 Run、发布产物或发送新提醒。

#### Scenario: A completed routine history is replayed twice
- **WHEN** 操作者从相同历史重建两次状态、结果列表和执行详情
- **THEN** 投影保持一致，source/model/tool 调用数、新 Run 数、新发布数和提醒投递数均为零
- **AND** 导出不含 credentials、前端 token 或未获准的私人上下文
