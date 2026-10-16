## Context

本变更把 PRD 的“用户不选执行模式”落实为服务端自动准入和阶段内按需执行，而不是新增一个可任意改图的 planner。首版限于已有 Research 单篇论文分析，以及同一 run、同论文源修订范围的补证与分析修复。

当前代码具备固定分析 Graph、TaskPlan 分析阶段、AgentLoop、确定性 gate 和持久控制面。搜索 `GuidedIntent` 解决检索语义，不是执行计划；分析入口仍依赖 `dynamic_analysis` / `dynamic_task_plan`，输入质量增强也与动态入口耦合。总体代码基线及变动边界见 `prd.md` 第 3 节。实验分析专项在 `1807ed6d7302f22b31b9b1eed773a60f8ea8bce4` 核对到更具体的接线缺口：默认 worker 调用一次候选生成；可选动态入口的 `_ResearchCandidateLLMClient` 包装 `final_output`，并注入空 `ToolRegistry()`。这不是缺少通用循环组件，而是缺少真实动作、工具与证据验收的业务接线，详见 `analysis-agent-loop-prd.md` 第 2 节。

用户需要的是研究结果和真实进度；Research application 拥有业务合同，Harness 拥有执行权，interface 只调用 application service。运行安全、并行执行、证据质量和桌面生命周期的现有 owner 不发生转移。此文是拟议设计，不声明相关 active changes 已验收。

## Goals / Non-Goals

**Goals:**

- 标准结构化请求不新增意图分类或动态 planner 调用；保留原有 worker、必要角色和 gate。
- 对固定模板无法覆盖的合法需求，允许首次执行前申请合适策略，也允许依据已接纳反馈局部增加工作。
- 所有分支在冻结 Graph、能力集合、质量 profile 和请求级预算内运行；支持解释、恢复、取消与部署回滚。
- 复用既有持久事件、版本化 refs、TaskPlan、AgentLoop 与评测基础，形成可检验的收益证据。

**Non-Goals:**

- 训练/在线更新复杂度分类器、通用多 agent 架构搜索，或增加另一套 runtime / scheduler / event store。
- 任意网页探索、跨论文研究、独立 Reader QA 或批量执行的自动扩权。
- 让 LLM 决定 Graph routing、quality pass/fail、授权、正式 memory 写入、发布或 skill promotion。
- 把模型内部思考细分成 Graph 节点，或用结构通过率代替研究语义质量。

## Decisions

### D1. 统一用户合同，部署资格和执行策略分离

外部请求表达论文、任务目标、材料范围、质量约束和允许的资源上限。上线后的公开 schema 不接受 `dynamic_analysis`、`dynamic_task_plan`、planner 开关、agent 数量、内部策略/Graph/binding 覆盖。应用服务给出明确 validation error；不能忽略后继续，也不能保留永久兼容别名。

服务端 release admission 决定该 scope 是否可使用已验收的自适应 profile；这不是每个用户可选的模式。未完成依赖与产品验收前，维持既有默认行为。上线后允许的固定、loop、TaskPlan 是内部执行机制，不是三个用户产品。

**替代方案与取舍：**保留“高级模式”开关会把判断负担留给用户，拒绝；按所有请求强制动态规划会增加标准任务成本，拒绝。运维发布控制仍保留，但不允许单次请求绕过。

### D2. 意图候选描述需求，不直接充当路由指令

Research application 规范化任务输入，持有任务意图和 execution policy；provider adapter 仅返回结构化候选。候选字段见 PRD FR-02，是待实现合同，不把现有 `GuidedIntent` 扩充成跨业务控制器。

- 已知结构化任务由普通函数规范化，零额外语义模型调用。
- 自然语言目标需要理解时，使用一次有界的候选生成过程；传输/结构修复也必须计费计次。不得另起“复杂度 agent”重复理解相同目标。
- 模型可提出缺失信息、交付物、局部子目标或建议动作；server 校验 schema、论文/源修订、权限、任务支持范围和候选与请求的一致性。
- `proposed_action` 是可拒绝的建议；真正可供 Graph 消费的是 Harness 接纳并持久化的 policy decision，不是模型原始字段或 confidence。
- 信息不足进入有界澄清；不支持的任务明确 unavailable。两者都不能简单映射为“复杂任务”。

“复杂度”不作为必须存在的总分。决策依据是固定合同覆盖情况、已验证的材料/能力条件、必要子目标和真实执行结果；输入长度、一个“深入”关键词或模型自评分都不是充分条件。

**替代方案与取舍：**独立学习型 router 需要本项目标签、收益模型与 held-out 数据，目前证据不足；首版保留有限候选和可审查策略，将训练方案留作后续变更。

