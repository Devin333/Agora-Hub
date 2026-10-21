## ADDED Requirements

### Requirement: Parsing evaluation separates backend production and downstream tracks

文档解析评测 SHALL 显式标识 `backend_raw`、`production_document` 以及 `downstream_projection` 完整性检查，并记录真实执行身份。raw 轨 SHALL 只证明原始 backend 内容，production 轨 SHALL 只评分实际产品文档规范内容，downstream 检查 SHALL 消费真实 chunker/store payload。项目自定义完整性指标不得冒称官方 OmniDocBench 分数。

#### Scenario: Product normalization omits a backend formula
- **WHEN** raw backend 有某公式，而实际 ResearchDocument 或其合法规范 artifact 没有该公式
- **THEN** raw 与 production 轨 SHALL 分别反映各自内容，production 导出不得从 raw 或 gold 补入公式掩盖漏失

#### Scenario: Old document cannot express reliable reading order
- **WHEN** 当前生产基线缺少足以忠实导出页/block 顺序的信息
- **THEN** 相关生产指标 SHALL 标为 unavailable 并保留覆盖缺口，在优化前冻结替代绝对门槛/保真要求或阻塞该维验收，不能虚构可比的 B1 delta

### Requirement: Baseline identity and input mapping are reproducible

正式运行 manifest SHALL 固定 dataset/annotation/source hashes、evaluator commit/config、parser image/weights/config、代码 commit 及 dirty 内容指纹、schema/normalizer/exporter/chunker、track、预算与运行环境。输入 SHALL 区分 raster、native PDF 和真实多页文档；batch mapping SHALL 可逆且保留原始 source 边界。正式 production parse SHALL 以单一来源文档为单位；来源文档未知的 benchmark 页作为独立单页 source，不将多个无关页面拼成同一业务文档；单页 benchmark 不等于完整论文 ingestion gate 通过。不可恢复的历史字段 SHALL 标 unknown，不伪造完整 provenance。

#### Scenario: Two runs use different parser images or matcher settings
- **WHEN** 候选与基线的不可变镜像、匹配配置或数据版本不同
- **THEN** 比较器 SHALL 标出差异并按预注册可比性规则拒绝直接 A/B delta 或以明确不同实验报告，不把它当作单一代码改动收益

#### Scenario: Historical result used a mutable latest tag
- **WHEN** 历史运行只有 `latest` 标签且没有可靠 image identity
- **THEN** 报告 SHALL 将该运行标为历史诊断及 provenance 不完整，不能用当前镜像 digest 反填旧运行

### Requirement: Scoring data is isolated from production parsing

gold、人工修正答案、标注 ID、评分缓存和 held-out 信息 SHALL 仅由获准 offline scorer/数据制作流程使用，不挂载到生产 parser/recovery，不参与在线策略。parser SHALL 不按 benchmark 样本 ID 特判；production 执行不得因 manifest 自报 production 就绕过真实 composition/execution receipt 校验。

#### Scenario: Production track runs with an offline scorer
- **WHEN** 同一评测计划包含生产 parser 和需要 gold 的 scorer
- **THEN** parser SHALL 仅接收文档及必要合法配置/source identity，scorer 在独立权限边界读取 gold，结果能验证二者输入隔离

#### Scenario: Direct subprocess diagnostic uses the real model
- **WHEN** benchmark 注入直接 subprocess runner 调用真实 Marker 但缺少 production execution identity/receipts
- **THEN** 运行 SHALL 标为 diagnostic，而不是仅因模型真实就标为正式 production 证据

### Requirement: Every requested page and metric has explicit outcome accounting

评测 SHALL 为每个 requested page 保留终态及原因，并分别记录 parser completion、matching、metric status 与各自分母。missing output、unmatched、timeout、exception、invalid、skipped、not_applicable SHALL 不被默默删除或视为通过；对有 gold 的识别缺失 SHALL 遵循固定官方 scorer 的处理并额外显式报告覆盖缺口。

