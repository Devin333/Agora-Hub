## ADDED Requirements

### Requirement: Versioned Run Identity and Orthogonal Execution Profiles

正式执行 SHALL 使用四类协议记录中的 `run`，对应 `EvaluationRun`，固定 `record_type`、`schema_version`、`protocol_version`、`run_id`、dataset/version/digest、计划 case 集合、corpus/index snapshot、`track`、`execution_profile`、配置及代码指纹、实际 component provenance、预算、环境、重复轮次、执行状态和既有 transcript/artifact 引用。未使用的字段 MUST 显式记录不适用原因，不得冒用其他轨的值。

同一实验中的正式结果主键 SHALL 为 `(dataset_version, case_id, track, variant_fingerprint, repetition)`；重试 SHALL 另记 `attempt_id`，不得作为新增独立题或新增 repetition。`track` MUST 为 `retrieval`、`oracle_generation` 或 `end_to_end`，分别表示 A 检索诊断、B 黄金证据生成诊断和 C 产品问答路径；A 不调用回答模型，B 不参与生产检索方案排名。`execution_profile` MUST 独立为 `fixture` 或 `production`，不得由轨名或是否联网推断。

执行 `status` 与 `qualification` SHALL 分开记录；正式 `decision` MUST 为 `pass`、`fail`、`inconclusive` 或 `not_applicable`，不得从“调用完成”直接推导质量通过。系统 SHALL 校验实际 adapter 身份：真实论文或真实 LLM 搭配 hash embedding、固定回答或其他测试替身仍为 `fixture`。显式请求 `production` 但发现替身时 MUST 在付费调用前拒绝，不得静默降级后给出 production 资格。

#### Scenario: Distinguish completion from qualification
- **WHEN** 一个 production `end_to_end` 单元已完成调用并落盘，但必需评分或人工审核尚未完成
- **THEN** 系统 SHALL 保留已完成的执行状态，并将正式资格记录为 `inconclusive`，不得填写 `pass`

#### Scenario: Reject mixed production adapters
- **WHEN** 配置声明 `production`，实际使用真实全文和真实回答模型，但 embedding 为 hash adapter
- **THEN** 系统 MUST 拒绝该 production 执行配置；只有显式 fixture 身份下才可运行此组合，且结果不得授予生产质量资格

### Requirement: Blinded Track Inputs and Evidence Only Oracle Context

A/C application service port SHALL 只接收问题及预先声明的合法 scope；`case_id` MAY 作为非语义关联字段或独立运行控制传递，但 MUST NOT 携带 `ReferenceLabel`、`reference_answer`、`reference_claims`、期望行为、gold IDs、证据组、审核说明或可访问这些内容的对象、路径和回调。A/C 的上下文及上下文补齐 MUST 来自正常生产检索与来源关系，不得借评测标签筛选或补入证据。

仅 B SHALL 允许评测侧从已审核标注准备原文证据，并通过独立的 evidence-only 输入向相同 answer worker 提供问题、scope、实际 text/image 及必要原文定位；MUST NOT 提供参考答案、参考事实、答案可得性标签或审核推理。`gold_context_used` SHALL 在执行工件中显式记录，而非将标注元数据送给生成器。B SHALL 只适用于已审核可答题；无答案题、待复核题及不支持的输入范围 MUST 显式记录不适用或错误，不得伪造 oracle 通过。

#### Scenario: Keep reference data outside retrieval and answer ports
- **WHEN** 评测协调者同时持有问题与答案册，并启动 A 或 C
- **THEN** 系统 SHALL 在端口边界仅构造盲输入；即使下游声明会忽略 gold，也 MUST NOT 传入完整 case 或答案册访问句柄

#### Scenario: Provide evidence without the expected answer
- **WHEN** B 为一个已审核可答题构造模型请求
- **THEN** 请求 SHALL 仅包含问题、scope、原文证据与引用定位，不包含 `reference_answer`、`reference_claims` 或“应回答什么”的标注说明；无答案题不得用参考拒答文本代替真实生成