### D3. 冻结粗粒度自适应 Graph，复用已有执行 owner

准入时冻结一个版本化 Graph/profile，其中声明固定业务主链、分析阶段的合法分支、补证/修复边界和终端 gate。已接纳的决策只能选择这些分支或填充 TaskPlan 的合法任务空间，不得中途替换 Graph identity/checksum。

优先复用 `Choice`、`BoundedLoop`、既有 `HarnessGraphRepairBinding`，以及 Function、AgentLoop、Subagent / TaskPlan stage 的准确注册合同。不新增 `ADAPTIVE` worker kind，也不在 application 中写竞争性调度循环。

```text
source / document / evidence（既有固定前缀与 gate）
                         ↓
      已接纳策略的 Choice（不是原始模型输出）
       ├─ 已有固定分析 worker / 必需角色
       ├─ 已注册的 bounded AgentLoop
       └─ 有界 TaskPlan / 合格 child adapters
                         ↓
             必需角色聚合 / VERIFY
               ├─ 合格 → 既有报告与发布门
               ├─ 可修复 → 已声明 repair → 再验证
               └─ 不可恢复或预算不足 → halt
```

初始策略建议可以在执行前形成，但任何任务派发仍要求其输入 refs、能力与预算已就绪。所有路径产出同一 `analysis_branch_refs` 合同，并保留 `analysis.structure`、`analysis.contribution`、`analysis.experiments`；不因节约成本省掉角色。

固定分支复用原实现，不把“零额外 planner”误写成“零模型调用”。单一子目标内的探索交给有界 AgentLoop；只有需要独立输入输出、验收、恢复或可证明并行收益时才分解成 TaskPlan。协调不产生收益时不 fan-out。

**替代方案与取舍：**运行时把 static Graph 换成 dynamic Graph 会破坏冻结身份和恢复，拒绝；统一包一层能自由调用子 runtime 的超级 agent 会形成第二控制器，拒绝；每个思考动作一个节点会放大维护与迁移成本，拒绝。

### D4. 用可执行修复条件替代任意“升级模式”

Research policy 把已接纳的 observation / gate result 映射到有限合法修复动作。候选必须引用实际目标、输入 revision、失败/覆盖检查结果和已有修复能力。

| 情况 | 处理 |
| --- | --- |
| 固定模板覆盖任务、输入合格 | 走既定 worker，不强制规划 |
| 合法目标需要模板外的同论文核验 | 在声明范围内申请 loop 或 TaskPlan，不要求先跑完另一整条路线 |
| 已接纳事实证明可补足的证据缺口 | 申请同论文补证，形成新 refs，再验证受影响结果 |
| 已声明可重建的候选输出失败 | 进入受限 retry/replan，保持原 gate |
| 必要信息不足 | 有界澄清，不伪装成复杂任务 |
| 权限拒绝、取消、预算耗尽、状态/identity 损坏、能力不可用 | 按原错误语义暂停/终止/恢复处理，不升级绕过 |

升级使用失败指纹，至少包含目标、gate、输入修订和已尝试修复类别。没有新增可接纳事实时，同一指纹不得反复升级。不同动作也不能重置请求级重试与规划额度；普通 transient retry 仅按既有 retry policy 执行，不能借“升级”增加次数。

进度文案、todo、模型自称完成或困难都不是 authoritative observation。模型发现的疑似缺口首先是候选，只有经过已声明校验且有授权修复能力，才可能成为 policy 输入。

**替代方案与取舍：**只做入口分类无法应对执行新事实；任何失败都转复杂模式则掩盖权限/基础设施错误。采用初始适配加受控局部反馈，接受有限策略可能无法解决某些任务的边界。

### D5. 质量、证据身份与发布资格独立于执行方式

quality profile 由准入合同和服务端发布策略确定，不由策略候选覆盖。当前 quality-enabled 与 dynamic flag 的耦合应在本变更实现时解除，但原证据质量 owner 的门和 release evidence 必须保留。

同论文补证仅从已授权、同源修订的真实材料产生版本化的新候选/已接纳 refs；记录 provenance、checksum 与 derivation。不得原地改写已接受 evidence pack，或因新内容出现而无条件复用旧分析。policy 按依赖血缘重新验证受影响输出；未受影响且仍有效的 refs 才能复用。

补证任务不会获得 source collection、顶层 quality、publication 或 memory promotion 的权限。source/evidence 的固定前缀不能跳过；局部补证是声明的边界内修复，不是任意回跳图节点。若源论文发生实质修订，按新任务 revision 重新准入，不能冒充同一证据状态。

