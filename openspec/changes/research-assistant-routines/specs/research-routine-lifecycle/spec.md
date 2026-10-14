## ADDED Requirements

### Requirement: Routine creation is inert until explicit confirmation
系统 SHALL 支持用户手动建立或由模型建议 `paper_watch` Routine 候选，默认保存为不调度的 `DRAFT`。持续执行 SHALL 要求用户确认明确的研究主题、来源、时间窗、触发与时区、工具/provider、数据与记忆范围、预算、有效期、保存行为及通知策略；自然语言意图、安装升级或保存草稿 SHALL NOT 代替确认。

#### Scenario: A conversation asks for daily paper tracking
- **WHEN** 用户在对话中提出“每天帮我追踪这个主题”，但未确认结构化任务定义
- **THEN** 系统只能展示和保存可编辑候选，不创建后台 Run 或收费请求

### Requirement: Authorization binds an immutable execution revision
`RoutineGrant` SHALL 绑定 owner/workspace、Routine ID、不可变 revision/digest、权限范围、有限有效期和授权代际。服务端 SHALL 校验并重建可信执行定义，不信任客户端自报 digest 或超出权限的工具/任务类型。Routine payload SHALL 只保存受控凭据引用，SHALL NOT 持久化登录 token、API key 或其他原始秘密。

#### Scenario: Confirmation payload adds an unsupported tool
- **WHEN** 客户端在确认候选时加入 shell 工具、其他 owner 凭据或未支持的任务类型
- **THEN** 应用服务拒绝授权，原草稿不成为可运行任务

### Requirement: Execution-affecting edits require renewed authorization
修改主题、来源、触发、provider、工具、memory/data scope、预算、有效期或提醒内容策略 SHALL 生成新 revision，使旧执行授权失效并进入 `NEEDS_CONFIRMATION`。系统 SHALL 停止旧版新准入并对其活动 Run 发出受控取消；新版本确认前 SHALL NOT 执行。仅显示名称等不影响执行的编辑 SHALL NOT 扩张权限。

#### Scenario: Scope changes while an old occurrence is queued
- **WHEN** 用户扩大来源范围并保存新版但未确认，旧版 occurrence 同时到达 worker
- **THEN** worker 拒绝旧授权，新版也不运行
- **AND** 新版确认只从确认后的新触发开始，不重放同一个旧 slot

### Requirement: Pause and cancellation are separate controls
`PAUSED` SHALL 阻止新的 occurrence 入场和尚未入场的队列项，已准入 Run SHALL 只在原授权仍有效时继续。界面 SHALL 明示暂停不等于取消当前运行，并提供独立取消操作。恢复 SHALL 重验授权、预算和 capabilities，SHALL NOT 补跑主动暂停期间的 slot。

#### Scenario: User pauses a running routine
- **WHEN** 一个 Run 正在执行而用户暂停 Routine
- **THEN** 新触发不能进入执行，现有 Run 仍可按原授权完成
- **AND** 用户可以另外请求取消，取消状态来自 Harness 而不是本地 UI 乐观完成

#### Scenario: User resumes after a week of intentional pause
- **WHEN** 授权仍有效的用户明确恢复 Routine
- **THEN** 系统从恢复后的下个计划 slot 开始，不为暂停期间创建一周的补跑任务

### Requirement: Revocation and archival fence future effects
撤销、归档或授权过期 SHALL 先持久化授权失效与执行 fencing，再请求 Harness 受控取消。后续 tool/model/source 调用、发布及新的内容提醒 SHALL 校验当前授权；旧审批 SHALL NOT 恢复失效授权。归档 SHALL 保留可审计历史且不可直接重新调度，复制 SHALL 产生新草稿。

#### Scenario: An approval arrives after revocation
- **WHEN** 用户撤销一个等待审批的 Routine 后，旧审批被提交或重复投递
- **THEN** Run 不能恢复外部调用或发布，事件记录拒绝原因
- **AND** 已发出的请求与既有产物不被虚假标记为已撤回

#### Scenario: Grant expires before the next trigger
- **WHEN** scheduler 处理一个授权已过期的 Routine
- **THEN** Routine 显示 `NEEDS_CONFIRMATION` 及过期原因，不创建可执行的新 Run

### Requirement: Manual execution does not enable a schedule
系统 SHALL 提供带 request idempotency key 的单次立即运行，并要求有效 grant。`DRAFT`/`PAUSED` 的确认 SHALL 生成绑定 owner/workspace、精确 revision/digest 和 request ID 的 one-shot grant，原子绑定唯一 occurrence，仅授权该 occurrence 的有界执行且不启用持续调度。`NEEDS_CONFIRMATION`/`REVOKED` SHALL 先取得新的有效授权，`ARCHIVED` SHALL 仅允许复制为新草稿。重复请求 SHALL 关联同一次 occurrence；已有活动 Run 时 SHALL 返回其引用，不绕过同一 Routine 的并发限制。

#### Scenario: Double-click run once on a paused routine
- **WHEN** 用户确认一次运行后重复提交相同 request ID
- **THEN** 仅存在一个逻辑 occurrence 和一个绑定 Run，Routine 仍为暂停状态

#### Scenario: One-shot authorization is reused for a different occurrence
- **WHEN** 相同单次 grant 被再次提交，但请求试图绑定另一个 request ID、revision 或 occurrence
- **THEN** 应用服务拒绝新增绑定，不产生第二次运行或将该 grant 用作持续调度权限

### Requirement: Calendar triggers pin timezone and DST policy
首期 Routine SHALL 仅支持每日、每周结构化日历计划及手动单次执行，并复用既有 scheduler 时间计算合同。日历计划 SHALL 保存明确 IANA timezone、本地逻辑 slot 和 UTC `scheduled_for`；默认值必须在确认页可见，SHALL NOT 隐式继承系统或 scheduler 时区。DST 缺失时刻 SHALL 跳过并记录原因，重复时刻 SHALL 只采用第一次对应的 UTC instant。

#### Scenario: Device timezone changes after confirmation
- **WHEN** 用户改变设备时区但没有编辑已授权 Routine
- **THEN** Routine 继续按原时区和规则触发，下一次执行同时显示其时区

#### Scenario: DST creates missing or repeated local time
- **WHEN** 计划时刻因夏令时切换不存在或出现两次
- **THEN** 不存在时记录跳过；重复时只对应一个已定义的逻辑 slot
- **AND** 重启、倒退时钟或重复 tick 不创建第二次运行

### Requirement: Missed triggers are bounded and explicitly authorized
Routine SHALL 显示本机 runtime 停止时不执行的事实，并保存 `RUN_ONCE` 或 `SKIP` 的 missed policy 及有限 `max_catchup_age`。默认 `RUN_ONCE` SHALL 只为期限内最近的遗漏 slot 合并一次执行；更旧区间 SHALL 标记跳过，SHALL NOT 无界逐次追赶。主动暂停、授权失效、旧备份恢复及新 revision 启用 SHALL NOT 被当作普通停机补跑。

#### Scenario: A desktop returns after three offline days
- **WHEN** 本机 runtime 恢复并发现多次遗漏触发
- **THEN** 系统仅按已授权 policy 和 age 上限处理最近 slot，展示跳过/合并范围
- **AND** 不启动云端执行者、不无界枚举积压，也不自动恢复已失效授权
