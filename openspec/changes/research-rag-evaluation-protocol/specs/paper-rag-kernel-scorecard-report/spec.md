## MODIFIED Requirements

### Requirement: Research reports expose generic RAG scorecards

Research evidence regression reports SHALL expose a generic `RAGEvaluationReport` payload in addition to existing Paper-specific report fields. 正式协议 SHALL 仅把有效测量的数值转换为 `MetricValue`，并在 Research-owned metadata/逐例记录中保留所有缺失、错误、未决评分及其分母，不向 framework 引入 Paper 业务模型。

#### Scenario: Report dictionary includes a scorecard
- **WHEN** `EvidenceRegressionReport.to_dict()` is called
- **THEN** the payload includes `rag_evaluation_report`
- **AND** the nested scorecard contains `MetricValue` entries for retrieval, answer, and generation metrics when those result types are present and the measurements are valid
- **AND** 正式协议的不可用指标保留显式 status/reason 而不是转换为虚构数值

### Requirement: Paper-specific report compatibility is preserved

scorecard 集成 SHALL 保留现有 Research report 字段及其旧协议语义。本变更明确保留未选择正式协议调用的旧阈值计算；显式正式协议 SHALL 增加版本化完整性、方向化阈值和可比性决策，不把旧诊断 `passed` 直接提升为正式资格。

#### Scenario: Existing report consumers read Paper metrics
- **WHEN** the regression report is serialized
- **THEN** existing `retrieval`, `answer`, `generation`, `ab`, `thresholds`, `passed`, and `issues` fields remain available
- **AND** 未选择正式协议的调用保留既有 threshold issue calculation
- **AND** 正式调用另外给出 protocol/status/decision，且只有必需测量与门禁明确通过时 `passed` 才为 true

## ADDED Requirements

### Requirement: Formal reports preserve experiment identity and completeness

正式报告 SHALL 包含 dataset/case-set/corpus/index/config/code/judge/metric 版本、track/profile、实际 component provenance、gold 使用标志、执行状态和全部样本计数。正式资格 SHALL 使用 `pass/fail/inconclusive/not_applicable`，与执行成功、命令成功或生成了报告文件分开。

#### Scenario: A report contains unscored required answers
- **WHEN** 所有回答已生成但部分必需 judge 结果为 error 或 needs_review
- **THEN** 报告保留全部计划样本和原因，并标记正式决策 inconclusive
- **AND** `passed` 为 false，不用缺失分数的默认值宣布通过

### Requirement: Formal thresholds declare direction and applicability

正式阈值 SHALL 显式声明 metric/version、适用集合、`min/max/delta_min/delta_max` 比较方向、最低样本及有效覆盖规则。没有配置正式阈值或必需指标缺失时 MUST NOT 因为空 issues 集合而得到正式 pass。

#### Scenario: Lower error rate passes an upper bound
- **WHEN** 实验使用 `max` 约束错误率或费用且测量完整
- **THEN** gate 根据上限方向判断，而不复用所有指标均为下限的旧算法

#### Scenario: No formal acceptance policy is supplied
- **WHEN** 实验只提供了观测配置而未提供已批准的正式验收阈值
- **THEN** 系统仍可生成测量报告
- **AND** 正式决策为 inconclusive，不授权发布

### Requirement: Comparison reports retain paired evidence and uncertainty

正式比较 SHALL 按同一 case 配对，先验证配置可比性，再报告整体/题型/模态切片、各自样本量、重复次数、差值、不确定性和逐题退化。重复输出不能计作新增独立问题，oracle 不能进入生产方案排名；配置不可比或证据量不足时 SHALL 明确标记而非选择赢家。

#### Scenario: Two reports use different unapproved answer models
- **WHEN** baseline 与 candidate 的差异包含未预声明的生成模型变更
- **THEN** 报告可并列展示独立成绩但比较状态为 incomparable
- **AND** 不计算可用于推广的胜负结论

### Requirement: Cost and latency reports expose measurement scope

正式报告 SHALL 分列 index、request、judge 成本，区分 provider 实测与带版本价格表的估算；缺失 usage/cost 保持未知。P50/P95 SHALL 标明样本量、计时范围、冷热缓存、并发及超时/失败数量，重试费用和预算中止不得丢失。

#### Scenario: Failed attempts consumed provider tokens
- **WHEN** 某题重试后成功，先前失败尝试已产生供应商 usage
- **THEN** 请求账单包含全部已知尝试消耗并保留未知部分
- **AND** 不只统计最后一次成功的费用或只对幸存成功请求宣称整体时延达标