只有顶层必需角色、真实 gate 和 publication authority 合格才能进入正式报告/产物目录。候选、诊断和子任务结果按既有隔离存储规则保存，不代表正式发布；deterministic gate 不被宣传为学术语义正确性的充分证明。

**替代方案与取舍：**用简单/复杂模式决定证据门会产生绕过；把所有中间输出丢弃重跑则浪费资源。保持同一质量合同，并按合法引用和依赖局部失效。

### D6. 一个请求血缘、一份有界资源责任

复用既有 budget / reservation owner；如现有合同缺少解释阶段的归属，扩展同一责任边界，不新建旁路账本。解释、planner、worker、tool、child、retry、compaction 与恢复都关联请求血缘，受总 turn、replan、调用、token、任务数、并发和时间上限约束。

派发前原子预留；结果后按 receipt 结算；结果未知时保守占用或核对，不能因恢复重新发出未知副作用。升级只能分配剩余额度，不能给 child 新开无限预算。预算扩充需要原有授权并形成可追溯 revision；用户澄清不自动重置资源。

**替代方案与取舍：**每种策略自带独立 budget 简单但可通过模式切换无限续航，拒绝；复用总账和预留会增加集成工作，但能覆盖并发与恢复的真实成本。

### D7. 接纳先持久化，恢复不重新判断“复杂度”

在既有 canonical event / transcript 中保存：proposal/ref、接纳或拒绝理由、授权输入与 observation refs、Graph/policy/quality/binding 版本、预算预留、attempt/plan revision、执行及结果 refs。事件与持久化责任沿用当前控制面。

接纳与资源预留必须先于对应派发持久化。exactly-once 不作为外部工具的凭空保证；通过幂等键、accepted receipts、pending 状态与恢复核对避免重复调度、双重结算或发布。不能仅根据 UI 状态判断任务完成。

恢复使用原冻结 profile 和已接纳决定，不重新调用分类器、重写计划或按当前默认值改路。所需旧版本不可用时 fail closed。恢复旧 run 的明确版本支持不等于继续接受旧的用户模式参数。

界面仅投影业务事实、简洁原因、缺口、资源消耗与既有取消/必要授权操作；不展示私有推理、凭据、其他 run 的上下文或未经接纳的策略结论。

**替代方案与取舍：**通过最新 prompt 重算计划恢复容易产生不同结果和预算重置，拒绝；新增 router event store 会引入双重真相，拒绝。

### D8. 发布依据是同质量配对证据，不是某篇论文的收益数字

复用既有评测入口与证据存储；内部评测可固定使用原路线、始终规划或自适应，但这些仅为 evaluator fixture/policy，不暴露为用户生产参数。

三组必须使用相同 source revision、模型/价格版本、输入材料、quality profile、有效权限和总预算；统计解释、规划、重试、失败与恢复的全部成本。离线控制测试和真实模型语义证据分开，禁止用 fake 成功证明智能收益。

按论文/任务族隔离 pilot 与正式留出数据，使用独立参考材料和冻结 rubric；在留出运行前冻结统计单位、样本量/效能、配对重复设计及阈值。PRD 第 9.3 节的数值是拟议发布目标，不是已测数据；证据不足维持未验收，不事后排除困难样本。

**替代方案与取舍：**只比较成功任务或只报少调用会奖励早停和质量退化，拒绝；优先质量/安全门，再看总资源和延迟。

### D9. 首个交付切片是实验分析的真实循环，不附带 planner 或新模型

`research-controlled-analysis-loop` 细化总体能力中一个明确的 worker：同论文、同源修订、已授权材料内的实验分析。结构、贡献等必需角色及固定前后缀不变；不要求先执行完整失败路径，也不因接入 loop 额外生成 TaskPlan、增加 child 或递归委派。材料充分时首轮即可提交候选。

实现复用已有 Graph-bound activity/child 接入，按实际所在路径选择适用适配器，不能以 standalone AgentLoop 绕过生产身份、预算、授权或发布门。Research application/ports 持有目标、失败分类与候选适配；composition 只注入模型、工具和资源；通用 framework 不承载论文业务判断。

真实 LLM client 继续使用已配置、已验收的 route，必须保留 execution identity、usage、errors 和对应调用证据。首版动作是 `tool_call` / `final_output`；中间动作协议与最终实验业务 schema 分离。沿用结构化动作协议时须提供明确的提示与解析合同；使用 provider 原生 `tool_calls` 时须有显式规范化和契约测试，不能假定已有 SDK 返回值自动进入循环。只包装旧 worker 最终结果的 client 不能满足此能力。

