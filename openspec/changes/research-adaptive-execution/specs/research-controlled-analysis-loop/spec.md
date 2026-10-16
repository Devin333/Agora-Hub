## ADDED Requirements

### Requirement: Controlled analysis admits only qualified experiment tasks

本 capability SHALL 作为 `research-adaptive-execution` 的专项细化并完整继承其合同。Harness SHALL 仅将单论文的 `analysis.experiments` 准入首版循环，冻结 Graph/version/checksum、目标、论文/source revision、合法 input refs、policy/quality/model/worker/tool bindings、请求预算及 repair 边界。已知结构化目标 SHALL 确定性规范化，准入 SHALL 由服务端已发布 profile 决定，而非用户或模型指定。

既有 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage` 主链及 `analysis.structure`、`analysis.contribution` 等必需角色 SHALL 保留。本专项 MUST NOT 增加复杂度分类模型、planner、TaskPlan、额外 fan-out 或递归委派，也 MUST NOT 将每次阅读或模型思考改成独立 Graph 节点。

关联：AL-FR-01；AL-AC-01、AL-AC-14。

#### Scenario: Sufficient evidence permits a first turn candidate
- **WHEN** 标准实验分析任务已合格准入且现有证据充分
- **THEN** worker SHALL 允许首轮提交 `final_output`，不强制调用工具或增加模型轮次
- **AND** 其他必需角色、交付物及相同 quality profile 下的全部适用 gate SHALL 继续执行

#### Scenario: A qualified task starts with a known evidence gap
- **WHEN** 已接纳目标需要当前论文内补读且所需能力、授权和预算均合格
- **THEN** Harness SHALL 允许直接进入合格循环，不要求先完整执行并失败一次既定分析
- **AND** 系统 MUST NOT 为此次补读额外调用分类模型、planner 或派发子 agent

#### Scenario: Admission dependencies are unqualified
- **WHEN** 模型、工具、输入质量或持久化资格不满足冻结合同
- **THEN** 系统 SHALL 在派发前明确拒绝该能力并记录 unavailable 或 disabled 原因
- **AND** 系统 MUST NOT 用空工具注册、伪造结果、隐藏替代路径或降低质量要求继续执行

### Requirement: Model actions remain bound to the accepted Graph execution

每轮模型请求 SHALL 经生产 application 路径和 Graph-bound worker 复用真实 LLM client/route，携带当前目标、授权输入与证据、可用工具 schema、已接纳观察、必要失败反馈及剩余资源限制。模型调用、工具动作和候选 SHALL 保持冻结 execution identity，并透传真实 usage、错误及关联 receipt；MUST NOT 以 standalone loop 或固定包装旧候选为 `final_output` 冒充真实动作闭环。

首版动作 SHALL 仅为 `tool_call` 与 `final_output`，由确定性协议校验处理。中间动作协议 SHALL 与最终实验业务 schema 分离；provider 原生 `tool_calls` SHALL 经显式适配及真实 client 契约验证，不假定 SDK 返回值已被消费。材料与工具文本 SHALL 仅作为不可信数据；模型 MUST NOT 决定全局路由、授权、gate 结论、memory 写入、skill 更新或发布。

关联：AL-FR-02；AL-AC-02、AL-AC-03、AL-AC-04。

#### Scenario: Tool observations reach the next model request
- **WHEN** 模型提出合格 `tool_call` 且运行时检查允许执行
- **THEN** 真实工具 adapter SHALL 执行动作，其实际 observation SHALL 回填同一任务的下一轮模型请求
- **AND** 中间动作 MUST NOT 被最终业务 schema 强制改写为终态，后续 `final_output` 仍 SHALL 接受完整业务校验

#### Scenario: Native provider actions are explicitly adapted
- **WHEN** 已合格模型 route 返回原生工具动作、usage 或 provider 错误
- **THEN** client adapter SHALL 按已验收协议交付可消费动作及真实 usage、错误、execution identity 和 receipt 关联
- **AND** 系统 MUST NOT 丢弃工具动作、吞掉错误或以虚构 usage 证明调用成功

#### Scenario: Unsupported or privileged actions are rejected
- **WHEN** 动作不受支持、伪造身份，或材料内指令要求扩权和修改控制策略
- **THEN** 确定性校验 SHALL 拒绝该动作，记录原因且不派发所请求的越界操作
- **AND** 普通协议错误仅 SHALL 进入已声明局部修复；权限或 identity 失败 SHALL 停止相应执行，不换接口绕过

### Requirement: Source retrieval reads only authorized material from one revision

`retrieval.read_source` SHALL 注册真实实现并复用现有文档、证据和检索 service，仅读取当前任务已授权的同一论文、同一 source revision。工具 SHALL 返回实际原文或解析结果及可核验的来源、修订、位置、checksum、结果 ref 和执行 receipt；这些事实 MUST 由 service/runtime 提供而非模型自签。ToolRuntime SHALL 执行授权、execution profile、参数、结果、超时、取消与预算检查，不仅依赖 prompt。

单次返回量及累计上下文 SHALL 受冻结 profile 约束，大结果 SHALL 使用既有受控 artifact/ref 机制。首版 MUST NOT 开放跨论文搜索、任意 URL、任意文件路径、shell 或未验收解析能力，也 MUST NOT 通过无上限 observation 文本规避 token 预算。

关联：AL-FR-03；AL-AC-02、AL-AC-04、AL-AC-15。

#### Scenario: An authorized experiment passage is read
- **WHEN** 动作定位当前论文当前修订内已授权且实际可用的实验段落或已解析表格
- **THEN** 工具 SHALL 读取真实材料并返回可核验定位、内容校验信息和结果引用
- **AND** 后续模型输入 SHALL 使用受返回量与上下文限制约束的实际观察，不虚构读取结果

#### Scenario: A request crosses the authorized source boundary
- **WHEN** 参数指向其他论文、其他修订、任意 URL 或文件路径，或 execution profile 不允许该工具
- **THEN** 运行时 SHALL 在读取前拒绝并停止相应分析执行，保存拒绝事实
- **AND** 系统 MUST NOT 通过另一工具、接口、层级或 agent 继续同一越权请求

#### Scenario: Requested source content is unavailable
- **WHEN** 授权材料未实际提供所需图片、表格、页面或解析结果
- **THEN** 工具 SHALL 返回具体不可用事实及缺口，供既有失败分类和质量合同处理
- **AND** 系统 MUST NOT 伪造已读内容、推测数值冒充原文，或通过增加轮次声称具备缺失解析能力

### Requirement: Supplementary evidence requires authoritative admission and versioning

读取既有已接纳证据 SHALL 保留其 identity 与血缘。新增片段 MUST 经既有 evidence/ref authority 校验并持久接纳后才可被合法引用；工具执行成功、observation 已记录和引用合格 SHALL 作为不同事实处理。接纳失败或结果未知时 MUST NOT 用该片段支持已验证断言；模型 MUST NOT 自增 allowlist 或宣布 ref 合格。原 authority 无法表达接纳时 SHALL 先由原 owner 补齐合同并验收，不建立旁路允许引用表。

补证 SHALL 限于同论文、同源修订授权材料，形成可追溯的新版本或派生 refs，并保留旧 evidence pack 与 checksum。系统 SHALL 按依赖使受影响结果失效并重新计算或验证，复用未受影响且仍有效的已接纳结果；MUST NOT 原地改写已接纳输入或继续发布陈旧候选。正式 source revision 改变 SHALL 重新准入。

关联：AL-FR-04；AL-AC-05、AL-AC-06。

#### Scenario: Admitted evidence updates dependent results
- **WHEN** 新片段通过既有 authority 的校验和持久接纳且影响已有实验结论
- **THEN** 系统 SHALL 生成带来源、位置、revision、checksum 和派生血缘的新版本 refs，再验证受影响结果
- **AND** 旧 evidence pack SHALL 保留，仍有效的来源、编译及其他结果 SHALL 可复用，不无条件重跑整条主链

#### Scenario: A tool result has not been admitted as evidence
- **WHEN** 工具文本已返回但补证接纳失败、未完成或结果不明，候选仍引用该新片段
- **THEN** 既有引用 gate SHALL 拒绝将其作为合法证据，保留工具观察与接纳状态的区别
- **AND** 系统 MUST NOT 因文本进入上下文、工具成功或模型自报可信而放行该断言

#### Scenario: A reread introduces another source revision
- **WHEN** 补读发现实际来源已不同于任务冻结的 source revision
- **THEN** 系统 SHALL 拒绝将该内容接纳到原任务补证范围，并要求新修订按既有合同重新准入
- **AND** 系统 MUST NOT 覆盖旧 refs、扩大授权或将旧候选伪装成基于新修订的合格结果

### Requirement: Candidate validation is distinct from Graph verification and publication

最终实验候选 SHALL 适配回既有 claims、scores、证据与来源血缘合同；合同扩展 SHALL 显式版本化，不能静默破坏消费者。对实际存在的结果 SHALL 保留指标值、单位/方向、数据集或评测集、比较对象、实验设置及限制；缺失信息 SHALL 显式表达，MUST NOT 补造数值或比较条件。

局部校验 SHALL 检查动作、结构、必需字段和当前任务约束；Harness SHALL 在有界 `PLAN -> EXECUTE -> VERIFY` 中以 deterministic gates 检查身份、引用资格、业务约束、血缘和质量。顶层 SHALL 独立验收全部必需角色、整体质量与 publication authority；任一局部成功 MUST NOT 替代整体完成。独立语义判定 SHALL 遵循 `research-adaptive-execution-evaluation`，不以引用存在、schema 合法或模型自评证明学术正确。

关联：AL-FR-05；AL-AC-02、AL-AC-12、AL-AC-13。

#### Scenario: A valid candidate proceeds through all acceptance layers
- **WHEN** `final_output` 满足既有实验输出合同且相关证据已合法接纳
- **THEN** 系统 SHALL 先完成局部校验，再执行 Graph VERIFY，并保留其他角色及顶层交付验收
- **AND** 只有全部适用条件合格时，既有 publication authority 才 SHALL 允许正式发布

#### Scenario: Local validation succeeds while Graph verification fails
- **WHEN** loop 提交合法结构候选，但身份、证据血缘、业务质量或必需角色验收未通过
- **THEN** Harness SHALL 按确定性失败分类，仅对可修复缺口执行声明 repair；身份等硬失败 SHALL 停止，不把 loop 成功解释为 run 完成
- **AND** 候选与诊断仅 SHALL 按既有隔离规则保存，MUST NOT 进入正式报告或发布目录

#### Scenario: A structurally valid claim is semantically incorrect
- **WHEN** 独立评测发现候选存在错值、错引、单位或比较条件错误、无据结论
- **THEN** 评测 SHALL 按既有独立语义合同记为相应失败，不因 schema 或引用资格通过而计为正确
- **AND** 控制校验通过 MUST NOT 被描述为真实模型语义收益已经验收

### Requirement: Repairs require accepted facts and bounded failure classification

修复反馈 SHALL 通过既有合同关联真实 observation/gate ref、原因码、目标候选或 claim、输入修订、缺口、允许范围和剩余资源。失败指纹 SHALL 至少包含目标、gate/失败类别、输入修订及已尝试修复类别；仅新接纳证据或新的有效执行事实可支持进一步修复，模型自由文本、confidence、进度字符串 MUST NOT 决定路由。

解析或结构错误 SHALL 仅在局部上限内修复；业务缺证 SHALL 由 Harness 根据已接纳事实进入声明 repair/replan 边界，只重做受影响工作。短暂可恢复故障 SHALL 由既有执行层执行预算内 transport/tool retry；权限拒绝、身份/来源修订损坏、能力不支持、取消或资源上限耗尽 SHALL 失败封闭，MUST NOT 升级为更多 agent、更宽权限或另一执行路线。

关联：AL-FR-06；AL-AC-06、AL-AC-07、AL-AC-08。

#### Scenario: A local format error is repairable
- **WHEN** 动作解析或候选必需字段校验失败且声明的局部修复额度尚未耗尽
- **THEN** loop SHALL 回填关联真实失败的有限反馈，并在原身份、范围和剩余预算内重新生成
- **AND** 格式修复 MUST NOT 被解释为授权修复、identity 替换或业务 gate 豁免

#### Scenario: A gate establishes a repairable evidence gap
- **WHEN** 已声明 gate 接纳可修复缺证事实且存在合格修复能力和剩余预算
- **THEN** Harness SHALL 持久接纳触发事实、失败指纹及局部 repair，再补证并重跑受影响分析
- **AND** 系统 MUST NOT 仅凭模型自述启动 repair，或无条件重跑主链、启动新 planner 与递归委派

#### Scenario: Repeated failure has no new accepted facts
- **WHEN** 同一分析失败指纹再次出现且没有新接纳证据或新的有效执行事实
- **THEN** 系统 SHALL 停止重复修复并记录无进展原因
- **AND** 改写 prompt、更换动作 ID 或重复查询 MUST NOT 被算作新事实或重置累计 attempt、turn、replan

#### Scenario: A terminal failure cannot be escalated
- **WHEN** 发生权限拒绝、identity 或来源修订损坏、不支持能力、取消、预算或时限及修复上限耗尽
- **THEN** 系统 SHALL 停止相应执行的新派发，将真实失败交回 Harness 并按既有合同处理
- **AND** 系统 MUST NOT 扩权、切换 agent 或路径绕过失败，也 MUST NOT 发布伪完整结论

### Requirement: Every attempt consumes the shared request budget

内层 loop、外层 repair、provider/tool retry、其他分析分支及恢复 SHALL 共用请求级权威预算账本和 reservation/receipt。派发前 SHALL 原子预留，按真实 receipt 结算；未知执行 SHALL 保守占用并核对，MUST NOT 默认视为零消费。每次 provider 重试 SHALL 作为额外实际调用计入，而非藏在一个 turn 内；重入、模式变化或恢复 MUST NOT 清零累计消费。

首个 pilot 前 SHALL 冻结版本化 model/task profile，分别给出模型轮次、实际模型调用、工具执行与重试、token、wall-clock、worker attempts、`max_replans` 及适用成本限制，区分局部执行上限与跨重入/repair 的累计上限，并取父级剩余额度与 profile 的更紧约束。缺少可执行限制 MUST NOT 启用 profile；扩额 SHALL 经过既有显式授权 revision。policy 要求保留最终输出机会时 SHALL 在分配工具预算前预留，但不保证成功或授权超额。

关联：AL-FR-07；AL-AC-08、AL-AC-09。

#### Scenario: Retries and branches compete for shared resources
- **WHEN** provider 重试、局部 repair 与其他分支同时申请剩余额度
- **THEN** budget owner SHALL 原子分配不超过请求余额的预算，并对每次实际调用和执行结算
- **AND** 未获额度的工作 MUST NOT 派发，局部 attempt 可重新分配剩余额度但 MUST NOT 获得清零后的新累计预算

#### Scenario: An external execution outcome is indeterminate
- **WHEN** 请求已预留但 crash 或传输错误使真实执行及 receipt 状态无法确认
- **THEN** 系统 SHALL 保留未知占用，利用既有账本、receipt 和幂等机制核对或失败封闭
- **AND** 系统 MUST NOT 盲目重发、免除消费、重复退款，或通过恢复获得额外额度

#### Scenario: Execution is unbounded or exhausted
- **WHEN** profile 缺少有限执行限制，或剩余资源、累计修复额度、时限不足以派发下一动作
- **THEN** 系统 SHALL 拒绝启用或停止新派发，并记录具体限制和消耗
- **AND** 最终输出预留、普通反馈或恢复 MUST NOT 被用来突破请求总额或伪造成功

### Requirement: Durable execution separates replay from safe resumption

准入与派发决定、Graph/worker/model/policy/quality identity、轮次、结构化动作、预留、receipt、观察、证据接纳、候选、gate 和停止事实 SHALL 写入既有 canonical event/transcript；准入及预留 SHALL 先于对应派发持久化，全部 Harness phase transitions SHALL 持久记录。信息 SHALL 复用既有合同与 refs，必要字段 SHALL 沿既有合同版本演进；MUST NOT 建立竞争性的 schema/store/event 体系、预算账本或调度器，也不要求复制原文、私有上下文或内部思考。

回放 SHALL 仅根据已记录事实还原决定，不重新执行模型或工具；续跑 SHALL 保持原冻结合同、有效 refs、pending 状态及剩余预算。已知完成且有有效 receipt 的操作 MUST NOT 重复消费或提交；未知操作 SHALL 先核对，仅在明确可安全重试且符合预算与幂等策略时重发，不承诺外部 exactly-once。必要旧版本不可恢复 SHALL 停止或隔离，不按当前默认值重新路由。

关联：AL-FR-08；AL-AC-09、AL-AC-10、AL-AC-11。

#### Scenario: Replay restores decisions without external execution
- **WHEN** 维护者回放已记录的分析过程以 review 接纳、修复和停止原因
- **THEN** 系统 SHALL 从既有事件与 refs 还原身份、动作、证据、gate 和预算事实
- **AND** 回放 MUST NOT 新请求模型、调用工具、产生新的消费结算或触发正式发布；依据历史事实重建预算投影不构成新消费

#### Scenario: Recovery reconciles interrupted execution
- **WHEN** 中断发生于动作预留、工具返回/receipt、证据接纳、gate 或发布前后
- **THEN** 系统 SHALL 按原冻结 identity 核对已接纳事实、pending 操作与预算后安全续跑或停止
- **AND** 重复、乱序或旧 revision 的事件和结果 SHALL 按既有 authority 去重、隔离或拒绝，不重复派发、结算或发布

#### Scenario: Shared model backends preserve task isolation
- **WHEN** 并行论文、run、分支或顺序任务复用同一模型后端
- **THEN** 系统 SHALL 隔离可变上下文、输入、工具结果、候选、授权与预算归属，并保持各自 execution identity
- **AND** 后端复用 MUST NOT 导致跨任务材料引用、结果串用或预算及权限混用

#### Scenario: Cancellation prevents new actions and disqualifies late results
- **WHEN** 用户取消分析且仍有在途模型或工具操作
- **THEN** 系统 SHALL 持久记录取消并停止新动作派发，迟到 receipt 和结果按原资格、去重及结算规则处理
- **AND** 迟到结果 MUST NOT 恢复已取消执行、替换有效新结果、重复结算或触发发布

### Requirement: User visible outcomes reflect verified business facts

系统 SHALL 复用现有状态投影，展示正在核对的业务事项、补证原因、主要缺口、资源消耗及停止原因；这些说明 MUST 来源于真实执行事实，不建立新的 run lifecycle 或作为路由依据，也 MUST NOT 暴露内部推理、凭据和未授权材料。`final_output` SHALL 仅表示候选提交，只有顶层结果及发布资格合格才能显示整体完成。

证据不足、预算耗尽、服务不可用与取消 SHALL 明确区分，不统一显示完成或未知错误。仅当同一冻结 quality profile 明确允许时 SHALL 提供带限制交付，披露未核实项且不标为已验证结论；否则 SHALL 保留隔离候选/诊断并以未完成结束。系统 SHALL 保留现有取消与必要授权入口，MUST NOT 新增要求用户选择执行引擎的开关。

关联：AL-FR-09；AL-AC-08、AL-AC-12。

#### Scenario: Progress explains accepted supplementary work
- **WHEN** Harness 已接纳补读或核验且实际开始执行
- **THEN** 用户 SHALL 看到相应业务进度、补证原因和已发生资源消耗
- **AND** 界面 MUST NOT 展示隐藏推理、无权材料，或要求用户选择 loop、planner 和 agent 数量

#### Scenario: A stopped or unqualified run remains incomplete
- **WHEN** loop 返回候选但业务 gate 未通过，或执行因证据不足、预算耗尽、服务不可用或取消而停止
- **THEN** 状态投影 SHALL 展示具体未完成原因及主要缺口，不宣称整体分析成功
- **AND** 系统 MUST NOT 因工具成功、局部校验通过或已有候选而触发正式发布

#### Scenario: A quality profile permits a qualified limited delivery
- **WHEN** 材料差异或信息缺失已披露，原 quality profile 允许限制性交付且其余适用 gate 与发布资格均合格
- **THEN** 交付 SHALL 明确区分已验证结论、未核实项和比较限制
- **AND** 系统 MUST NOT 编造缺失值、降低 quality profile，或用限制性交付绕过取消、耗尽和发布资格

### Requirement: Deployment qualification and rollback preserve the frozen contract

首版 SHALL 复用已配置且合格的模型服务及受支持材料 service，MUST NOT 要求新 checkpoint、专用训练模型或额外模型服务。模型动作、真实工具、证据接纳、预算、持久化与质量 SHALL 有可审计资格记录；模型 route/fallback 仅 SHALL 来自已冻结且合格的集合。执行、证据质量和适用桌面生命周期依赖 SHALL 按原 owner 验收；若动态选项与输入质量耦合影响该路径，SHALL 先由所属任务解除并验收，不改变 quality profile。

发布证据 SHALL 建立本需求、实现及测试/运行证据映射，并满足同变更 `research-adaptive-execution-evaluation` 的专项与总体发布合同，不另设总体比较组或阈值。fake 控制测试、局部评测或内部资格 MUST NOT 代替真实语义验收及总体 release；缺证时 SHALL 明确区分控制接线已验证、语义收益未验收和未发布。真实模型实验 SHALL 另行取得资源授权。

版本化启用与回滚 SHALL 仅改变新准入；活动 run SHALL 按原合同完成、取消或安全停止，MUST NOT 静默重路由、删除恢复依赖、降低质量门或扩大权限。普通 business run MUST NOT 更新 active policy、路由、阈值、gate 或 skill；本专项 MUST NOT 提前切换总体公共 API 模式字段合同。

关联：AL-FR-10；AL-AC-14、AL-AC-15、AL-AC-16。

#### Scenario: A qualified profile uses the existing deployment
- **WHEN** 现有模型配置与材料 service 满足动作、工具、质量及全部适用发布依赖资格
- **THEN** 新准入 SHALL 可使用该版本化 profile，无需部署额外 checkpoint 或常驻模型服务
- **AND** 调用及 fallback SHALL 限于已冻结合格集合，界面仍通过 application service 使用业务能力

#### Scenario: Partial verification cannot authorize production release
- **WHEN** 依赖资格缺失，或只有 fake 成功、局部评测而缺少适用总体发布证据
- **THEN** 系统 SHALL 在对应准入范围明确 unavailable/disabled 或未发布状态，并指出未满足的资格
- **AND** 系统 MUST NOT 以已支持多轮为由开启生产默认、临时替换模型或声称整体 release 合格

#### Scenario: Rollback does not reroute an active execution
- **WHEN** 某版本受控分析 profile 被关闭而其原合同下仍有活动或待恢复 run
- **THEN** 新准入 SHALL 遵循回滚后的发布策略，已有 run SHALL 保留原 Graph、bindings、授权、quality profile 和预算血缘
- **AND** 原依赖不可恢复时 SHALL 停止或隔离，MUST NOT 改用新默认路线、清零预算或降低原验收要求
