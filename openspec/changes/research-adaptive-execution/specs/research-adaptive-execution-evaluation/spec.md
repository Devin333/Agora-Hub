## ADDED Requirements

### Requirement: Adaptive evaluation uses comparable internal baselines

评测 SHALL 比较既定分析、始终规划和自适应三种内部策略，并固定论文/材料修订、模型版本、quality profile、有效权限和总预算。评测强制策略 SHALL 只存在于受控评测入口，MUST NOT 作为用户生产请求覆盖。模型/工具/重试等全部资源及失败任务 SHALL 计入比较；没有可信价格版本时 MUST NOT 宣称已节省货币成本。对应 PRD G6、9.2。

#### Scenario: A paired baseline run is prepared
- **WHEN** 同一评测任务准备运行三组策略
- **THEN** evaluator SHALL 固定可比较的输入、验收与资源合同，并记录策略及所有配置版本
- **AND** 始终规划组与既定路径组 MUST NOT 获得更宽松的质量门、额外权限或未记录的预算

#### Scenario: A run spends resources before failing
- **WHEN** 一组策略在意图理解、规划、调用、重试或恢复后失败/超时
- **THEN** 该任务及全部已发生开销 SHALL 保留在成功率、总资源与延迟报告中
- **AND** evaluator MUST NOT 只保留成功样本或把路由成本排除以宣称收益

### Requirement: Held-out quality evidence is independent of the router

评测 SHALL 按论文/任务族隔离 pilot 与正式留出集，覆盖标准任务、需要补充工作的任务及信息不足/不可解/权限拒绝等样例。任务分层和语义质量 SHALL 使用独立参考材料与冻结 rubric，MUST NOT 由待评估 router、模型自评、todo 或 schema 通过率定义。正式留出运行前 SHALL 冻结统计单位、配对重复设计、样本量/效能、阈值和失败分母；控制测试与真实模型语义证据 SHALL 分开。对应 PRD 9.1、9.2。

#### Scenario: A release evaluation protocol is registered
- **WHEN** pilot 已提供方差和可用样本信息，正式留出评测尚未开始
- **THEN** 项目 SHALL 记录版本化协议、论文/任务族划分、独立 rubric、样本需求及配对统计方法
- **AND** 调参数据 MUST NOT 泄漏到未声明的正式留出评测中

#### Scenario: Deterministic fake workers pass all control scenarios
- **WHEN** fake/fixture 测试验证候选拒绝、预算、路由和恢复等控制合同
- **THEN** 结果 SHALL 仅作为对应控制证据
- **AND** 项目 MUST NOT 据此声称真实模型理解、语义质量或成本收益已验收

#### Scenario: Held-out evidence is insufficient or unfavorable
- **WHEN** 正式留出评测未达到预注册的样本要求、质量门或统计阈值
- **THEN** 对应 profile SHALL 保持未验收，并报告不足/失败及不确定性
- **AND** 项目 MUST NOT 为通过而事后降低阈值、删除困难任务或把正式结果重新标记为未用过的留出数据

### Requirement: Release evidence demonstrates safety quality and resource value

首版 release SHALL 同时满足 PRD 9.3 的质量、安全、资源、延迟与恢复门，而非任选一个收益指标。数值 SHALL 被视为拟议验收目标，不是已测性能；任何正式评测前的调整 SHALL 形成受审查的协议版本，正式结果产生后 MUST NOT 为通过而降低标准。对应 PRD AC-01 至 AC-14。

初始门 SHALL 包含：

- 适用控制场景全部通过，零授权、预算、gate 或发布边界绕过；预先冻结的关键案例零新增错误发布、证据丢失或必需角色缺失。
- 已知结构化标准任务零新增语义分类/动态 planner 调用，不为自适应额外 spawn 子 agent。
- 同质量 profile 下，相对始终规划基线，任务质量通过率差的 95% 配对置信区间下界不低于 -2 个百分点。
- 预先标注的需要补充工作任务，相对既定分析基线，质量通过率差的 95% 配对置信区间下界不低于 0，且存在真实基线未完成而自适应合格完成的证据。
- 满足质量门时，混合任务的平均模型 token 相对始终规划基线至少减少 15%，计入理解、规划和重试。
- 同环境、同材料状态，标准任务服务端 active-time p95 不超过既定路径的 1.15 倍；冷启动、人工等待和失败 SHALL 如实分列，不选择性删除。
- exact identity replay、重复/乱序投递、取消、关键 crash boundaries 与 rollout 回滚证据齐全。

#### Scenario: Token savings accompany a quality regression
- **WHEN** 自适应组达到 token 目标，但未满足质量非劣或关键安全案例要求
- **THEN** release SHALL 被阻止
- **AND** 成本下降 MUST NOT 替代质量、权限、证据或发布验收

#### Scenario: Quality passes but the simple path adds mandatory planning
- **WHEN** 真实语义质量合格，但已知结构化标准请求新增了意图/planner 调用
- **THEN** 低开销产品合同 SHALL 判为不通过
- **AND** 项目 MUST NOT 以总体平均成本较低掩盖标准路径的无必要协调

### Requirement: Automatic admission requires upstream and scope-specific qualification

自适应 profile SHALL 同时满足本变更验收与适用的 runtime execution、并行、证据质量及桌面运行管理依赖门。资格 SHALL 绑定 task/quality/model/capability 版本和具体范围；源码存在、其他 profile 通过或文档任务完成 MUST NOT 自动授予上线资格。真实模型评测 SHALL 在明确资源授权下进行。对应 PRD 8、10。

#### Scenario: Adaptive tests pass while an execution dependency remains unqualified
- **WHEN** 本变更测试通过，但需要的并行/恢复/证据质量上游 gate 未通过
- **THEN** 对应 scope SHALL 保持 disabled/unavailable
- **AND** 系统 MUST NOT 以自动执行需求为由跳过依赖验收或声明 enabled parallel

#### Scenario: A qualified serial profile is available
- **WHEN** 已明确批准且完成等价验收的 serial adapter 满足该任务合同
- **THEN** release admission SHALL 能将其作为显式版本化 profile 接纳并记录
- **AND** 系统 MUST NOT 在活动 run 中把未合格的并行失败静默替换为隐藏串行路径

### Requirement: Rollback and observation do not mutate active execution policy

部署回滚 SHALL 限制新准入而不重新路由活动 run；活动 run SHALL 按原 Graph、policy、quality、bindings、预算和 receipts 完成、取消或安全终止。评测/观察数据 SHALL 进入既有受控反馈流程，MUST NOT 在普通 business run 中直接改 active router、阈值、policy 或 skill。对应 PRD FR-09、FR-10。

#### Scenario: A release regression triggers rollback
- **WHEN** 监测或评测发现某 profile 不再满足发布门
- **THEN** 系统 SHALL 停止该 profile 的新准入并保留可追溯 rollback 事件
- **AND** 活动 run SHALL 保持 exact identity，不降低质量门或借恢复重置预算

#### Scenario: A model recommends modifying the production strategy
- **WHEN** 普通研究运行产生提高策略或 skill 的建议
- **THEN** 建议 SHALL 仅作为候选进入已授权的观察、memory 或评测流程
- **AND** 建议 MUST NOT 直接修改 active policy/skill、跳过 held-out eval 或自行发布版本