既有实验输出的归一化、claims/scores 与下游消费者继续复用；确需扩展时进行版本化，而非另造一套并行结果模型。新增 binding/repair 边界形成新准入版本，不原地改活动 run 的 Graph 或 worker identity。模型仍只提出局部动作和内容候选，Harness 仍掌握 routing、VERIFY、预算及发布。

**替代方案与取舍：**拒绝先训练路由/工具选择模型、强制每个任务走动态 planner 或复制 loop。选择一个完整业务切片会限制首版覆盖面，但能先验证真实收益且不增加必选 checkpoint 部署。

### D10. 工具观察与合法证据是两个不同的接纳事实

先接通已声明的 `retrieval.read_source`，由 ToolRuntime 调用真实文档/证据 service；权限、参数、execution profile、预算、取消及结果边界由代码执行。工具只读当前授权论文及源修订，返回真实文本/解析结果、来源位置、checksum 与结果 refs；较大结果沿用现有 artifact/ref 限制。未解析的表格或不可用材料返回事实性失败，不由 adapter 伪造业务内容。

工具返回成功只能证明一次读取的结果存在，不能直接证明其成为可引用证据。已接纳证据保持原 identity；新增片段必须由既有 evidence/ref authority 校验并持久接纳，形成带来源、修订、checksum、派生关系的新版本 refs。原 evidence pack 不被原地修改，模型不得自行追加合法引用或授权集合。

冻结的是来源授权范围、接纳规则、Graph/quality/policy/bindings；范围内新增的派生 refs 仍须显式接纳。若消费新 refs 需要更新输入投影、grant 或修复 revision，应沿已声明的接纳/repair 合同产生新版本并保留历史，不让旧 identity 自动获得新权限。scope 或 source revision 变化须重新准入。只有合法且仍有效的 refs 能进入候选与后续 gate；受影响的旧输出失效并重新验证，其他仍有效输出按原血缘复用。

**替代方案与取舍：**拒绝只把补读文本拼进 prompt、在 worker 内改写已接纳 evidence pack、或建立旁路 allowlist。接入原 authority 增加合同适配工作，但能保住当前 lineage gate 与回放依据。

### D11. 局部反馈、业务修复和真实调用计量保持分层

loop 内处理普通动作解析/输出结构问题，并在授权内提出补读；Harness 只消费已接纳的 gate/observation facts 来执行声明的业务 repair。失败反馈绑定原因、目标/claim、输入修订、缺口、允许修复范围、预算与事实 ref；模型自由文本、confidence 或“需要更多时间”本身不产生路由资格。

权限拒绝、身份/来源损坏、不可用能力、取消和耗尽不得通过换工具、换路线或委派绕过。基础设施可恢复重试由原执行层处理。失败指纹绑定目标、gate/失败类别、输入修订和已尝试修复类别；改写问题或重复同一查询不算新事实。没有新接纳证据或有效执行事实时停止重复升级。

预算区分逻辑 turn 与实际 provider/tool 调用，包括 transport retry、repair、并行其他角色、恢复和未知结果；全部归属原请求 ledger，先原子预留后按 receipt 结算。attempt 可以从剩余预算重新分配局部上限，但累计消费不清零。M0 为 task/model profile 冻结有限 turn、工具、replan、实际调用、token、wall-clock 与适用成本上限；不能把某个现有 SubAgentSpec 数值当作统一最优默认。缺少可执行限额的 profile 不准入。

复用既有 phase/turn、动作、receipt、证据接纳、候选及 gate 记录。回放只还原事实，不重调模型；恢复后的新执行使用原 identity 与剩余资源。已知完成结果不重复结算；未知状态先核对，只按明确幂等和预算策略重试。并发分支不能串用可变上下文、工具结果、授权或额度；取消后停止新派发，迟到结果仍按原提交资格处理。

**替代方案与取舍：**拒绝“内层各自有限所以总成本有限”、恢复后重建空账本、或任意宣称 exactly-once。复用原执行事实和预算 owner，接受无进展、状态不明或版本缺失时明确停止。

### D12. 专项验收不等于总体自适应发布

以专项 `AL-FR-01` 至 `AL-FR-10`、`AL-AC-01` 至 `AL-AC-16` 建立正式规范、实施任务、自动测试和真实运行证据映射。fake/fixture 测试必须走生产 application/Graph-bound worker/真实工具 adapter/gate 接线，证明工具结果回填，而不是只观察调用过 AgentRunner。它们只证明控制正确性，不能证明模型语义收益。

