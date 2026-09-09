## ADDED Requirements

### Requirement: Immutable Claim Review Binding

系统 SHALL 将每次评审绑定到不可变的 claim revision 与 checksum、断言种类及范围、研究目标 revision、tenant/actor scope、run、论文快照、证据包 revision/checksum 和规则/策略版本。系统 MUST 区分 `reported_result`、`evidence_interpretation` 与 `independent_validation`，并按类型冻结必要要求和不适用条件。

#### Scenario: A narrowed claim needs a new review
- **WHEN** “方法整体更好”改为特定数据集和设置中的数值陈述
- **THEN** 系统产生新的 claim revision 并重新核验，原意见不能直接授予新句子发布资格
- **AND** 原任务的泛化判断目标仍保持未解决，除非用户明确修改目标版本

#### Scenario: An old observation uses another paper or evidence version
- **WHEN** 意见绑定的论文、实验、证据修订或规则不同于当前对象
- **THEN** 它不能参与当前 resolution，只能作为明确标记的历史意见或待重新核查线索

#### Scenario: An assertion requires independent validation
- **WHEN** 任务要求独立实验验证而只有论文作者的陈述
- **THEN** 该要求保持未满足，系统不能将作者陈述标为独立验证

### Requirement: Separate Check Execution From Claim Meaning

系统 SHALL 分别记录检查执行状态、案卷生命周期、结论状态和关闭原因。检查工具、权限或输入失败 MUST NOT 形成语义反证；预算耗尽 MUST NOT 改写已知的证据关系。

#### Scenario: A referenced table exists but semantics are unreviewed
- **WHEN** 引用 ID 检查通过而要求的实验范围复核尚未完成
- **THEN** 引用检查可以记录 PASS，claim 仍为 PENDING，不能解释为语义通过

#### Scenario: A source reader or checker is unavailable
- **WHEN** 原文加载、OCR 或适用检查器无法完成有效检查
- **THEN** 系统记录 UNAVAILABLE 或 INPUT_INVALID 及具体原因，claim 不得被判为 CONTRADICTED
- **AND** 无法在有界恢复中解决时形成 VERIFICATION_UNAVAILABLE

#### Scenario: Review ends with conflicting or missing evidence
- **WHEN** 原文解释冲突未解决或必要材料仍缺失且额度已耗尽
- **THEN** 系统分别保留 DISPUTED 或 INSUFFICIENT_EVIDENCE，并另记 budget_exhausted，不把两者混合或改成反驳

### Requirement: Challenges Are Source Bound Observations

系统 SHALL 接受有权限评审者提出的具体异议，要求对象/检查项、理由和可核对位置、反例、缺口或工具错误。系统 MUST 验证作用域、完整性和实质新依据；身份权威、同意票数或自报置信度 MUST NOT 建立质量通过。评审者 MUST NOT 直接写最终 verdict 或发布状态。

#### Scenario: A human points to an unrelated experiment
- **WHEN** 人工提交的脚注属于另一组实验或不同论文版本
- **THEN** 系统保存意见与不适用理由，不能让其覆盖当前证据绑定或自动解除拦截

#### Scenario: Reviewers agree on a forged citation
- **WHEN** 人工和模型都支持结论但引文无法定位或必要条件未覆盖
- **THEN** 一致意见不能覆盖真实性或要求项检查失败

#### Scenario: A materially new challenge is admitted
- **WHEN** 新依据经接纳确认会影响当前支持范围
- **THEN** 系统使旧支持记录失去当前发布资格，并按持久化顺序进入复核

### Requirement: Evidence Repair Preserves Source Meaning

系统 SHALL 区分原文缺失与证据投影遗漏，以有界步骤核对当前快照的上下文、表题、脚注和已授权附录。引文规范化 MUST 保留原文映射、数字、单位及否定含义；候选抽取字段完整 MUST NOT 代替语义适用性核查。超出现有来源授权或范围的材料 MUST 使用新绑定与明确范围说明。

#### Scenario: A baseline is present in the PDF footnote
- **WHEN** 人工给出可定位脚注，而当前 Evidence Pack 没有该内容
- **THEN** 系统先核对真实原文，分类材料处理遗漏，补齐后产生新 evidence revision 重评
- **AND** 人工描述本身不能代替原文，旧失败保留

#### Scenario: Percentage points are confused with relative change
- **WHEN** 同一条件下 85% 与 80% 被描述成相对提高 5%
- **THEN** 检查指出差异为 5 个百分点、相对增加为 6.25%，并要求修订错误候选

#### Scenario: A calculation lacks a usable denominator or unit
- **WHEN** 分母为零、单位不明或比较条件尚未确认
- **THEN** 系统记录具体未解决项，不能编造相对比例或把不同条件当作可比

