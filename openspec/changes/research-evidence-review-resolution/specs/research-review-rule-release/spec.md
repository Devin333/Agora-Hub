## ADDED Requirements

### Requirement: Review Rule Challenges Require Reproduction

系统 SHALL 将检查器实现缺陷、证据抽取遗漏和产品标准变更分别记录。规则异议 MUST 绑定原材料、claim、规则版本、原结果、争点和可复现反例；普通 Research run MUST NOT 修改自身 active rule、gate 或验收条件。

#### Scenario: A checker ignores an applicable footnote already in evidence
- **WHEN** 现有证据含适用基线而规则仅查表格单元格导致误拦
- **THEN** 系统建立可复现规则缺陷案卷，独立生成候选修复
- **AND** 当前业务 run 不得以人工豁免或热替换规则获得通过

### Requirement: Rule Candidates Pass Independent Held Out Evaluation

每个候选规则包 SHALL 版本化绑定 checker、类型要求、评审 prompt、阈值及适用范围。promotion MUST 要求候选验证、开发正反例、独立留出集旧新成对评估和不同于候选作者的有权维护者审核。候选作者/运行环境 MUST NOT 读取留出答案；定量质量与成本通过线 MUST 在候选留出结果揭晓前登记。

#### Scenario: A fix passes its motivating example but admits invalid claims
- **WHEN** 新规则减少当前误拦却在负例或留出集产生不可接受漏放
- **THEN** 系统拒绝 promotion，不能删除反例、削弱断言或事后改变门槛使其通过

#### Scenario: A candidate is evaluated against uncertain human labels
- **WHEN** 独立标注者对参考语义仍无可核查共识
- **THEN** 样本标为争议并单独统计，不能强制一个人的答案成为唯一 gold 或只报与其一致率

#### Scenario: A proposed repair changes an active skill
- **WHEN** 规则修复实际需要变更 active skill package
- **THEN** 业务经验须先写入 memory 并 consolidate 为 procedural strategy，再进入 Harness 控制的 skill validation、held-out eval、promotion、versioned release 与 rollback
- **AND** 普通 Research run 和规则申诉接口不得直接修改 active skill

### Requirement: Rule Release Creates New Evaluation History

系统 SHALL 发布不可变规则版本和可回滚的准入配置。当前 run MUST 保持原绑定，新版本核验 MUST 建立关联重评；历史 replay MUST 不读取最新规则或覆盖旧判定。权限、来源真实性和证据绑定 MUST NOT 被人工豁免关闭。

#### Scenario: A corrected checker becomes available
- **WHEN** 候选完成要求验证并由独立维护者发布
- **THEN** 受影响任务通过新 run 绑定新版本重评，旧失败与原因保留
- **AND** 新版本不能直接把原失败记录改为成功

### Requirement: Defective Rule Versions Are Quarantined With Impact Tracking

确认失效规则 SHALL 以规则版本和适用 claim 类型隔离新发布能力，并将当前无法有效检查的结果标为 VERIFICATION_UNAVAILABLE。系统 MUST 跟踪已发布产物的依赖关系和待复核状态，传播至阅读器、卡片、检索与其他消费入口；传播未完成时 MUST 查询当前隔离状态或阻止无条件可信消费。

#### Scenario: A rule defect is found after publication
- **WHEN** 已发布结果依赖被确认失效的规则版本
- **THEN** 原产物保留历史，当前查询显示待复核且不能作为无条件可信 claim 继续使用
- **AND** 修订结果只能经新评审与新版本发布

#### Scenario: A rule release is rolled back
- **WHEN** 发布方恢复到先前经验证的准入版本
- **THEN** 回滚只影响后续可选规则，不清除争议、隔离记录或旧失败，也不自动恢复原产物的可信状态
