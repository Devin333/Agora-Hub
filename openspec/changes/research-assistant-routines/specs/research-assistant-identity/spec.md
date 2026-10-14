## ADDED Requirements

### Requirement: Persistent research assistant identity
系统 SHALL 持久化 owner/workspace 范围内的 `AssistantProfile`，包含稳定 `assistant_id`、职责和 policy revision。首期每个 owner/workspace SHALL 至多有一个活动 Research Assistant；重新打开应用 SHALL 恢复同一身份而非重新创建助手。

#### Scenario: Restart preserves assistant identity
- **WHEN** 同一 owner 在相同 workspace 重新打开应用
- **THEN** 返回原助手身份、已关联 Routine 与历史运行
- **AND** 重启本身不创建 Run、不启用草稿或恢复已暂停任务

### Requirement: Assistant identity reuses existing resource authorities
助手 SHALL 通过稳定引用关联已有 workspace、Conversation、Research Run、论文资料和个人记忆，SHALL NOT 复制第二套事实源。Conversation SHALL 只是交互入口；长期助手身份 SHALL NOT 要求保持无界 LLM 循环或以聊天文本替代 Harness 状态。

#### Scenario: Conversation claims completion before a run finishes
- **WHEN** 模型回复声称任务完成，但关联 Run 尚未通过 deterministic VERIFY 或 controller-owned publication
- **THEN** 工作台继续显示实际 Run 状态，不生成成功收件箱项
- **AND** 对话重开或文字编辑不改变执行状态

### Requirement: Owner-scoped assistant access
所有助手、Routine、Run、结果和记忆访问 SHALL 在 application service 验证可信 owner 与 workspace 权限。后台 delegation SHALL 由可信后端根据精确授权和当前代际建立，SHALL NOT 使用客户端自报身份、前端 cookie 或模型生成的 grant 作为权限来源。

#### Scenario: Another owner submits an assistant identifier
- **WHEN** 一个 owner 读取、编辑或运行其他 owner 的助手或 Routine
- **THEN** 请求被拒绝，不能通过 Run、artifact 或 memory 关联泄露受保护内容

#### Scenario: Explicit logout invalidates background delegation
- **WHEN** 用户主动退出登录、切换 owner 或激活隔离恢复的数据代际
- **THEN** 旧 delegation 失效，旧执行者不能开始新的外部调用或发布
- **AND** 再次登录不能自动恢复调度，用户必须明确恢复并重新通过必要的授权检查

### Requirement: Routine memory context obeys existing user controls
Routine SHALL 默认不使用或外发个人记忆；用户可以明确授权已确认记忆的读取范围与远程处理策略。实际上下文 SHALL 记录使用的 memory ID/revision，并在每次构建时排除未确认、停用、删除或超出 owner/scope 的记录。Routine SHALL NOT 因授权了读取而获得记忆写入权限。

#### Scenario: A memory record is disabled after scheduling
- **WHEN** Routine 已入队但用户在模型调用前停用其曾引用的个人记忆
- **THEN** 后续上下文不包含该记忆，记录的实际使用集合与发送内容一致
- **AND** 已发往 provider 的历史请求限制被如实说明，不宣称能够远程撤回

#### Scenario: Remote memory processing is not authorized
- **WHEN** Routine 允许读取本机记忆但不允许向所选远程 provider 发送其内容
- **THEN** source、模型和 embedding 请求均不包含该个人记忆或未经许可的派生摘要
- **AND** 无合格本机替代时显示能力限制或等待用户决定，不静默切换远程处理

### Requirement: Candidate content cannot change persistent authority
自然语言、论文正文、工具结果和模型候选 SHALL 作为不可信输入。系统 SHALL 只允许确定性应用服务依据真实用户确认更新助手策略、授权或活动个人记忆；反馈事实与 memory candidate SHALL 同活动个人记忆分离。普通 Routine SHALL NOT 修改 active skill、决定 promotion 或绕过 skill evolution gate。

#### Scenario: A paper attempts to redirect private data
- **WHEN** 论文内容要求上传笔记、增加工具权限或自动记住一项偏好
- **THEN** 内容仅作为研究材料，不产生新授权、外发目标、活动记忆或 skill 修改

#### Scenario: User marks a recommendation irrelevant
- **WHEN** 用户保存“不相关”反馈但未确认个人记忆候选
- **THEN** 反馈可保存为可追溯业务事实，活动个人记忆不变
- **AND** 后续召回不得把未确认候选当作已授权偏好
