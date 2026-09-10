## ADDED Requirements

### Requirement: First-read extraction shares available evidence
首次处理论文证据时，系统 SHALL 支持在同一次阅读/分析中记录命题、已知条件、来源与核验范围，并复用本轮已有的同版本证据。系统 SHALL NOT 默认因每条新句子创建额外回读和独立 verifier 调用；额外核验 SHALL 由可审计策略触发。

#### Scenario: Multiple claims share one table
- **WHEN** 本轮已有完整相关表格、表注和实验说明，多条结论由其产生
- **THEN** 抽取与范围核对 SHALL 共享该证据上下文
- **AND** SHALL NOT 为每条结论重复获取同一片段

### Requirement: Rechecks are triggered by use and evidence changes
Harness SHALL 按确定性政策评估回查触发：证据/依赖失效、条件删改、用途扩大或新推断、引用异常、冲突、当前要采用但范围未核验的结论，以及最终关键结论覆盖不足。worker 的疑点和检索建议 SHALL 仅为候选，不能自行授予工具权限或预算。

#### Scenario: Unknown omitted qualifier
- **WHEN** 工作记录只有“A 比 B 好”及合法引用，没有完成指标与适用范围核验，且当前回答准备采用该比较
- **THEN** 系统 SHALL 读取对应表格、表注与必要实验说明执行针对性范围核对
- **AND** 无法确认 SHALL 保留未决，不因结构检查通过就接受普遍结论

#### Scenario: New use exceeds verified scope
- **WHEN** 有效记录仅支持小样本结果，而本次要回答所有样本规模下的表现
- **THEN** 系统 SHALL 将扩大范围视为新的核验需要
- **AND** SHALL NOT 仅因原结论文字或引用未变而命中可用核验

### Requirement: Recheck reuse is identity and scope bound
抽取与核验结果复用 SHALL 绑定命题、适用范围、证据/依赖版本、policy/verifier revision 和 actor scope，并检查当前权限与有效性。缓存命中 SHALL NOT 被解释成独立交叉验证；首次缺失条件仍 SHALL 被视为残余风险。

#### Scenario: Unchanged verified comparison
- **WHEN** 已核验的比较记录及其条件、用途、来源与依赖均未变化
- **THEN** 系统 SHALL 复用核验结果，无需额外语义 LLM 调用

#### Scenario: Rephrasing has no established semantic identity
- **WHEN** 模型自由改写了论断，但无法证明命题和范围身份保持一致
- **THEN** 系统 SHALL 视其为新候选或待核验变化
- **AND** SHALL NOT 仅凭“表达优化”标签自动沿用旧核验

### Requirement: Targeted reads and batch checks are bounded
回查 SHALL 优先使用已载入/缓存证据和精确引用，再按需要扩展到表头、表注、实验设置或相关章节。可共享的疑点 SHALL 按来源与实验范围分组，在 item/token 上限内批量核验，每条命题单独获得结果。无新证据的相同问题 SHALL NOT 因窗口变化重复查询。

#### Scenario: Batch has one unsupported claim
- **WHEN** 同一批次中四条命题有支持，一条证据不足
- **THEN** 五条命题 SHALL 分别记录核验状态
- **AND** SHALL NOT 以整个批次通过覆盖未支持命题

#### Scenario: No new evidence after exhausted investigation
- **WHEN** 同一来源、范围、依赖和触发的问题已完成有界调查，且没有新材料
- **THEN** 系统 SHALL 复用未决结果和已查范围
- **AND** SHALL NOT 每轮再次发起相同补证

### Requirement: Conflict resolution distinguishes scope and absence
系统 SHALL 按来源血缘、版本、条件与支持关系裁决冲突，保留不可裁决的双方证据。未找到支持 SHALL 表达为已查范围内证据不足，不等价于命题已被证伪；新抓取时间或重复转述数量 SHALL NOT 自动决定真值。

#### Scenario: Three summaries share one source
- **WHEN** 多个 agent 的支持结论都来自同一原始片段
- **THEN** 系统 SHALL 按血缘去重，保留它们的候选记录
- **AND** SHALL NOT 把它们计作多份独立证据来压过反证

#### Scenario: Search found no large-sample experiments
- **WHEN** 在固定版本的正文与附录范围内没有找到大样本结果
- **THEN** 输出 SHALL 限定为本次核查范围内证据不足
- **AND** SHALL NOT 自动推断方法在大样本下无效

### Requirement: Verification has finite shared budgets
策略 SHALL 定义有限的核验调用数、输入/输出 token、原文读取数及 bytes、单问题尝试数和批量 item/token 上限，并受总 Harness 预算约束。摘要、回查、抽查、verifier 与子任务成本 SHALL 计入完整成本账本；调用前预留，调用后按 usage 结算，未决调用 SHALL 保留 reservation。费用未知 SHALL 报告 unknown；配置了货币上限却无法进行保守估价时 SHALL 拒绝相关调用，不以 unknown 绕过准入。

#### Scenario: Verification budget is exhausted
- **WHEN** 剩余预算不足以完成所需核验
- **THEN** 系统 SHALL 保留未决项，收窄答案或报告类型化未完成原因
- **AND** SHALL NOT 将待核验结论升级为已确认，或以最终 gate 名义绕过预算

#### Scenario: Timeout without complete usage
- **WHEN** 核验调用超时且实际 usage 未返回
- **THEN** 系统 SHALL 保留未决预算预留并进行既有对账流程
- **AND** SHALL NOT 将费用记零或通过新窗口重新授予额度

### Requirement: Final checks cover material conclusions without mandatory duplicate calls
最终回答 gate SHALL 对主要结论、数值/排名比较、因果与泛化主张执行覆盖检查；有效范围核验 SHALL 复用，缺少覆盖的关键部分 SHALL 按预算集中核验。无法确认的结果 SHALL 降级或披露，程序 gate SHALL NOT 宣称保证全部自然语言含义正确。

#### Scenario: Generator marks a central ranking as low impact
- **WHEN** 生成者试图把最终答案中的主要方法排名标记为低影响以跳过检查
- **THEN** Harness SHALL 依据输出角色和业务规则保持关键结论覆盖要求

#### Scenario: All material claims already have applicable checks
- **WHEN** 最终答案中的核心论断已有符合当前用途的有效核验记录
- **THEN** final coverage gate SHALL 复用这些记录
- **AND** SHALL NOT 默认再逐条调用模型或重读全文

### Requirement: Evaluation reports cost and semantic limits honestly
上线评估 SHALL 固定来源、任务、模型与工具设置，对比当前 compaction 基线及本方案；统计全部调用、失败、缓存、token、费用、时延和回答质量。结构不变量与语义指标 SHALL 分开报告；上线 SHALL 满足冻结任务集的质量/完成率不下降和配对完成任务至少一项总 token/费用降低、另一项不恶化的门槛。

#### Scenario: Lower tokens caused by early refusal
- **WHEN** 新策略因为提前退出而显著减少 token
- **THEN** 评估 SHALL 同时报告任务完成率、答案范围和未决比例
- **AND** SHALL NOT 将不可比任务范围造成的减少作为达成效率上线门槛的证据

#### Scenario: Functional checks pass but semantic evaluation fails
- **WHEN** 条件字段、引用和 replay 测试通过，但标注关键案例出现新增条件遗漏或无依据泛化
- **THEN** 策略 SHALL 保持 shadow/disabled，记录失败案例并修复后再评估
- **AND** SHALL NOT 将严格 OpenSpec 校验或结构测试当作语义安全证明
