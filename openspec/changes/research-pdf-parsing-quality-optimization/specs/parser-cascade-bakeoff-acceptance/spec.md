## ADDED Requirements

### Requirement: Parser bakeoff exposes document parsing protocol identity

既有 parser bake-off JSON/Markdown 报告在包含正式文档解析评测时 SHALL 增加带版本的协议身份、manifest 引用、track、execution profile、完整性/可比性和 page/object/sample 分母。历史或未执行协议结果 SHALL 显式标 not_evaluated，不能推断为生产解析质量通过；现有 penalized metrics 与 cascade acceptance 字段 SHALL 不被替换。

#### Scenario: Report includes raw and production document results
- **WHEN** 一个 parser 运行产生 `backend_raw` 和 `production_document` 结果及下游完整性检查
- **THEN** JSON 与 Markdown SHALL 按轨展示结果、协议指纹、分母、失败/不可评原因和 artifact refs，不能合并成无法辨认来源的单一分数

#### Scenario: Older bakeoff artifacts do not declare the protocol
- **WHEN** 报告读取没有新文档解析协议的旧 artifacts
- **THEN** 旧 report 行为 SHALL 保留可识别诊断结果，新 document parsing evaluation SHALL 为 not_evaluated，不从旧 ready 标志推导通过

### Requirement: Document parsing acceptance is independent and fail closed

报告 SHALL 使用独立 `document_parsing_acceptance` 记录 pass/fail/blocked、frozen acceptance profile、实际/目标值和 reason；pass SHALL 同时要求协议可比、分母完整、必要生产与独立文档 held-out 证据、质量/保真/资源门槛达标。既有 `cascade_acceptance` 或 composite score SHALL 不单独授权新解析 profile 晋级，报告不得自行发布配置/模型/skill。

#### Scenario: Legacy cascade passes but normalized evidence is missing
- **WHEN** 旧 cascade thresholds 全通过，但必要的 production document 或 held-out 证据缺失
- **THEN** cascade checks SHALL 保留原结果，`document_parsing_acceptance` SHALL 为 blocked 并指出缺项，不能声明新 profile production-ready

#### Scenario: All preregistered checks are satisfied
- **WHEN** 同一冻结协议下必要 production、保真、质量、完整性、held-out 和资源 checks 均满足预注册阈值
- **THEN** 新验收 SHALL 为 pass 并关联全部证据，但仍只作为既有人工/确定性发布流程的输入，不直接启用新 profile

#### Scenario: Candidate fails a measured requirement
- **WHEN** 可比且完整的候选结果未满足一个冻结质量或资源门槛
- **THEN** 新验收 SHALL 为 fail，列出 actual/threshold/reason，不把失败降级成没有影响的 warning
