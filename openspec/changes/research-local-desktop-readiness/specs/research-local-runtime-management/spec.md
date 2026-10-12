## ADDED Requirements

### Requirement: Explicit loopback deployment profile
The system SHALL 使用显式 `local-desktop` profile、单一配置解析和固定工作目录运行本机应用，默认 frontend `127.0.0.1:3000`、API `127.0.0.1:8000`。启动 SHALL 验证配置优先级、origin、端口、解析后路径和秘密边界，不静默改绑公网或选择不同 origin。

#### Scenario: Start with the default desktop configuration
- **WHEN** 合法本机配置完成并启动
- **THEN** 前后端仅监听指定 loopback 地址，浏览器通过统一同源入口访问

#### Scenario: Conflicting or non-loopback settings
- **WHEN** 配置包含冲突 origin、非法路径或非 loopback bind
- **THEN** 工具以非零退出和脱敏 reason code 终止，且不启动部分开放实例

### Requirement: Reproducible production release identity
The system SHALL 固定项目虚拟环境与前端 lockfile，生成带源码/依赖/配置身份和资源清单的 production release，并只在完整构建后激活。正常 start SHALL NOT 静默安装依赖、下载模型或复用未匹配的旧 `.next`；运行 SHALL 不依赖可变开发工作树。

#### Scenario: Activate a verified build
- **WHEN** 同一候选的 production build 与依赖身份校验通过
- **THEN** 工具原子激活其 release，状态显示真实 build/source identity

#### Scenario: Dependency drift or incomplete release
- **WHEN** 目标 release 缺文件、checksum 错误或实际解释器/依赖与 manifest 不符
- **THEN** 启动拒绝该 release，并保留原可用版本与数据

### Requirement: Bounded capability diagnosis
The system SHALL 提供 `doctor`，区分 configured/reachable/verified，检查实际解释器、依赖、配置、端口、存储和所选能力。默认检查 SHALL 不调用外部 provider；显式网络探测 SHALL 有超时、次数与费用预算。缺可选能力 SHALL 标识影响，缺必需能力 SHALL 阻止相应可用声明。

#### Scenario: Offline diagnosis finds unavailable Docker
- **WHEN** 所选 parser 依赖 Docker 而引擎不可达
- **THEN** doctor 指出受影响的解析能力和修复动作，不将 Docker CLI 存在视为可运行

#### Scenario: Explicit live probe exceeds its budget
- **WHEN** 用户选择网络探测且触及超时/请求/费用上限
- **THEN** 探测有界终止、记录实际结果与限制，不重复无限请求或声称验证通过

### Requirement: Owned and idempotent process lifecycle
The system SHALL 提供 Windows `start/stop/restart/status`，使用单实例锁、原子操作记录和 PID/创建时间/可执行身份管理自有进程及 child/container。后台辅助进程 SHALL 隐藏窗口并输出可查日志；不得按进程名或端口误杀无关进程。

#### Scenario: Start an already running owned instance
- **WHEN** 同一配置实例已经健康运行而再次执行 start
- **THEN** 返回该实例与入口，不产生第二套服务

#### Scenario: Port belongs to an unrelated process
- **WHEN** 所需端口被其他进程占用或旧 PID 已复用
- **THEN** 报告冲突/过期记录并保持无关进程不变，不直接 kill 或静默换端口

### Requirement: Real readiness and bounded startup recovery
The system SHALL 通过实际进程、API、BFF 和所需能力检查区分 STOPPED/STARTING/READY/DEGRADED/STOPPING/FAILED。readiness SHALL 有截止时间；失败启动 SHALL 只清理本次拥有的资源。任何自动重启 SHALL 有预算/退避和稳定失败态。

#### Scenario: A process listens but the required API is unhealthy
- **WHEN** 端口已监听但 API 或必要 BFF 检查失败
- **THEN** 系统不报告 READY，并在截止时间内给出实际失败/降级信息

#### Scenario: One child fails during startup
- **WHEN** 本次启动的一个必需服务异常退出
- **THEN** 工具记录失败并收敛本次拥有的资源，不影响此前存在的无关服务

### Requirement: Controlled shutdown preserves run semantics
The system SHALL 在 stop/restart 时停止新的 admission 并让既有有界操作按原 owner 生命周期收敛。无法在截止时间内安全停止时 SHALL 返回 busy/明确限制；停止等待 SHALL NOT 伪造业务取消、完成或删除 durable 记录。

#### Scenario: Stop while analysis is active
- **WHEN** 正在分析时收到停止命令
- **THEN** 工具使用现有生命周期等待/处理，保留 durable request/run/effect 记录
- **AND** 未确认终止的任务不会被标为成功或已取消

### Requirement: Redacted bounded operational logs
The system SHALL 提供 status/doctor 的 JSON 与稳定退出码，以及有界读取和轮换的日志。日志/诊断 SHALL 排除 credentials、cookies、Authorization、原始 `.env`、论文/模型全文及不必要的用户输入；业务内容的 durable owner 不得被 launcher 复制。

#### Scenario: Authentication or provider request fails
- **WHEN** 启动或使用中发生认证/provider 错误
- **THEN** 日志显示稳定原因、时间与 request/run 引用，不暴露 token 或上游响应正文

### Requirement: Explicit desktop entry and optional logon startup
The system SHALL 提供中文帮助、`open` 和桌面入口；可选的当前用户登录启动 SHALL 默认关闭，仅由明确 enable/disable 操作变更。工具 SHALL NOT 在安装或测试中擅自重启电脑、注册全局服务或使用管理员权限。

#### Scenario: Open a stopped instance
- **WHEN** 用户执行 open 而实例未运行
- **THEN** 返回清楚的启动方法，不打开错误端口或伪造已启动状态

#### Scenario: Enable and then disable logon startup
- **WHEN** 用户明确启用或关闭可选自启
- **THEN** 只变更该实例拥有的当前用户启动项，动作幂等且可查询