#### Scenario: Some requested pages fail before prediction export
- **WHEN** 一次 215 页计划中有页面解析失败或没有 prediction
- **THEN** 报告 SHALL 保留全部 215 页计划及失败页身份、失败率/原因，official metrics 的实际分母和 completeness gate SHALL 独立显示，不能只汇总成功页声称完整通过

#### Scenario: A page contains no gold table annotation
- **WHEN** 某页没有待评分表格类别且不是执行或 scorer 故障
- **THEN** table metric SHALL 标为 not_applicable，而不是失败、虚构满分或混入 table 分母

#### Scenario: Metric evaluator crashes
- **WHEN** CDM/TEDS 或 matcher 对某样本超时或异常
- **THEN** 系统 SHALL 保存 metric error/timeout 和样本身份，并阻塞需要该指标的完整性验收，不能将错误静默当作有效分数

### Requirement: Official metrics retain aggregation units and comparable support sets

官方指标 SHALL 使用冻结版本的 Edit_dist、CDM、TEDS 等定义和 matching 规则；报告 SHALL 同时声明 page/object/sample 聚合单位、有效集合、分母、未匹配和评分异常。A/B SHALL 仅在预注册规则判定可比时给出方向化 delta，并报告全请求集合的失败变化；不同 track/聚合单位 SHALL 不混作改进幅度。

#### Scenario: Formula page average differs from sample average
- **WHEN** 同一运行 CDM 的 page mean 与 sample mean 不同
- **THEN** 报告 SHALL 分别命名两者及分母，不能把 sample mean 与另一运行的 page mean 相减或解释为公式语义正确率

#### Scenario: Candidate improves scores only after losing difficult pages
- **WHEN** 候选比基线丢失更多 difficult pages 而成功子集平均分更高
- **THEN** 比较 SHALL 显示有效集合及全请求失败变化，完整性 gate SHALL 不允许把该子集提升作为整体验收通过证据

### Requirement: Development exposure and document heldout are enforced

已被查看或用于调参的页面 SHALL 标为 exposed dev/regression。正式 held-out SHALL 按原始文档、版本族和近重复组隔离，并在候选优化前冻结来源、标注、使用规则、最小样本量和阈值；无法确定来源组的页面不得获得无泄漏文档级验收身份。对 held-out 结果调参后 SHALL 不继续称其为未见保留集。

#### Scenario: Existing 215 public pages are randomly resplit
- **WHEN** 已暴露的 215 页被重新随机划出一部分作为 test
- **THEN** 数据身份 SHALL 仍是 exposed regression，不能仅通过重命名用于 unseen document-heldout 验收

#### Scenario: A preprint and published version share content
- **WHEN** 文档族的不同版本或近重复页被发现分布在 dev 与拟议 held-out
- **THEN** 数据制作 SHALL 在冻结前按来源族归组并消除泄漏；已冻结版本出错则须修订版本并重新评估，不静默删题提高成绩

### Requirement: Evaluation produces actionable errors cost and preregistered gates

报告 SHALL 关联低分/随机复核样本的原页、raw/normalized/prediction、匹配结果与错误类别，并保存各阶段延迟、恢复次数、资源环境和可得成本。candidate gate SHALL 使用首个算法变更前冻结的指标门槛、适用范围、预算与不确定性规则；不足样本或缺 production/held-out 证据 SHALL 返回 blocked/fail，而非自动通过。

#### Scenario: Root cause is a normalization loss rather than backend OCR
- **WHEN** 原页和 raw 内容正确但 normalized/table/chunk 内容缺失
- **THEN** 错误证据 SHALL 识别实际失效阶段并关联对应 artifacts，不能只输出一个总体 OCR 分数

#### Scenario: Resource overhead exceeds the frozen allowance
- **WHEN** 候选质量达到目标但同机同缓存条件下延迟或恢复资源超过 acceptance profile
- **THEN** quality 与 resource checks SHALL 分别报告，整体验收不得忽略已冻结资源限制而通过

#### Scenario: Required sample size is not available
- **WHEN** 一个必要 slice 或文档保留集未达到预注册的最低有效样本量/置信规则
- **THEN** 该检查 SHALL 返回 blocked 或明确受限支持范围，不能以缺样本视为无回归
