## ADDED Requirements

### Requirement: Paper watch uses real scoped Research sources
生产 `paper_watch` SHALL 通过 Research application/ports 使用真实允许的数据源，遵守用户确认的主题、时间窗、过滤条件、最大论文数、调用预算与保存范围。系统 SHALL 记录材料实际可用的分析深度，SHALL NOT 使用样本论文、伪造正文或将仅有 metadata/abstract 的分析标为全文阅读完成。

#### Scenario: Only an abstract is available
- **WHEN** 真实来源只返回论文摘要且全文获取失败
- **THEN** 结果如实标识摘要级分析和来源限制，不生成假正文或宣称已验证全文实验细节

### Requirement: Canonical paper identity prevents duplicate business records
论文发现 SHALL 复用 Research 已有 arXiv、DOI、规范 URL 等身份规则并与目标 workspace catalog 和历史交付记录比对；标题相似 SHALL NOT 作为唯一身份依据。重复发现 SHALL 不产生重复论文条目、报告交付或相同推荐提醒；论文版本信息 SHALL 保留，不将同一工作的修订伪装为全新论文。

#### Scenario: Two sources and a later run find the same paper
- **WHEN** 同一论文通过 DOI 与 arXiv 来源出现，且后续 Routine 再次发现它
- **THEN** 数据归并到同一可核验论文身份，库中引用和已交付推荐不重复
- **AND** 不同来源证据及版本信息仍可检查

### Requirement: Source coverage and success watermarks are honest
系统 SHALL 为每个来源记录请求窗口、成功处理游标、截断/分页状态和失败原因。来源失败、解析失败、预算中断或未完成处理 SHALL NOT 被当作完整成功推进水位。恢复抓取 SHALL 受授权 lookback、条数及调用预算限制，覆盖缺口 SHALL 明示而非无界回补或宣称没有论文。

#### Scenario: One required source times out
- **WHEN** 一个要求的来源超时而其他来源成功
- **THEN** 失败来源不推进成功水位，结果显示 `partial_failure` 与遗漏范围
- **AND** 下次获准运行仍可在有限范围内重试遗漏数据

#### Scenario: Offline gap exceeds the authorized lookback
- **WHEN** 用户离线时间超过授权允许的回查窗口
- **THEN** 系统仅处理获准窗口并展示未覆盖区间，不自动扩大抓取或费用范围

### Requirement: Recommendation candidates pass deterministic Research gates
模型 SHALL 只生成候选相关性、推荐理由和报告。Research deterministic gates SHALL 检查结构、可解析来源/引用、证据支持范围、声明的分析深度、适用阈值及失败状态。未知或不可用 gate SHALL 在入场前阻断；gate 失败 SHALL 进入有界 retry/replan/halt，SHALL NOT 以模型自评替代质量门或承诺超出实际检查的语义正确性。

#### Scenario: A candidate recommends a paper using fabricated evidence
- **WHEN** 候选理由引用不存在的论文、片段或未获取的全文实验
- **THEN** 对应候选不通过 gate，不能进入成功报告或推荐收件箱
- **AND** 修复重试受同一 Run 的预算和最大轮数约束

### Requirement: Publication owns delivery and catalog effects
只有通过全部适用 VERIFY 的结果 SHALL 由既有 controller-owned publication authority 幂等发布。报告、资料关联和“已交付”标记 SHALL 使用稳定身份与可验证 receipt 收敛，SHALL NOT 因发现候选或任务出队就提前标为已交付。授权内的本地保存 SHALL NOT 隐含公开发布、删除已有笔记或未经授权的全文下载。

#### Scenario: Process crashes after artifact publication but before acknowledgment
- **WHEN** 产物已发布而 occurrence、catalog 关联或交付确认尚未完全更新时进程退出
- **THEN** 恢复依据 publication receipt 幂等补齐关联，不重复发布、重复推荐或从头再分析
- **AND** 没有成功 receipt 的候选不能因旧“已发现”记录而被永久跳过

### Requirement: Business outcomes distinguish no change from incomplete work
结果投影 SHALL 区分 `new_results`、`no_change`、`partial_failure`、`failed`、`needs_action` 并保留 Harness 的取消/未知状态。只有所有要求来源在声明窗口内完成、必要筛选/分析/VERIFY 正常完成且无新增可交付结果时 SHALL 标为 `no_change`。正常业务筛选淘汰 SHALL 与执行或验证失败分开记录。部分结果 SHALL 只展示已通过 gate 的部分，并明确未完成范围；模型失败、gate 重试耗尽、等待审批、预算中断、解析失败或未决调用 SHALL NOT 被掩盖为无更新。

#### Scenario: All sources fail
- **WHEN** 所有要求来源都超时或不可用
- **THEN** 结果为 `failed` 并提供原因，不报告“没有新论文”

#### Scenario: Sources succeed but required analysis or verification fails
- **WHEN** 来源收集成功，但模型调用失败或候选 VERIFY 在预算内无法修复
- **THEN** 结果保留 `failed`、`partial_failure` 或 `needs_action` 及原因，而不是因无合格候选而标为 `no_change`
- **AND** 相应重要故障或行动需求不会被无更新静默策略隐藏

#### Scenario: All sources complete without qualifying new papers
- **WHEN** 声明窗口内的来源检查、去重与候选验证均完成且无新合格论文
- **THEN** 保存可追溯的 `no_change` 运行记录和覆盖范围，不新增普通结果提醒
