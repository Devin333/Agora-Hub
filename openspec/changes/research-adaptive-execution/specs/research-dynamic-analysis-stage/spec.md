## MODIFIED Requirements

### Requirement: Research exposes an opt-in dynamic analysis workflow variant

Research SHALL 提供版本化、checksum-bound 的 Graph definition，在声明的分析区域内使用既有固定 worker、bounded AgentLoop 或受控 `dynamic_analysis_stage`。本 requirement 的 opt-in SHALL 指完成 dynamic parity、replay、适用依赖门及自适应产品验收后的服务端 release admission，MUST NOT 要求终端用户选择执行模式。资格满足前，`build_paper_analysis_graph_definition()` SHALL 继续作为默认组合路径，已有合格行为 SHALL 保持不变。

资格满足后，新公开分析请求 SHALL 使用已接纳的自适应 profile，且 MUST NOT 接受调用者提供的 `dynamic_analysis`、`dynamic_task_plan` 或内部 Graph/policy/binding 覆盖。具体策略 SHALL 只选择预先声明的可到达分支，MUST NOT 替换 run 冻结的 Graph identity。历史 run SHALL 保留其 exact compatible recovery definitions 和 receipts；历史恢复 MUST NOT 演变为永久的用户模式兼容接口。

#### Scenario: Existing default remains before adaptive qualification

- **WHEN** 自适应 profile 尚未完成适用的依赖与产品发布门
- **THEN** 普通分析请求 MUST 保持现有合格默认组合行为
- **AND** 本变更文档或模型建议 MUST NOT 开启未验收的动态能力

#### Scenario: A qualified request automatically uses the declared analysis region

- **WHEN** 服务端在合格自适应 profile 下接纳一个新请求
- **THEN** Harness MUST 在已固定 Graph 的分析区域内选择适合的工作，不要求用户选择模式
- **AND** 被选用的 `dynamic_analysis_stage` MUST 位于已接纳证据准备与固定下游 claim verification 之间，遵守准入时的 Graph identity 和 checksum

#### Scenario: A standard request needs no dynamic planning

- **WHEN** 自适应 profile 中的固定分析合同覆盖一个已知结构化请求
- **THEN** 已声明固定 worker 分支 MUST 无新增语义分类或 dynamic-planner 调用地执行
- **AND** 全部必需分析角色、适用质量门与发布控制 MUST 继续生效

#### Scenario: A caller supplies a legacy mode override

- **WHEN** 统一合同切换后的新公开请求提交 `dynamic_analysis`、`dynamic_task_plan` 或内部执行覆盖
- **THEN** admission MUST 明确拒绝不支持的字段
- **AND** 系统 MUST NOT 执行覆盖、静默接受，或暴露替代的技术模式选择器

#### Scenario: A Graph or policy changes after run creation

- **WHEN** run 记录 Graph 与 policy identity/version/checksums 后，Research 代码或策略发生变更
- **THEN** recovery MUST 使用固定的兼容版本与已接纳决定继续，或 fail closed
- **AND** 系统 MUST NOT 静默切换到 static Graph、新 dynamic definition 或重新分类的策略

### Requirement: Dynamic analysis has a fixed Research stage boundary

`research.analysis` 动态阶段 SHALL 只接收已接纳的 `document`、`evidence_pack` 引用、已准入执行任务意图，以及明确声明且属于同一授权论文/源修订的补充证据引用；输出 SHALL 保持现有 `analysis_branch_refs` 聚合合同。补证 SHALL 使用带来源、授权、修订、checksum 和依赖血缘的版本化 refs，MUST NOT 原地修改已接纳输入。

阶段 MUST NOT 增加未声明的跨阶段依赖、跳过固定 source/evidence 前缀，或创建 publication、side-effect、quality-decision、memory-promotion 任务。补证和分析修复 SHALL 仅通过预先声明、已授权的 repair/loop 边界、exact registered capabilities 与共享请求预算执行。任务或具体策略变化 MUST NOT 改变冻结 Graph、quality profile、必需角色或固定下游 gate。

#### Scenario: Dynamic analysis receives valid inputs

- **WHEN** `document`、`evidence_pack` 及任何补充 refs 均通过适用 deterministic gates 与引用检查
- **THEN** stage SHALL 仅在声明的任务意图、能力、输入、角色与预算合同内接收待校验 TaskPlan 候选
- **AND** stage MUST NOT 将原始 parent messages 或未授权上下文复制到 subagent envelope

#### Scenario: Candidate references an unavailable future output

- **WHEN** 候选任务引用 `research_quality`、`reader_payload` 或其他固定后续步骤的输出
- **THEN** Research stage validation MUST 在派发前拒绝候选
- **AND** 固定下游 Graph MUST 保持不变

#### Scenario: A valid local evidence repair is proposed

- **WHEN** 已接纳 observation 识别到可修复证据缺口，且声明的同论文能力可在剩余预算内处理
- **THEN** Harness MUST 先校验、持久接纳再派发修复，产生新 evidence refs 并重新验证受影响输出
- **AND** 修复 MUST NOT 引入不同源修订、无权材料、未注册工具、accepted checksum 的原地变更或未声明图依赖

#### Scenario: A supplementary reference exceeds the admitted scope

- **WHEN** 候选提供其他 tenant、论文、源修订或无权上下文中的证据
- **THEN** Research MUST 在派发或聚合前拒绝该引用
- **AND** 系统 MUST NOT 以任务复杂或模型 confidence 扩大来源与权限边界

#### Scenario: Analysis aggregation completes

- **WHEN** 已接纳任务结果满足每个必需 Research 分析角色，且对其证据修订仍有效
- **THEN** deterministic stage aggregator MUST 使用稳定 refs 和 checksum 写入 `analysis_branch_refs`
- **AND** `verify_claims` MUST 通过既有输入合同消费该聚合，并保持与所选执行方式无关的验收标准