### Requirement: Application Ports and Outer Composition Ownership

评测 SHALL 扩展既有 evaluator，由 Evaluation application service 接收结构化 options，Research-owned core 执行确定性实验调度，外层 composition 注入真实 Research application service port、评分 adapter 和既有工件端口。interface 层 MUST 调用 application service，不得直接访问 executor 或 store；业务层 MUST NOT 为接入生产实现而反向导入 `interfaces` 或 `infrastructure`。该边界 SHALL 同时约束 `business/research` 所指业务职责及仓库中的 `backend/research` 包，并禁止依赖 legacy Paper Radar 业务实现。

新增 `scripts.dev rag-eval` 命令组 SHALL 提供 `validate-dataset`、`run`、`score`、`export-review`、`import-review`、`report` 子操作，并通过上述 application 边界调用；库调用方 MUST NOT 通过拼接 CLI argv 复用逻辑。该扩展 MUST 复用既有 evaluator、报告和持久化设施，不另建平行 runner、run/event store 或通用评测平台。既有诊断入口 SHALL 保留其显式兼容语义，不自动获得正式资格。

#### Scenario: Inject production services from composition
- **WHEN** production 评测需要现有 Research 检索、回答和 Harness 能力
- **THEN** 外层 composition SHALL 注入业务 port 的真实实现，Research core 仅依赖业务契约，CLI 仅传递结构化 options

#### Scenario: Reject a reverse dependency shortcut
- **WHEN** 接入方案试图让 Research evaluator 导入 interface composition、直接打开生产 store，或为三轨各建一套独立 evaluator
- **THEN** 该方案 MUST 不满足此契约，即使其局部输出与现有结果相同

### Requirement: Native Harness End To End Gates

