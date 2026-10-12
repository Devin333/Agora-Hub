## ADDED Requirements

### Requirement: Authorized bounded single paper submissions
The system SHALL 将选定单篇论文、来源修订、问题/selection 和允许的 options 经认证 application service 提交到现有 Research 路径。服务端 SHALL 固定请求语义和 owner，拒绝客户端修改预算上限、quality gate、工具授权、实验 rollout 或 publication policy。

#### Scenario: Submit a selected paper analysis
- **WHEN** 用户提交有有效来源的论文分析请求
- **THEN** 服务端绑定可信身份、来源修订与受限参数并进入现有 Research/Harness 实现
- **AND** 前端保留可恢复的 request/run 引用

#### Scenario: Client attempts to weaken verification
- **WHEN** 请求附带关闭 gate、提升工具权限或开启未准入实验的参数
- **THEN** 服务端在执行前拒绝不允许的选项

### Requirement: Durable admission and duplicate protection
The system SHALL 在既有 application/persistence 边界为付费或长耗时运行建立 owner-scoped 请求身份和内容 fingerprint。首次网络发送前 SHALL 生成并按 owner 持久化 request 标识、请求语义及当前实例/恢复代际，服务端 SHALL 原子绑定 admission 并允许按此标识查询，run/import 身份可以稍后返回。此合同 SHALL 覆盖分析、问答及有副作用的导入。相同请求的重复提交 SHALL 查询或复用同一执行，标识与内容冲突 SHALL 拒绝；客户端 disabled 状态 SHALL NOT 是唯一去重保证。新增合同 SHALL NOT 引入第二套业务 scheduler/run store。

#### Scenario: Concurrent duplicate submission
- **WHEN** 两个并发请求带有同一 owner、请求标识与内容
- **THEN** 只有一次业务执行获得 admission，其他请求返回同一执行或结果身份

#### Scenario: Same identity with different content
- **WHEN** 相同请求标识被用于不同论文、修订或问题
- **THEN** 服务端返回明确冲突，不复用错误结果或执行第二次请求

#### Scenario: First acknowledgement is completely lost
- **WHEN** 分析、问答或导入已被服务端接收，但浏览器连第一次 request/run 回执都未收到便刷新
- **THEN** 系统用首次发送前保存的 owner-scoped request 标识核对 admission 和结果
- **AND** 不因缺少服务端返回的 run/import id 就新建执行

### Requirement: Truthful run state and uncertain outcome recovery
The system SHALL 从既有 durable 状态、事件或服务结果投影进度；没有真实阶段时 SHALL 只显示等待。刷新、断连、进程重启或响应丢失后 SHALL 先核对既有请求/运行身份，不盲目重试。浏览器 abort SHALL NOT 被描述为后端已取消。

#### Scenario: Response is lost after execution
- **WHEN** 后端完成运行但浏览器未收到响应
- **THEN** 用户重新打开时查询已提交身份并恢复已有结果，不自动启动第二次付费执行

#### Scenario: Outcome cannot yet be confirmed
- **WHEN** 后端没有足够证据确定任务是否终止
- **THEN** 页面显示待核对或中断状态及允许的下一步，不伪造成功/取消

#### Scenario: Stop waiting without server cancellation
- **WHEN** 用户停止浏览器等待而后端未确认取消
- **THEN** 页面明确等待已停止、后端结果仍需查询

### Requirement: Existing Harness authority is preserved
The system SHALL 保留 source collection → evidence → analysis → quality gate → artifacts/storage 的已有受控路径。Harness SHALL 持有 bounded PLAN → EXECUTE → VERIFY、replan/retry/budget、durable transition、工具权限和 publication；UI/LLM SHALL NOT 替代这些决策或写入 active skills。

#### Scenario: Verification rejects candidate content
- **WHEN** 既有 deterministic gate 拒绝候选结果
- **THEN** Harness 执行既定 retry/replan/halt，前端仅显示允许的诊断与恢复动作
- **AND** 候选内容不会因为 UI 点击保存或导出而成为已接受产物

### Requirement: Real single paper analysis and grounded question answering
The system SHALL 提供调用现有真实分析与论文问答路径的前端入口，显示问题、证据定位和适用条件。RAG 或其他可选路径 SHALL 仅在其真实依赖可用时开放；缺少必需 provider SHALL 标明不可用而不是返回 fake 内容。

#### Scenario: Analyze and ask about a real paper
- **WHEN** 所需配置已验证且用户提交真实论文分析及相关问题
- **THEN** 返回来自实际 runtime/provider 的结果和可追溯 evidence/run/artifact 引用

#### Scenario: Required model capability is absent
- **WHEN** 必需 LLM/embedding 能力未配置或不可达
- **THEN** 对应操作不可成功提交或返回明确 typed failure，并保留用户输入
- **AND** 该实例不能据此标记真实分析验收完成

### Requirement: Research report projections retain source and quality identity
The system SHALL 经 application projection 从既有 Research result/artifact 构造报告，保留 owner、run、source revision、quality 和 citation 身份。用户草稿、多材料整理、候选/失败结果与接受报告 SHALL 可区分；本变更 SHALL NOT 自动生成新的跨论文自主综述或报告 truth store。

#### Scenario: View an accepted analysis report
- **WHEN** 当前 owner 打开完整性和质量均符合既有合同的 Research 结果
- **THEN** 界面展示原分析、适用条件、质量结果及能回到相应来源定位的引用

#### Scenario: Open a multi-material draft
- **WHEN** 用户打开含多篇论文的准备草稿
- **THEN** 页面显示草稿和各材料真实来源，不将其标为已由模型完成并核验的综述

#### Scenario: Source or artifact no longer validates
- **WHEN** 引用版本、artifact checksum 或授权校验失败
- **THEN** 系统给出明确不可用/损坏状态，不静默替换来源或展示为已验证报告

### Requirement: Owner scoped report saving and export
The system SHALL 将报告保存为 owner-scoped 的结果引用或用户草稿，并通过相同授权后的 application projection 导出 Markdown/JSON。接受报告的导出 SHALL 保留证据、条件、质量和修订；草稿 SHALL 标注未验证；失败候选 SHALL NOT 使用接受报告导出路径。

#### Scenario: Export and reopen an accepted result
- **WHEN** 用户导出自己的有效报告后重新打开保存项
- **THEN** 输出内容与源 run/artifact 一致并保留来源/质量身份，保存项仍指向同一结果

#### Scenario: Unauthorized or rejected export
- **WHEN** 用户请求其他 owner 的报告或以 accepted 类型导出 gate-rejected 内容
- **THEN** 系统拒绝该导出，不返回私有正文或伪造通过状态

### Requirement: Safe content rendering and bounded diagnostics
The system SHALL 按既有安全渲染规则处理论文/模型 Markdown、外部链接和文件名；错误 SHALL 使用稳定原因和 request/run 引用，不能直接暴露内部路径、token、完整 provider 响应或执行脚本。

#### Scenario: Untrusted paper content includes active markup
- **WHEN** 论文或模型结果包含脚本、危险 URL 或 HTML 指令
- **THEN** UI 与导出预览不会执行其内容，证据链接仍经过允许的 scheme/path 校验
