## ADDED Requirements

### Requirement: Complete owned storage inventory
The system SHALL 建立实际解析的 storage inventory，记录 auth、workspace/history、笔记/选区、imports、Research run/artifacts 及必要 Harness events/checkpoints/index 的 owner、路径、schema、一致性方法、敏感性与引用关系。路径可配置时 SHALL 不假定所有数据均在单一默认目录。

#### Scenario: A storage owner uses a custom path
- **WHEN** workspace 或 Research artifact 被配置到不同数据根
- **THEN** backup 清单覆盖真实路径或明确报告无法覆盖而失败，不静默漏备份

### Requirement: Consistent bounded backup snapshots
The system SHALL 在受控停止写入/服务并验证一致性后创建有界备份。SQLite SHALL 使用一致状态或原生备份而非遗漏 WAL 的文件复制；未知 writer、锁冲突或不受控存储 SHALL 导致明确失败。备份 SHALL NOT 修改源数据。

#### Scenario: Back up a quiescent local instance
- **WHEN** 所有包含项的写入已收敛且快照成功
- **THEN** 生成完整 manifest 和内容 checksum，可关联实际 build/schema 与 owner

#### Scenario: Another writer or disk exhaustion is detected
- **WHEN** 无法建立一致性或复制过程中磁盘不足
- **THEN** 备份失败且不标为可恢复，原数据保持可用，已有有效备份不被覆盖

### Requirement: Sensitive backup handling and explicit exclusions
The system SHALL 为含认证/私有资料的备份保留本机访问控制，明确包含/排除项与所需外置配置。默认 SHALL 不复制原始 `.env`、provider secrets、任意内容日志、依赖、缓存或模型。恢复 SHALL 使旧会话失效并要求重新登录。既有 admission owner SHALL 创建新恢复代际或等价 fencing，拒绝旧实例/旧标签页请求自动执行；恢复的非终态 intent SHALL 保持待核对。备份后已发生的外部调用可能缺 receipt，not-found SHALL NOT 被当作重试许可；新执行须由用户明确发起并使用新身份。

#### Scenario: Restore requires an external credential
- **WHEN** 备份未包含实际 provider 密钥
- **THEN** manifest 明确标注需重新提供该配置，工具不将该备份描述为可独立完成所有网络业务

#### Scenario: Session token exists before restore
- **WHEN** 备份被成功恢复
- **THEN** 恢复前或备份中的会话不能继续授权，用户使用恢复的账号重新登录

#### Scenario: Old request is retried after restoring an earlier snapshot
- **WHEN** 备份后付费执行已完成，而恢复的快照缺少该 receipt，旧标签页重提请求
- **THEN** 新恢复代际拒绝自动 admission 并说明需要核对，不因查无记录再次付费执行
- **AND** 备份中的非终态 intent 不自动重放

### Requirement: Verified staged restoration and safe activation
The system SHALL 在隔离 staging root 验证备份的路径、资源限额、checksum、schema、owner 和引用后再激活；拒绝越界、绝对路径、symlink/junction 逃逸及未知格式。Inventory SHALL 定义 owner 到恢复根的映射，无法通过原 owner 安全重定位的持久化引用 SHALL 拒绝；校验 application 配置 SHALL 只指向恢复根和明确列出的外置依赖。多存储切换 SHALL 有可恢复 activation 记录与预恢复快照，失败不得暴露混合代际数据。

#### Scenario: Restore valid data to an isolated destination
- **WHEN** 合法备份通过全部校验
- **THEN** 可在隔离目录读取真实 workspace、论文、笔记及报告并生成恢复证据
- **AND** 校验进程不能读取原业务数据根，仍可通过 application service 读取恢复数据；激活前原实例数据不被覆盖

#### Scenario: Malicious path or corrupt artifact
- **WHEN** 备份包含越界链接、checksum 错误、未知 schema 或缺失必需引用
- **THEN** 恢复拒绝且不写入受控 staging root 以外的路径

#### Scenario: Activation fails after partial switching
- **WHEN** 多 owner 数据切换中途失败
- **THEN** 恢复流程按记录回到原一致状态，服务在一致性恢复前不能继续接收业务

### Requirement: Compatible release rollback
The system SHALL 在恢复旧 release 前验证其代码、构建、依赖、配置与数据 schema 兼容性。兼容时 SHALL 保留已提交数据；不兼容时 SHALL 拒绝原地降级，只有显式选择配套有效备份后才能按恢复合同切换。不得自动破坏性逆迁移或更改开发工作树。

#### Scenario: Previous release can read current data
- **WHEN** 目标 release 资源和兼容性验证通过
- **THEN** 工具切换到该已验证版本并用既有数据检查健康和读取能力

#### Scenario: Previous release is incompatible or incomplete
- **WHEN** 旧版本缺依赖资源或不能读取当前 schema
- **THEN** rollback 拒绝，保留当前数据与版本，说明需要的兼容备份或资源