C SHALL 通过产品使用的 Research application 路径执行，保留既有 `rag_ask(generate=True, gated=True)` 语义和 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage` 流程。正式 C MUST NOT 以直接调用 generator、固定答案或复用 B 的黄金上下文代替真实端到端路径；fixture C 也 SHALL 保留相同控制路径，仅允许显式注入测试 adapter。

Harness SHALL 继续拥有有界 `PLAN -> EXECUTE -> VERIFY` 状态机，`VERIFY` MUST 由既有 deterministic gate 完成。gate 失败 SHALL 触发受控 replan/retry/halt，并遵守 `max_replans`、`max_turns` 和共享 retry budget；每次 phase transition MUST 写入既有 durable transcript/event log。LLM SHALL 只生成候选内容，不得决定轨选择、预算、quality pass/fail、工具授权、memory write 或 publication；评测代码不得增加第二套执行状态机或绕过原生 gate。

#### Scenario: Retain a rejected answer as a rejected result
- **WHEN** C 的 answer worker 返回了流畅回答，但原生证据或 Claim gate 未通过
- **THEN** Harness SHALL 按预算受控重试或终止，保留 gate 结果及 transition 记录，不得由 evaluator 直接把候选答案作为已通过结果

#### Scenario: Reject direct generation as end to end
- **WHEN** 一个实现绕过 application/Harness，直接将检索片段或 gold 送入生成器，再把输出标为 C
- **THEN** 系统 MUST 拒绝其 `end_to_end` 合规身份，且不得恢复已移除的无门禁 direct answer 产品路径

### Requirement: Complete Stage Traces in Existing Artifacts

每个正式单元及 attempt SHALL 保存可关联原生 transcript 的完整逐阶段 trace，至少包含：

- `source`：实际来源身份、文档版本、corpus/index snapshot、解析完整性和来源工件引用。
- `candidates`：初始候选的 chunk/source IDs、通道、原始 score 和 rank。
- `rerank`：实际启用状态、输入/输出候选、重排 score/rank 及 adapter 身份。
- `context`：最终实际发送的 text/image、顺序、来源范围、token/模态信息、被裁剪或舍弃的内容及原因。
- `answer` 与 `citations`：原始和结构化回答、实际引用定位、解析结果及原生 gate 结果。
- `errors`：阶段、attempt、类型、原因、重试情况、时间和已发生的 usage；没有错误时 SHALL 显式记录空集合。

各阶段 MUST 区分成功、失败、跳过和 `not_applicable`，不得以空字段暗示已执行。A 的未执行生成阶段、B 的未执行检索阶段和显式关闭的 reranker SHALL 标记不适用。工件 SHALL 使用既有 `.newsroom/eval/` 及 Harness 持久化机制，固定内容摘要与引用，不新建执行事实权威源；敏感凭据 MUST NOT 写入 trace。

#### Scenario: Diagnose evidence lost after retrieval
- **WHEN** 必要证据出现在初始候选中，但被 reranker 或最终上下文裁剪移除
- **THEN** trace SHALL 分别保留候选、重排和实际上下文，能够定位丢失阶段，不得用初始命中冒充模型实际获得证据

#### Scenario: Preserve partial traces on failure
- **WHEN** 回答请求超时，或必需 trace 工件未成功保存
- **THEN** 系统 MUST 保留已完成阶段、失败状态和原始 transcript 引用，并阻止缺失工件被当作完整正式结果；不得填入参考答案或空成功记录

### Requirement: Comparable Fingerprints and Controlled Initial State

每个方案 SHALL 固定 `variant_fingerprint`，比较前 SHALL 计算包含全部不变量的 `comparison_fingerprint`。配置身份 MUST 覆盖 dataset/version/digest、相同 case 集合与 scope、原文及 split、track/profile、代码及 adapter、模型/prompt、最终上下文预算、请求/token/费用上限、重复计划、cache/memory 初态、并发和计时边界。身份 SHALL 基于内容与配置，不把机器绝对路径或凭据写入可比性哈希。代码身份 MUST 同时保留 Git revision 和相关工作区内容指纹；正式发布资格证据 MUST 对应可重建的已提交版本，不得只记录 HEAD 而遗漏执行时的未提交补丁。

成对比较 MUST 使用同题、同 repetition 计划及等预算，且 `allowed_differences` SHALL 只允许预声明的单一研究变量及其必然派生变化。比较 chunking/embedding 时 MAY 使用不同 index fingerprint，但 MUST 映射同一冻结原文证据并声明其依赖关系。cache 的冷热策略和 memory 初态 SHALL 在各方案、各题间一致恢复；不匹配时 MUST 标记 `incomparable`，仅展示独立结果，不产生胜负或正式改进结论。

#### Scenario: Compare one retrieval variable
- **WHEN** 两个方案只改变已声明的 reranker 开关，使用相同题集、generator、上下文/token 上限、重复计划和隔离初态
- **THEN** 系统 SHALL 允许同题成对比较，并单独记录重排引入的实际费用与延迟

#### Scenario: Reject a confounded comparison
- **WHEN** 两个方案同时变更检索策略和 prompt，或一方使用已含前次答案的 memory/热响应缓存而另一方为空
- **THEN** 系统 MUST 标记不可比，不得仅凭平均分较高或延迟较低宣布方案胜出

### Requirement: Retrieval Independent Oracle Reuse

B 的 `variant_fingerprint` SHALL 由实际 generator、prompt、输出契约、解码配置、oracle context policy 和预算等生成因素确定，不包含无关 retrieval variant。对于同一 dataset/case、相同生成配置和 `repetition`，系统 SHALL 只执行一份 B 结果，并允许多个 A/C 检索方案引用该诊断结果；共享引用不得重复计费、重复计为独立样本或冒充新一次测量。

oracle 上下文 SHALL 从冻结原文锚点按预声明策略选择每个必要证据组的一条充分路径，独立于待测检索排名、chunk 划分和 gold 命中情况。系统 MUST 记录实际上下文摘要及模态；超出预算、窗口或 generator 能力而不能提供完整必要证据时 SHALL 显式记录错误或范围不适用，MUST NOT 静默裁掉必要证据后称为完整 oracle。

#### Scenario: Reuse one oracle result across retrieval variants
- **WHEN** `dense_only`、`hybrid` 和 `hybrid_reranker` 共享同一 generator/prompt/context budget，且 B 的同一 repetition 已完成
- **THEN** 三个方案 SHALL 引用同一 B 工件，不再发出三次相同 oracle 请求，且 B 不进入生产检索方案排名

#### Scenario: Do not reuse across changed generation conditions
- **WHEN** generator、prompt、证据上下文或预算发生变化，或计划要求新的独立 repetition
- **THEN** 系统 MUST 生成对应的新正式单元，不得把旧 oracle 结果当作新条件下的测量；必要证据无法完整输入时不得伪造成功

### Requirement: Bounded Budgets and Explicit Incomplete Outcomes

执行 manifest SHALL 在调用前声明总请求/token/费用预算、逐请求 timeout、逐例及整次运行期限、并发、重试上限和恢复策略。所有实际 attempts、Harness replans 和重试 MUST 共用预算账本，恢复不得重置已消费预算。未知价格 SHALL 保持未知并记录原因，不得填为零或取消其余 request/token 限制；估算费用 MUST 记录价格依据和版本。

正式 run SHALL 在付费调用前验证冻结题库、完整语料、所需真实 adapter、隔离配置和预算。缺少凭据或不可用的必需能力 MUST 显式记录未执行，不得降级为合成结果。达到预算、期限或取消条件时 SHALL 停止调度新请求并保存未执行集合；run SHALL 记录 `incomplete` 等真实状态，正式必需阶段未完成时 `decision` MUST 为 `inconclusive`，CLI SHALL 返回非零。超时、解析丢证据及质量失败 MUST 留在其适用单元中，不得通过删除失败题缩小分母。

#### Scenario: Stop without losing planned cases
- **WHEN** 运行仅完成部分题目时达到预算或 deadline
- **THEN** 系统 SHALL 保存已执行 usage、未执行主键及原因，停止新请求，并报告未完成而非按成功子集宣布整体验收通过

#### Scenario: Refuse a silent fallback
- **WHEN** production 运行缺少所需模型凭据或真实 adapter，或未知价格导致费用无法确认为零
- **THEN** 系统 MUST 记录明确的阻塞/跳过或未知费用状态，不得替换为 hash、fixture answer 或零费用成功记录

### Requirement: Idempotent Case Recovery and Uncertain Attempts

恢复 SHALL 以 `(dataset_version, case_id, track, variant_fingerprint, repetition)` 定位正式单元，以独立 `attempt_id` 记录每次尝试；外部调用前 MUST 持久化尝试意图，调用后 SHALL 保存响应、usage、错误及内容摘要。已完成且工件完整的单元 MUST 在恢复时复用而不再请求，未完成单元 SHALL 按原 manifest、预算和恢复策略继续；更改题库或配置必须建立新单元，不得覆盖旧结果。

本地工件合并 SHALL 幂等，但系统 MUST NOT 承诺外部供应商调用的 exactly-once。请求可能已被接受但结果未落盘时，attempt MUST 标记为未知，保留请求关联、可能费用及不确定性，不得自动再发或宣称成功。只有完成可用的供应商对账，或按显式恢复策略确认允许重复调用且预算足够后，才 MAY 建立新 attempt；重复费用 MUST 可追踪。

恢复及 `score` 子操作 SHALL 复用既有 artifact/transcript；只重评分 MUST 读取不可变输出，不调用 RAG 或生成器补全缺失回答。缺失工件 SHALL 显式报错或进入待恢复状态，不建立新的 run/event store。

#### Scenario: Resume after a completed case
- **WHEN** 进程中断后恢复，部分单元已有完整回答、引用和 transcript 工件
- **THEN** 系统 SHALL 校验主键、配置及摘要，跳过这些单元的外部请求，仅恢复未完成工作，并保留原有费用

#### Scenario: Record an uncertain external request
- **WHEN** 供应商可能已接收请求，但进程在保存结果前崩溃且不能确认远端结果
- **THEN** 系统 MUST 保存未知 attempt，不自动重发、不伪造答案，也不把它当作零成本失败；后续请求必须满足显式恢复授权和剩余预算条件

### Requirement: Isolated Evaluation State and Side Effects

评测 SHALL 使用独立的 tenant、index、memory 和工件 namespace，并固定可审计的初始 cache/memory 状态。完整原文 MAY 通过合法、只读的来源快照进入评测索引，但 gold 标签、参考答案、题目派生笔记和先前运行答案 MUST NOT 经索引、memory、响应缓存或工具结果进入 A/C。production profile 表示真实 adapter 与路径，不表示允许使用生产业务会话或写生产状态。

系统 SHALL 允许必要的隔离索引构建、评测结果、transcript 和审核工件写入，但 MUST 禁止生产 publication、生产业务 memory 写入、active skill mutation 及自动 promotion。工具授权和副作用策略 MUST 由既有确定性控制面执行；LLM、judge 或评测分数不能扩大权限。允许跨方案复用的只读工件 SHALL 具备明确内容身份，不得携带隐藏状态。

#### Scenario: Persist evaluation artifacts without publishing
- **WHEN** production profile 通过真实 Harness 完成一个回答并保存 trace
- **THEN** 系统 SHALL 将其写入隔离评测工件空间，不产生生产发布、生产记忆更新或 active skill 变更

#### Scenario: Reject contaminated initial state
- **WHEN** 配置指向用户生产会话 memory，或评测索引/缓存含有 gold 答案及前一方案生成的答案笔记
- **THEN** 系统 MUST 拒绝该隔离配置或在执行前按已声明策略重建干净初态，不得悄悄带入污染后继续报告可比成绩

### Requirement: Real Ingestion Provenance and Separate Cost Boundaries

production 的语料入库 SHALL 使用真实源文件及实际 parser、chunker、embedding 和 index 路径；启用的 reranker MUST 是实际声明的实现，关闭时 SHALL 明确标为不适用。系统 MUST 记录实际 runtime provenance，包括源文件/全文摘要、parser/chunker 版本和配置、embedding/reranker/generator 的 provider/model/可得 revision、adapter 及代码版本、索引身份、文档与 chunk 数量和解析完整性；不得仅复制请求配置冒充实际执行证明。B 不使用的检索组件 SHALL 明确不适用，不得伪造其执行记录。

原文有效但 parser 或索引遗漏必要证据 SHALL 作为系统失败保留，不得通过缩减语料、改 gold 或删题取得通过。索引 MAY 复用已验证的真实 snapshot，但 MUST 保留其创建 provenance，不能将旧 hash 索引因本次接入真实模型而重新标为 production。

费用及时间 SHALL 分离为 `index`、`request`、`judge`：建库解析/embedding/索引成本和耗时独立记录；逐题 request latency SHALL 按声明边界覆盖真实请求、重试及 Harness/gate 工作，不混入共享建库或事后评分耗时。复用索引 SHALL 记录复用状态和原始成本引用；未测或不可得费用 MUST 为 `null` 并注明原因。失败、超时及重试 SHALL 保留费用/时间记录，不能仅用幸存成功请求宣称总体快速。

#### Scenario: Reuse a verified real index
- **WHEN** 多个同一语料方案复用已完成真实入库的适用 index snapshot
- **THEN** 系统 SHALL 校验并引用其 runtime provenance，分别报告建库成本与逐题实际请求延迟，不把一次建库费用重复算入每题 latency

#### Scenario: Preserve parser failures and unknown costs
- **WHEN** 原文表格有效但实际 parser 遗漏，或请求失败后供应商未返回完整 usage
- **THEN** 系统 MUST 分别记录可归因的系统失败和未知费用，保留计划单元及可得时延，不得删除失败题、填零费用或以真实语料替代实际 adapter 证明
