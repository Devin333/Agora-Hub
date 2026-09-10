## ADDED Requirements

### Requirement: Research working state separates domain facts from control authority
启用本策略的 Paper RAG session SHALL 维护绑定 actor、run、session、来源版本及 state version 的研究工作状态，复用既有论断和证据模型。Harness SHALL 继续唯一控制路由、预算、权限、质量 gate、memory write 与 publication；笔记和模型上下文 SHALL 是可重建的派生视图。

#### Scenario: Read-only handoff notes
- **WHEN** 系统生成或重新生成交接笔记
- **THEN** 笔记 SHALL 从已提交状态派生目标、进度、缺口和关键引用
- **AND** 笔记编辑 SHALL NOT 直接改变正式研究状态或控制权限

#### Scenario: Model attempts to modify control state
- **WHEN** worker 补丁包含预算重置、路由选择、授权、质量放行或发布状态修改
- **THEN** 系统 SHALL 拒绝该修改并记录原因
- **AND** 当前已接受状态和控制状态 SHALL 保持不受该补丁影响

### Requirement: Claims retain evidence scope and uncertainty
每条可复用的研究论断 SHALL 绑定命题 revision、已知适用条件、scope status、证据与依赖版本、核验范围及核验记录。系统 SHALL 区分条件未知、证据不足、存在争议、在已核验范围内得到支持和被替代；引用有效 SHALL NOT 被解释为语义完整或事实绝对正确。

#### Scenario: Missing scope is unknown
- **WHEN** 抽取结果有论断和引用，但没有明确实验适用范围
- **THEN** 系统 SHALL 保存范围未知或待核验状态
- **AND** SHALL NOT 将空条件解释为无条件成立

#### Scenario: Paper report is not independent replication
- **WHEN** 支持证据只有论文自身报告的实验结果
- **THEN** 记录 SHALL 保留该证据层级与实验范围
- **AND** SHALL NOT 将其自动升级为独立复现或普遍成立

### Requirement: Candidate patches are versioned and scope constrained
语义更新 SHALL 作为白名单业务操作候选提交，绑定 `operation_id`、`base_state_version`、producer task/attempt identity 和所依赖证据版本。系统 SHALL 在接受前检查当前身份、版本、操作范围及引用；确定性工具结果和预算使用 SHALL 由程序记录。

#### Scenario: Stale attempt returns a valid-looking patch
- **WHEN** 补丁格式正确，但来源 attempt 已失效或 base state version 过期
- **THEN** 系统 SHALL 拒绝其直接进入正式状态
- **AND** 若需要重新评估，SHALL 基于当前有效身份与依赖生成新的候选记录

#### Scenario: Evidence changed while a patch was generated
- **WHEN** state version 未冲突，但补丁依赖的 source snapshot 或 evidence revision 已失效
- **THEN** 系统 SHALL 拒绝复用旧核验并要求受影响部分重新评估

### Requirement: Recorded conditions cannot be silently removed
结论继续保留或使用时，系统 SHALL 对已记录条件的删除、放宽或解除关联进行确定性检查；此类变更 SHALL 进入范围重新核验，只有新的证据及准入记录满足规则后才能接受。该检查 SHALL NOT 宣称能识别首次未记录的条件。

#### Scenario: Small-sample qualifier is removed
- **WHEN** 原记录为“小样本设置下 A 的准确率高于 B”，补丁保留比较结论却删除“小样本设置”
- **THEN** 系统 SHALL 拒绝直接接受范围扩张并记录 `scope_change_requires_recheck` 或等价类型化原因
- **AND** SHALL 保留原有效限定结论，直到新范围完成核验

### Requirement: Accepted history is the recovery authority
原始候选、结果与拟接受状态 SHALL 先持久化为可验证 artifact；状态接受 SHALL 通过 canonical durable owner 使用预期序号或等价 CAS 提交，绑定前后版本、证据、policy 和 checksum；当前状态 SHALL 从接受记录投影。日志提交失败 SHALL NOT 推进正式状态。

#### Scenario: Crash after event commit
- **WHEN** 接受事件已提交，但进程在写入状态投影前崩溃
- **THEN** 恢复 SHALL 从接受事件重建相同 state version 和内容
- **AND** SHALL NOT 重复调用原 worker 或重放外部写入

#### Scenario: Duplicate operation
- **WHEN** 相同 operation identity 与相同内容被重试提交
- **THEN** 系统 SHALL 返回此前提交结果而不重复应用
- **AND** 同一 identity 携带不同内容 SHALL 被拒绝

### Requirement: Revision invalidates dependent conclusions
事实、范围或证据修订 SHALL 保留旧版本、变更依据与接受记录，并使受影响依赖进入 `needs_recheck`。不同实验条件 SHALL 分开建模；同范围无法裁决的矛盾 SHALL 保留双方证据与争议状态。

#### Scenario: Corrected fact had downstream conclusions
- **WHEN** 结论 C1、C2 依赖的事实 F1 被修正
- **THEN** C1、C2 SHALL 被标记为需要重新核验，不能继续作为已成立的前提
- **AND** 与 F1 无关的工作 SHALL 能在既有权限和预算内继续

#### Scenario: Different experiment settings
- **WHEN** 两条比较结果来自不同数据集或样本设置
- **THEN** 系统 SHALL 保留各自的适用范围
- **AND** SHALL NOT 仅按返回顺序或数值大小互相覆盖
