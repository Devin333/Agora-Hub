## ADDED Requirements

### Requirement: Context is projected from committed state
启用本策略的下一轮 LLM 请求 SHALL 从已提交研究状态与当前任务组装上下文，并绑定 state/event version、论断 revision、证据 manifest、policy/model/tokenizer revision 与 prepared request fingerprint。派生视图 SHALL NOT 自行变更正式状态。

#### Scenario: Resume in a new model window
- **WHEN** Harness 选择创建新的模型上下文窗口
- **THEN** 新请求 SHALL 包含当前任务需要的目标、有效结论、已知条件、核验范围、未决问题和必要证据
- **AND** SHALL NOT 依赖完整旧对话才能识别当前有效版本

### Requirement: Known qualifiers survive physical materialization
系统 SHALL 将继续使用的命题、已知适用条件、核验范围与不确定性作为关联语义单元保护，并检查最终 provider 输入中的实际保留情况。需要进行新证据判断时，系统 SHALL 提供必要原文片段；裸引用 SHALL NOT 被视为模型已经读取证据。

#### Scenario: State is correct but prompt loses a qualifier
- **WHEN** 正式状态保存“小样本设置”，最终请求只留下“A 比 B 好”
- **THEN** provider dispatch SHALL 在调用前被拒绝或重新组装
- **AND** SHALL NOT 仅因数据库字段齐全就放行

#### Scenario: Protected content exceeds the window
- **WHEN** 预算内无法同时保留当前任务必需的结论、条件和证据
- **THEN** 系统 SHALL 缩小任务范围、分步处理或返回类型化超限结果
- **AND** SHALL NOT 通过静默删除条件获得预算通过

### Requirement: Archive precedes removal from the active window
在启用路径裁剪或替换活动窗口内容前，系统 SHALL 持久化可解析、带顺序/身份/checksum 的原始历史 manifest，覆盖被移除的已准入消息、工具输入/返回和必要源材料。源文档、视觉表格与相关说明 SHALL 按本任务所需的形式可恢复；历史正文缺失 SHALL 被明确记录。

#### Scenario: Archive write fails
- **WHEN** 原文归档或 manifest 持久化失败
- **THEN** 系统 SHALL 停止该次裁剪/切换激活
- **AND** SHALL NOT 静默转入会丢失唯一原始副本的旧压缩路径

#### Scenario: Legacy summary lacks original messages
- **WHEN** 旧会话仅有摘要标记，无法恢复被压缩原文
- **THEN** 系统 SHALL 将其视为未验证历史并披露缺口
- **AND** SHALL NOT 补造原始内容或从该摘要直接授予新核验状态

#### Scenario: Retention or access changes
- **WHEN** 原始证据因保留策略删除或当前 actor 失去访问权
- **THEN** 回读 SHALL 遵守当前权限并报告不可用
- **AND** 相关缓存/核验记录 SHALL 失效，系统 SHALL NOT 声称原文仍可恢复

### Requirement: Window switching preserves execution continuity
窗口切换 SHALL 仅在需要移交的工具事务已完成且本步骤的结果、状态接受和必要记录已持久化的安全点执行；run/conversation identity、权限、预算、重试和 replan 计数、操作幂等身份 SHALL 连续。未决外部副作用 SHALL 使用既有 reconciliation，不能被窗口切换自动重试。

#### Scenario: Tool outcome is indeterminate
- **WHEN** 工具写入超时且结果尚未确认
- **THEN** 系统 SHALL 保留未决状态并先对账、等待或受控停止
- **AND** SHALL NOT 通过创建新窗口把该操作当成从未执行

#### Scenario: Repeated rollover
- **WHEN** 同一 run 多次达到软窗口阈值
- **THEN** 每次切换 SHALL 沿用已消耗预算和重试次数
- **AND** SHALL NOT 因窗口编号改变重新授予额度

### Requirement: Window reconstruction reuses bounded context controls
窗口触发 SHALL 使用物理请求预算和输出预留，并复用既有受控 compaction、tool transaction integrity 与 dispatch 验证。系统 SHALL 支持已归档大结果引用替换、相关证据选择及经既有 gate 验证的可选摘要；摘要 SHALL NOT 覆盖正式事实或自行授权调用。

#### Scenario: Existing record is valid across a switch
- **WHEN** 命题、条件、证据、依赖和本次用途均未变化且已有有效核验记录
- **THEN** 窗口切换 SHALL 复用该记录
- **AND** SHALL NOT 仅因切换发生而新增逐条语义核验或全量原文回读

### Requirement: Rollover is replayable and deployment scoped
系统 SHALL 持久化窗口选择、投影验证、激活、回读和预算决策；纯 replay SHALL 只使用历史记录与已提供 artifacts，不调用 live LLM、工具、来源服务或外部写入。新策略 SHALL 对新 run 显式启用并固定 revision，缺失必需端口 SHALL 阻止启用。

#### Scenario: Replay with forced switch positions
- **WHEN** 使用固定候选及工具记录，在不同安全点模拟切换并执行纯 replay
- **THEN** 最终接受状态、条件、证据身份和预算 SHALL 与对应记录一致
- **AND** live worker/source/tool/publication 调用次数 SHALL 为零

#### Scenario: Roll back policy selection
- **WHEN** 灰度结果不满足上线门槛而回滚
- **THEN** 新 run SHALL 使用回滚后的策略选择
- **AND** 已有 run SHALL 继续固定原策略或停在可恢复 checkpoint，历史及未决操作 SHALL 被保留
