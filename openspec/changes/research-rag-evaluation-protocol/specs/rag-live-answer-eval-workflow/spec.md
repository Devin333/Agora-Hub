## ADDED Requirements

### Requirement: Production evaluation is an explicit bounded workflow mode

现有 live workflow SHALL 在保留 fixture/既有诊断步骤的基础上，支持显式 frozen experiment manifest 的生产开发集评测。启动前 MUST 核验 dataset/corpus/index readiness、实际 adapter、provider/data policy、总费用/token/request 预算和逐次超时；不能根据仅存在 API key 就自动开始任意规模付费实验。

#### Scenario: Scheduled production evaluation has a reviewed manifest
- **WHEN** 定期或手动任务选择已审核的开发集 manifest 且所有前置条件满足
- **THEN** workflow 通过统一 application/core 执行声明的模式与方案
- **AND** 保留 actual provenance、原始响应、逐例失败和费用范围

#### Scenario: A manifest omits spending limits
- **WHEN** 请求正式 live 实验但没有必要预算边界
- **THEN** preflight 在付费请求前拒绝运行并写出原因

### Requirement: Held-out evaluation requires explicit release intent

保留集运行 SHALL 显式声明 release/qualification 用途、冻结配置、审核批准引用和题库暴露史。默认定期任务 MUST NOT 以保留集作为自动调参输入；首次暴露后的标签/失败反馈用于改动时 SHALL 记录相应历史并为后续无偏验收补充未使用数据。

#### Scenario: A routine scheduled job selects held-out data
- **WHEN** 常规定期任务未声明保留集验收意图与批准却选择 held-out split
- **THEN** workflow 不启动该付费评测
- **AND** 报告所缺批准/用途，不静默改用开发集后声称保留集完成

### Requirement: Skips and incomplete stages remain visible

workflow SHALL 对缺少凭据、材料、授权数据范围、预算耗尽及评分失败写出机器可读状态和原因，并在失败/跳过时仍上传可用工件。允许旧诊断步骤按既有规则 skip，但正式必需阶段缺失 SHALL 形成 inconclusive 和非零退出码，不能合成 pass。

#### Scenario: Required secrets are absent in a diagnostic job
- **WHEN** 旧 fixture live 诊断任务缺少所需 secrets
- **THEN** 任务保留清晰 skipped 原因而不调用模型
- **AND** 汇总不宣称已经获得真实答案成绩

#### Scenario: Budget expires during a formal matrix run
- **WHEN** 正式多方案 run 用完预算且还有计划题目未执行
- **THEN** 停止新请求并保留全部已执行/未执行项与原生 trace
- **AND** 上传 partial artifacts，正式验收为 inconclusive 而不是按已完成子集通过

### Requirement: Workflow supports score-only replay without repeating RAG

live workflow SHALL 支持从不可变运行工件执行新的评分/审核版本并生成比较报告，保留原始运行身份与历史评分。重新评分若需要 judge SHALL 仍遵守独立评分预算与数据发送策略，不隐式重建语料或重新生成答案。

#### Scenario: Judge rubric changes after answers are captured
- **WHEN** 操作者选择相同运行工件和新的 rubric version 执行 score-only
- **THEN** workflow 仅追加评分修订并对所有比较方案采用同一 rubric
- **AND** 原始答案、检索结果、生产调用次数与旧评分保持不变
