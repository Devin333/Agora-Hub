## ADDED Requirements

### Requirement: Safe stage-specific failure diagnostics

回答与 judge 失败 MUST 保留故障阶段、原错误类型、脱敏限长的消息和可取得的 provider/HTTP 状态、retryable 信息及失败响应摘要。缺失信息 MUST 明确为未知；业务拒答和证据不足 MUST NOT 被改记为系统异常。诊断 MUST 经现有 typed-error、Harness transcript/decision 和评测工件传播，MUST NOT 暴露凭据或通过外层通用标签伪造根因。

#### Scenario: Transient gateway error
- **WHEN** provider 返回可识别的临时服务错误
- **THEN** case 和报告 MUST 保留该状态及原错误类型，区别于已知认证或模型配置错误
- **AND** retryable 字段 MUST NOT 自行授予无限重试或改变 Harness 路由权限

#### Scenario: Sensitive or oversized diagnostic
- **WHEN** 错误消息或失败输出包含凭据样例或超过保存上限
- **THEN** 持久化与显示内容 MUST 先脱敏、限长并标记截断
- **AND** 工件 MUST NOT 包含 Authorization header、API key 或未经筛选的 provider request

### Requirement: Per-case progress preserves machine-readable results

application 编排 MUST 提供每题开始与完成进度，含 case ID、序号、计划题数、阶段及结果状态。CLI MUST 将进度写入 stderr 并及时输出，stdout MUST 保持机器可读汇总。进度启用与否 MUST NOT 改变样本顺序、provider 调用或指标。

#### Scenario: Mixed successful and failed cases
- **WHEN** 一次运行包含成功题与失败题
- **THEN** 每道已完成题 MUST 有对应完成事件和实际状态，且 stdout 汇总 MUST 可被 JSON parser 读取
- **AND** 进度输出 MUST NOT 将失败题伪装成未处理题或替代持久化 case 工件

### Requirement: Recovery validates complete measurement identity

恢复或合并 MUST 先验证必填身份存在、类型与版本有效，再比较协议、dataset/mapping/cache 和影响测量的检索、模型、prompt、上下文及预算身份。两边同时缺少字段 MUST NOT 视为验证通过。旧工件适配 MUST 仅从已校验来源重建身份，MUST NOT 用当前默认值补齐未知历史配置。不同代码快照 MUST 分别记录；语义配置变化 MUST NOT 冒充原配置故障恢复。

#### Scenario: Identity missing on both sides
- **WHEN** 源与恢复工件均缺少某个必填身份，且无法按声明的版本适配验证
- **THEN** 合并 MUST 失败并报告该字段，MUST NOT 发布成功的混合指标

#### Scenario: Generation configuration changed
- **WHEN** 恢复结果使用不同的回答 prompt、上下文运行时、模型、检索策略或预算
- **THEN** 原配置恢复合并 MUST 被拒绝，该结果 MUST 作为独立运行比较

### Requirement: Answer recovery only replaces explicit system failures

回答恢复 MUST 限于源运行中目标系统失败的显式 case 子集。源和恢复工件的 case ID MUST 唯一；未知题、多余题、重复题和正常答案覆盖 MUST 被拒绝。正常回答、业务拒答、证据不足和只有 judge 失败的题 MUST NOT 通过回答恢复被重答替换。选择资格 MUST NOT 依赖答案正确性。

#### Scenario: Duplicate or successful source case
- **WHEN** 恢复工件包含重复 case ID，或请求替换源运行中正常完成的题
- **THEN** service MUST 在发布合并结果之前失败，MUST NOT 静默覆盖字典项或保留较好答案

#### Scenario: Recovery fails again
- **WHEN** 合格题的新增尝试仍然系统失败
- **THEN** 工件 MUST 保留实际失败和尝试来源，MUST NOT 将其记为成功或从计划分母删除

### Requirement: Recovery outputs are immutable complete descendants

恢复 MUST 输出到新的目录，MUST NOT 覆盖父工件。输出 MUST 包含完整 report、answers 和 sample，保留原样本数量及顺序，并保存父 report/answers/sample 哈希、各次代码身份及逐题来源。派生指标 MUST 从最终逐题记录重算；源与新增尝试的费用/耗时 MUST 可追溯，MUST NOT 将选中结果的用量冒充所有尝试总花费。