### Requirement: Independent Review Does Not Confer Authority

系统 SHALL 对启用策略下需要语义判断的实验比较和外推默认要求独立人工复核，机器评审只能辅助；纯引文与明确可计算陈述 SHALL 按版本化类型合同处理。首次独立意见提交前，服务端 MUST 隐藏已有意见、通过标签和模型置信度。不同提示词或同一评审者重复提交 MUST NOT 被计为已证明的独立性。

#### Scenario: An independent reviewer first opens a case
- **WHEN** 评审者尚未提交自己的意见
- **THEN** API 和页面只提供对象、标准及同一范围材料，不提供先前结论或置信度
- **AND** 提交后才允许比较已授权可见的分歧理由

#### Scenario: Majority support leaves an applicable counterargument unresolved
- **WHEN** 多数评审支持但仍有适用于同一断言的实质反证或解释冲突
- **THEN** 系统要求针对争点的解决依据，未解决时保持 DISPUTED，不能按票数放行

### Requirement: Deterministic Resolution Uses a Frozen Review Contract

Research resolver SHALL 以冻结的要求、适用观察、依据与未解决项产生 resolution 候选；确定性 gate MUST 重算接纳条件，Harness MUST 决定是否接受并记录状态。VERIFY MUST NOT 调用模型，也 MUST NOT 将人工或模型输出的 SUPPORTED 字符串直接视为通过。

#### Scenario: An observation says supported but a required review is missing
- **WHEN** 观察给出支持意见而必要评审、适用检查或要求覆盖尚未完成
- **THEN** resolver 不得生成可接受的 SUPPORTED_WITHIN_SCOPE，最终 gate 仍不通过

#### Scenario: The recorded scope is supported
- **WHEN** 当前绑定下适用检查和复核均已完成、必要条件覆盖且无相关未解决冲突
- **THEN** 系统可以记录 SUPPORTED_WITHIN_SCOPE，并展示有限材料和范围
- **AND** 该状态不代表论文科学结论被证明，也不单独授予整份报告发布资格

### Requirement: Dispute Handling Is Bounded and Durable

系统 SHALL 持久记录补材料、额外独立复核和重开额度，默认上限分别为 2、1、2，并同时遵守更严格的 Harness run 预算。人工等待 SHALL 不超过 24 小时与剩余 deadline 中较小值并释放活动资源。重复理由、重启或措辞变化 MUST NOT 重置预算。

#### Scenario: Repeated feedback contains no new grounds
- **WHEN** 同一异议通过重复请求、改写或服务恢复再次出现
- **THEN** 系统关联原案卷并保留已用额度，不能重复触发副作用或无限重开

#### Scenario: A required human response never arrives
- **WHEN** 持久等待达到有效 deadline
- **THEN** 系统记录未完成复核及超时原因并关闭或受控终结，不能视为同意

### Requirement: Review Writes Are Idempotent and Scope Protected

所有评审写入 SHALL 经 application service 校验身份、scope、binding、expected case revision 和幂等键。相同键同内容 MUST 返回原接受结果，相同键不同内容 MUST 拒绝；并发关闭 MUST 只产生一个当前 resolution。来源与理由 MUST 被当作数据，不能成为控制指令。

#### Scenario: Two reviewers close the same case revision
- **WHEN** 并发请求尝试更新同一案卷 revision
- **THEN** 只有一个条件写可成为当前记录，另一请求须读取新状态并明确处理冲突

#### Scenario: Persistence fails around an accepted observation
- **WHEN** 意见、事件或状态投影提交期间进程中断
- **THEN** 系统依据持久身份对账，提交未成功不得推进，无重复接纳或静默丢失

#### Scenario: Feedback attempts cross tenant access or policy injection
- **WHEN** 意见引用未授权材料或要求跳过 gate、改变预算或发布
- **THEN** 系统拒绝越权并忽略文本中的控制指令，不泄露原文

### Requirement: Reader Review UI Shows Evidence and Uncertainty

系统 SHALL 提供中文结论状态、同版原文、争点、检查失败原因、意见和修订差异，并通过 application service 提交异议、证据与重评请求。UI MUST 区分内部候选/诊断和正式报告，MUST NOT 提供绕过 gate 的批准入口，也 MUST NOT 在普通阅读流程堆叠内部标识符。

#### Scenario: A reader challenges a blocked experiment claim
- **WHEN** 阅读者打开争议详情并提交具体原文位置
- **THEN** 页面展示被检查材料与未解决项，提交只创建有身份和版本的观察，不能直接改变发布状态

#### Scenario: Valid structure analysis is retained during an experiment dispute
- **WHEN** 系统保存已有支持的结构和方法材料但必要实验仍未解决
- **THEN** 授权用户可查看诊断材料，页面不得显示整份报告已完成或提供正式成果引用