专项两组评测比较原一次候选生成与受控实验分析 loop，固定原始材料、源修订、模型、价格、质量、权限、可读范围及总预算；新增读取、重试、失败和恢复全部计量。按论文/任务族隔离 pilot 和留出集，独立 rubric 校验值、单位、比较条件与证据支持，保留完整失败分母及置信区间。真实模型运行需要另行取得资源授权。

局部两组可验证实验分析补工作分层的收益及标准任务延迟，不能替代 D8 的三组总体比较，不能以不同基线宣称达到对始终规划的 -2 个百分点非劣界或 15% token 降低门。专项通过不自动勾选覆盖范围更大的父任务，也不打开尚不合格的全局 release。部署沿用现有模型后端，无新增必选 checkpoint；质量与必要角色保持不变。

用户状态只投影“分析、补证、核验、缺口、取消、已完成”等事实；候选成功不冒充总体完成。证据不足仅在原质量 profile 允许时作明确限制性交付，否则停止且不发布。按 scope 准入与回滚只影响新 run，旧 run 保持冻结合同及恢复依赖；不存在用静默 one-shot fallback 放宽已接纳合同的例外。

**替代方案与取舍：**拒绝用类名、fake 成功、单个真实案例或局部两组数据宣称整体上线。先完成实验分析的控制与语义证据，再满足原 owner 和总体产品门，避免将接线工作包装为已验收的自适应系统。

## Risks / Trade-offs

- [意图候选可能误解目标] → 保留结构校验、范围/证据校验、必要澄清、真实反馈和独立语义评测；不把模型 confidence 当授权。
- [策略更多造成控制复杂度] → 只声明少数业务粒度分支，复用 owner，并通过新策略的收益和维护成本决定是否保留。
- [固定与动态质量耦合难拆] → 先建立统一 quality profile 的跨路径合同测试，未达成前不开启该 scope。
- [已有能力缺少可调度的补证 adapter] → 明确补齐真实能力并验收，不能让 prompt 假装能力存在；缺失时不可升级。
- [并发与恢复放大超支风险] → 先原子预留与 durable acceptance，再派发；关键 crash boundary 使用故障注入测试。
- [额外的语义评测成本] → 使用冻结 pilot 估计样本需求，真实模型调用须获授权；不以合成结果冒充 live evidence。
- [与多个活跃变更冲突] → 在每次实现/发布前核对 owner 和真实 gate 状态，不复写他人完成记录；本次不修改这些变更。

## Migration Plan

1. 冻结首版 scope、输入合同、policy/quality profile、原因码、总预算归属与评测计划；复核依赖变更真实状态，不因文档存在标记验收通过。
2. 在现有 application / composition 接入统一准入、意图候选和版本化自适应 Graph，先通过已知结构化请求无额外 planner 的合同测试；生产默认仍保持不变。
3. 先按 `tasks.md` 第 8 节完成实验分析的真实 loop 切片：合同 → 模型动作 → 真实只读工具 → 证据 authority → Graph-bound 输出与验收 → 预算/恢复/状态 → 接线与语义证据；该切片不依赖额外 planner 或新增 TaskPlan。更广的 TaskPlan 接入按原任务独立推进。所有策略的同质量 gate、取消及持久恢复均须合格，不合格 profile 不进入候选集合。
4. 在受控环境完成错误/注入/恢复测试与配对真实模型评测；满足本 PRD 和上游 release gates 后，按合格 scope 启用服务端 admission。
5. 同次用户合同切换中移除界面模式开关，明确拒绝旧外部模式字段；新 run 使用已验收的统一合同。内部评测强制策略与运维 rollout 保持独立、不可从用户请求注入。
6. 仅为已有 run 保留所需的 exact 旧版本恢复绑定与 receipts；清理不再服务新准入的旧路由分支和无必要兼容层。关闭 release 只限制新准入，活动 run 继续按冻结合同完成、取消或安全终止。
7. 回滚不得放宽 quality gate、权限或数据隔离。若明确批准了等价且合格的 serial adapter，必须在准入时固定；不能把缺少并行能力静默替换成未验收路径。

## Open Questions

控制权、用户不选模式、质量不降级、Graph 不换身份和共享预算不是待定事项。以下参数需实现阶段用证据冻结，不阻塞本 PRD，但阻塞相应发布：

- 真实 provider / capability 组合、适用的并行或 serial release 资格，以及可覆盖的具体修复类型。
- 基于 pilot 方差的正式样本量、配对统计方法和人审 rubric 细则；必须在正式留出结果产生前批准。
- 可承受的请求级预算默认值与成本计价版本；先沿用已审查配置，不能把当前默认值或论文数字当作本项目最优值。