#### Scenario: Partial recovery produces a complete report
- **WHEN** 仅恢复原 300 题中的一部分合格失败题
- **THEN** 输出 MUST 仍按原顺序包含 300 题，未选题 MUST 保持原记录内容
- **AND** 父工件哈希 MUST 保持不变，恢复题 MUST 能追溯到指定父记录和新增尝试

### Requirement: Judge recovery freezes the answer execution

judge 恢复 MUST 默认只选择实际已回答且 judge 失败的题。只有显式启用 `include_missing_judge` 时，才 MAY 补判没有任何 verdict 的已回答题，并 MUST 记录该选择，区别于调用失败。service MUST 只调用 judge，MUST NOT 重新生成回答、检索上下文或请求 embedding。新格式恢复 MUST 使用相同 judge model/protocol/prompt/schema 身份，MUST 保留原 answer、context、citations、Answer-F1、coverage 和业务 outcome，仅更新 judge 观测及其派生统计/新增尝试信息。

#### Scenario: Judge-only recovery
- **WHEN** 20 道合格题被选中进行 judge 恢复
- **THEN** service MUST 仅对这 20 题调用 judge，回答模型、检索与 embedding 调用数 MUST 为零
- **AND** 未选题及所有冻结回答字段 MUST 保持不变

#### Scenario: Changed judge identity or ineligible case
- **WHEN** judge 身份改变，或选择包含正常 judge、拒答、证据不足或模型系统失败
- **THEN** 恢复 MUST 被拒绝，MUST NOT 将不同口径判定混入原报告

#### Scenario: Judge retry also fails
- **WHEN** 新 judge 请求返回非法结果或调用失败
- **THEN** 该题 MUST 继续记为 judge error 并保存诊断，MUST NOT 作为错误答案或成功判定计入正确率

### Requirement: Judge requests are reproducible and source-bound

新回答工件 MUST 保存完整可重建的 judge 请求及身份/digest，包含问题、参考答案、原回答和按原引用顺序渲染的证据。旧工件重建 MUST 验证 dataset/mapping/base embedding cache 身份，沿用原 valid-marker 索引及 `render_answer_context` 规则；若历史工件记录 corpus/index 哈希，也 MUST 校验。历史缺少该字段时，MUST 分别记录当前重建哈希和历史不可验证字段，MUST NOT 将当前哈希伪装成历史证明。缺失 chunk、非法引用或已记录身份不符时 MUST 阻断，MUST NOT 用空证据、当前默认配置或新检索结果替代。

#### Scenario: Legacy answer reconstruction
- **WHEN** 旧工件没有请求快照，但保留的答案、引用和 context IDs 可在已验证原资产中重建
- **THEN** service MUST 使用相同引用顺序和渲染规则生成请求，并保存来源和请求 digest

#### Scenario: Missing cited chunk
- **WHEN** 原引用对应 chunk 在已验证资产中不存在
- **THEN** 该恢复 MUST 在 judge 调用前失败并说明缺失来源，MUST NOT 静默省略引用

#### Scenario: Legacy judge configuration is incomplete
- **WHEN** 历史工件缺少 judge model 身份
- **THEN** service MUST 要求操作者显式声明历史模型，核对当前模型，并记录 model-only 比较及其余历史配置未知
- **AND** service MUST NOT 从当前默认值自动补齐历史 judge 身份，也 MUST NOT 宣称已验证完整历史配置

### Requirement: Recovery reports distinguish evidence and quality claims

报告 MUST 区分原运行、回答失败恢复、judge 恢复和共同有效题配对比较，逐项显示分母、失败与成本归属。未校准 judge 的限制 MUST 保留；恢复正确性或 context coverage 提升 MUST NOT 被解释为已证实的答案语义质量提升。

#### Scenario: Small paired accuracy difference
- **WHEN** 恢复后共同有效题的正确数出现变化，但人工 judge 校准仍未完成
- **THEN** 报告 MUST 保留校准限制和配对分母，MUST NOT 仅凭该变化宣布显著改进或生产资格
