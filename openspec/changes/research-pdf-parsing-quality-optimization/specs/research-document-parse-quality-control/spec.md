## ADDED Requirements

### Requirement: Parser quality checks are deterministic and observable

解析质量检查 SHALL 由现有 Research probe/service 的确定性规则执行，分别表示 execution completion、结构/来源合法性与可观测完整性。每条规则 SHALL 记录 identity、status、severity、actual/expected、reason 及受影响 block/page。在线检查不得依赖 benchmark gold 或 LLM 自报成功，不得声称规则通过已证明任意页面语义正确。

#### Scenario: Process succeeds with empty or malformed output
- **WHEN** backend exit code 为 0，但产物为空、页映射非法或 grid 结构冲突
- **THEN** 系统 SHALL 记录 execution success 与独立的质量 failure，不能仅因进程成功将文档判为正常 parsed

#### Scenario: Benchmark annotations are unavailable online
- **WHEN** 生产 parser 在没有任何 benchmark gold 的情况下运行
- **THEN** 相同配置与文档输入 SHALL 产生相同的确定性质量判定，不以测试标签控制路由或接受结果

### Requirement: Parse outcomes follow explicit degradation and quarantine policy

检查结果 SHALL 按版本化 policy 映射到现有 `parsed`、`degraded` 或 `quarantined` 文档状态及 evidence 使用资格。source mismatch、unsafe artifact 或不可恢复关键结构错误 SHALL 不被当作正常可信证据；软问题 SHALL 保留原内容与明确原因，交由现有输入/证据 gate 管理消费资格，不新增 LLM authority。

#### Scenario: A local issue does not invalidate the whole source
- **WHEN** 一个局部 block 顺序不确定且冻结 policy 允许保留可追溯内容
- **THEN** 文档 SHALL 带局部问题及 degradation 标记，不能把未知内容升级为已验证正确

#### Scenario: Source identity verification fails
- **WHEN** 输出 source hash 与输入不匹配或 artifact 属于其他来源
- **THEN** 系统 SHALL 拒绝把其作为该 source 的正常解析结果入库，并保存确定性原因

### Requirement: Recovery is bounded and controlled by the existing Harness

任何重解析或 ROI candidate 尝试 SHALL 复用既有 Harness/execution adapter 的 authorization、预算与 `PLAN → EXECUTE → VERIFY` 控制。policy SHALL 显式限制 max_replans、max_turns、attempts、pages/regions、时间和资源；LLM 或 worker 不得自行扩展预算、选择工作流、批准 gate 或 publication。未获批准的外部 provider SHALL 默认关闭。

#### Scenario: Region recovery exhausts its allowance
- **WHEN** 一个 ROI 已达到允许重试次数，或文档/Harness 总预算耗尽
- **THEN** 系统 SHALL 停止新尝试，记录未恢复原因并执行受控 degrade/halt，而非循环 fallback

#### Scenario: Required execution capability is missing
- **WHEN** 配置请求的恢复后端缺少获准 execution provider 或 capability
- **THEN** 系统 SHALL fail closed，不能改用直接 subprocess 或其他未授权服务继续尝试

### Requirement: Recovery candidates preserve original evidence and uncertainty

恢复候选 SHALL 与原始输出、source crop/ref、profile 和验收理由共同保留。确定性结构校验只能拒绝/标识可观测错误，不得凭“长度更长”“能渲染”或 worker 自报质量证明未知识别内容正确；不足以可靠选择时 SHALL 保留原始证据和不确定状态，不编造数据。

#### Scenario: A renderable formula candidate is the only new evidence
- **WHEN** 候选公式可渲染但缺少足以替换原式的内容依据
- **THEN** 系统 SHALL 保存该候选为未验证结果，不能将其作为内容正确的自动通过证明

#### Scenario: Cell crop cannot be mapped reliably
- **WHEN** 表格内容恢复需要 cell bbox，但现有 geometry 无法可靠定位 cell
- **THEN** 系统 SHALL 不猜测 crop、填写 cell 或声称恢复成功，并记录不可恢复条件

### Requirement: Parse attempts and transitions are durably auditable

所有 production parser/recovery 尝试及 Harness phase transitions SHALL 写入现有 durable transcript/event/artifact 链，包含 attempt identity、profile、输入/输出引用与 checksum、reason、budget 使用和 execution receipt。超时/取消/崩溃 SHALL 保留终止确认或 indeterminate 状态；恢复执行不得伪造 exactly-once 或成功回执。

#### Scenario: Recovery times out and termination is unconfirmed
- **WHEN** backend 超时且 execution adapter 无法确认子进程终止
- **THEN** attempt SHALL 标为 indeterminate 并由现有安全策略隔离/停止后续动作，不能将其计为成功或无记录地再执行

#### Scenario: Audit or replay is requested
- **WHEN** 操作者查看一次已完成的解析或恢复运行
- **THEN** 记录 SHALL 能关联原始/候选结果、采用或拒绝理由、phase transitions、执行回执与预算，且 replay 不需再次调用 live backend

### Requirement: Formula and table processing treat content as untrusted data

LaTeX rendering、HTML/table parsing 和 crop/export SHALL 服从既有文件、网络、进程与资源安全边界，禁止 shell escape、任意路径/外部命令、script/event handler 执行及无限资源消耗。文档正文 SHALL 不被当作工具授权或系统指令。识别配置 SHALL 经 typed settings allowlist，而非拼接自由 argv 或复用 Docker runtime args 作为模型参数。

#### Scenario: Formula contains unsafe file or command requests
- **WHEN** 待处理公式包含越权文件读取、外部命令或网络动作
- **THEN** 受限处理 SHALL 拒绝或安全隔离该输入，不能访问未授权数据或启动任意命令，并记录错误类别

#### Scenario: Table markup contains scripts and event handlers
- **WHEN** 原始表格 HTML 包含脚本、事件属性或恶意外链
- **THEN** 解析/展示 SHALL 保留允许的表格/数学内容但不执行有害内容，不能因过滤脚本就篡改合法 cell 数字

### Requirement: New profiles require explicit promotion and consistent rollback

新解析/恢复 profile SHALL 默认保持 opt-in，晋级 SHALL 由已有确定性 gate 和人工 owner 流程批准。parse/chunk/index snapshot SHALL 绑定同一 source 与版本身份，旧产物不得伪装成新 profile 的已验证输出。rollback SHALL 恢复完整已验证 snapshot/profile 组合并记录原因，而非只替换 parser 名称；不得因此新增通用 legacy 兼容层。

#### Scenario: Planning or development metrics are available
- **WHEN** 新 profile 只有方案、开发分数或不完整验收证据
- **THEN** 系统 SHALL 保持默认生产配置不变，不能自动启用恢复能力、发布模型/配置或修改 active skill

#### Scenario: A rollout fails after candidate artifacts are written
- **WHEN** 新 parse artifacts 已生成但 chunk/index 一致性或 rollout gate 失败
- **THEN** 系统 SHALL 保持或恢复上次已验证的完整 snapshot，保留失败和前后身份，不混用新 parse 与旧 chunks 伪装成功
