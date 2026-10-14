## ADDED Requirements

### Requirement: Routine workspace extends the existing paper product
系统 SHALL 在既有论文工作台提供助手设置、Routine 列表/确认页、执行详情和应用内结果收件箱，复用既有 owner、workspace、资料库、阅读器和 Run 服务。首期 SHALL NOT 新增通用 Agent 控制台、任意电脑操作或第二套本机运行管理器。

#### Scenario: User opens a recommendation from the inbox
- **WHEN** 当前 owner 打开已发布的论文推荐
- **THEN** 进入既有论文/报告阅读入口，并可追溯 Routine、Run、来源和实际使用的 memory 引用
- **AND** 其他 owner 或无权访问的 workspace 不能通过深链接读取结果

### Requirement: Displayed execution and operation status is authoritative
工作台 SHALL 从 application service 投影 durable Routine/Run 状态，显示计划时区、下次执行、本机 runtime 是否可用、实际运行进度和阻断原因。暂停、取消、撤销、归档和立即运行 SHALL 分别说明影响。未收到可信服务端持久化确认的操作 SHALL 显示未生效/待确认或失败，SHALL NOT 乐观宣称执行已停止。

#### Scenario: User requests revocation while the local service is unavailable
- **WHEN** UI 无法连接服务端完成撤销持久化
- **THEN** 显示撤销未确认，并提供恢复连接后的重试与实际状态查询
- **AND** 不显示已撤销、不假设在途 Run 已终止

#### Scenario: Runtime stops while the window remains open
- **WHEN** 本机 runtime 已停止或不可验证但 UI 仍显示计划列表
- **THEN** 界面显示当前不能执行，而不是仅依据下次执行时间承诺后台在线

### Requirement: Inbox alerts are meaningful and durably deduplicated
首期提醒 SHALL 仅使用应用内收件箱，并绑定 owner、Routine、稳定 outcome-key 和可核验 Run/artifact 或健康事件。新合格推荐、重要失败和需要用户行动 SHALL 生成可行动提醒；相同报告、重复投递和同一未解决错误 SHALL 不反复新增提醒。普通 `no_change` SHALL 只保留运行历史；故障恢复 SHALL 更新或关联原健康事件而非伪造新论文结果。

#### Scenario: A completed run event is delivered twice
- **WHEN** 同一发布结果事件因重试再次送达收件箱 consumer
- **THEN** 只有一条对应内容提醒，已读状态不会因重投重置

#### Scenario: Repeated checks produce no new papers and one ongoing failure
- **WHEN** 多次运行没有新内容，或连续遇到相同未解决的 provider 故障
- **THEN** 无更新的成功运行保持安静，故障保留一条更新后的可行动健康提醒
- **AND** 故障不能套用 `no_change` 静默规则而被隐藏

### Requirement: Notification privacy and authority remain scoped
内容提醒 SHALL 在创建前重新核验当前授权、owner 和 outcome 身份，默认仅展示必要摘要，不暴露个人记忆、provider 凭据或私人上下文。撤销后的既有历史及安全/控制状态 SHALL 仍可供已认证 owner 查看，但不得借此新增已失效任务的推荐或外部消息。首期 SHALL NOT 调用系统推送、邮件、IM 或 webhook。

#### Scenario: A queued recommendation arrives after revocation
- **WHEN** 报告结果已形成但新的内容提醒尚未创建时用户撤销 Routine
- **THEN** 该旧授权不能再创建推荐提醒，已存历史保持可审计
- **AND** 不发送外部消息，也不隐藏已生效的撤销状态

### Requirement: Backup and restore never reactivate autonomous work
助手、Routine、必要授权元数据、occurrence、outbox/inbox、budget 与结果关联 SHALL 纳入既有数据清单、导出和隔离恢复合同，SHALL NOT 导出可复用 credentials/delegation token。恢复 SHALL 校验 owner/checksum/schema，提升 restore/authorization epoch，使还原的 Routine 停用且要求重新确认。旧队列、lease、审批、提醒和预算快照 SHALL 不自动变成当前执行权或可用余额。

#### Scenario: User restores a backup from before revocation
- **WHEN** 隔离恢复的数据含一个后来已撤销的 active Routine 和未完成任务
- **THEN** 激活后该 Routine 仍不可自动运行，旧消息和审批因代际不符被拒绝
- **AND** 用户必须明确重新确认，既有非终态任务和历史提醒不能自动重放

#### Scenario: Restored accounting cannot prove remaining allowance
- **WHEN** 旧备份中的费用余额无法与已发生的实际消费核对
- **THEN** 即使用户重新登录或确认 Routine，系统仍阻断基于该旧余额的新收费动作，直到可信对账或用户明确批准独立追加预算；普通 Routine 重新确认不构成余额重置或追加费用授权

### Requirement: Delivery remains a default-off follow-up capability
本变更 SHALL 保持为 `paper-agent-desktop-app` 之后独立启用的论文例行任务能力，不改写其首版范围。安装、升级、迁移与创建文档 SHALL NOT 启用后台工作；发布 feature gate 与每个 Routine 用户授权 SHALL 分别满足才可运行。前置变更的未完成资格 SHALL 保留原责任，静态 Research 路径 SHALL 不以未经合格的动态 parallel 路径替代。

#### Scenario: A user upgrades from the paper desktop first release
- **WHEN** 新版本包含 Routine 代码或新增存储 schema
- **THEN** 旧资料与手动 Research 路径保持可用，后台功能默认关闭且没有自动 active Routine
- **AND** 只有受测部署 capability 齐备并经用户明确启用/确认后才允许调度

### Requirement: Disablement and rollback preserve safety and evidence
功能关闭 SHALL 停止新准入、围栏化本功能的后续副作用并受控取消活动运行，保留事件、已有产物及只读查看。回滚 SHALL 不破坏性删除用户数据或自动逆迁移；不兼容版本 SHALL 阻断写入或要求明确的隔离恢复。发布证据 SHALL 包括真实路径、重复投递、崩溃、取消、时区/DST、预算、owner 隔离、旧备份和无副作用 replay，并记录输入版本与未通过项。

#### Scenario: Feature is disabled during a run and the application restarts
- **WHEN** 操作者关闭功能后进程重启，队列中仍有消息
- **THEN** 旧消息不能恢复执行或发布，历史和产物仍可查看
- **AND** 未确认终止的远端动作保持可核验的不确定状态，不被重启清空

#### Scenario: Offline checks pass but live provider qualification is missing
- **WHEN** 单元测试与 smoke 成功，但未获授权或尚未完成真实 provider/恢复组合验收
- **THEN** 发布证据明确保留未通过项，不能标记为已具备受控 live 启用资格
