## ADDED Requirements

### Requirement: Versioned Dataset and Case Record Contracts

正式评测协议 SHALL 使用 `dataset`、`case`、`run`、`score` 四类版本化记录，分别对应 `DatasetManifest`、`EvaluationCase`、`EvaluationRun` 和 `EvaluationScore`；本能力定义前两类，执行和评分能力分别定义后两类，不得用未声明的宽松字典替代其契约。契约 SHALL 统一既有 `EvidenceQAPair` 的序列化与消费边界，不另建互不相通的论文 QA 模型。每类记录 MUST 包含 `record_type`、`schema_version` 和 `protocol_version`。冻结前 SHALL 固定以下必填字段、枚举、显式空值语义和引用规则：

- `dataset`：`dataset_id`、`dataset_version`、`lifecycle_state`、`release_purpose`、`dataset_digest`、`corpus_snapshot_id`、`case_manifest`、`split_manifest`、`review_manifest`、`annotation_version`、`license_reference` 和 `authoring_protocol`；manifest SHALL 固定成员身份、内容摘要和审核依据。
- `case`：`dataset_id`、`dataset_version`、`case_id`、`case_digest`、`question_text`、`scope`、`question_type`、`paper_group_id`、`split`、`answerability`、`expected_behavior`、`reference_answer`、`reference_claims`、`required_evidence_groups`、`provenance`、`exposure_history` 和 `review`；其逻辑内容 SHALL 按题目与参考标注权限分开持久化，不代表生成器可以接收完整记录。

`case_id` SHALL 在同一数据集版本内唯一，`run` 与 `score` MUST 引用明确的 `dataset_id`、`dataset_version`、`dataset_digest` 及 `case_id`，不得引用可漂移的 `latest`。系统 MUST 区分字段缺失与经协议允许的显式 `null`，拒绝未知契约版本、重复样本及悬空引用。

#### Scenario: Load a complete frozen contract
- **WHEN** 加载已冻结版本，所有 case、语料和审核引用的身份及内容摘要均匹配
- **THEN** 系统 SHALL 返回该版本确定的完整成员集合，并使后续 run 和 score 引用同一身份，不在加载时生成新题或参考答案

#### Scenario: Reject incompatible or ambiguous records
- **WHEN** 输入包含未知 `schema_version`、重复 `case_id`、缺失的 `reference_answer` 字段或不匹配的 `dataset_digest`
- **THEN** 系统 MUST 报告明确的契约错误，且 MUST NOT 将缺失字段自动补为空答案或继续作为正式评测输入

### Requirement: Physical and Typed Question Reference Separation

题目 `questions` 与参考标注 `references` SHALL 存储在不同文件或独立存储对象中，例如 `questions.jsonl` 与 `references.jsonl`，通过版本化 `case_id` 关联；它们是同一逻辑 case 的分权投影，而非新增顶层记录类型。两侧 MUST 同时通过版本、成员完整性和摘要校验。

送入检索或端到端 application service 的 `QuestionInput` SHALL 只含 `question_text` 与事先声明的合法 `scope`；运行关联标识和预算等控制信息 SHALL 通过不含标注的独立控制参数传递。`ReferenceLabel` 中的答案、`reference_claims`、证据组、答案可得性、审核说明和标注路径 MUST NOT 出现在该 DTO、提示词、检索过滤器或附带的可读取句柄中。制作、oracle 上下文准备和评分组件 SHALL 分别按需读取 references，普通 RAG 执行组件不得因共用 evaluator 而获得该对象。

#### Scenario: Execute using only the question projection
- **WHEN** 同一 case 被送入 `retrieval` 或 `end_to_end` 轨
- **THEN** application service SHALL 仅收到 `QuestionInput` 和无标注的运行控制参数，参考记录仅由有相应职责的组件持有

#### Scenario: Reject hidden reference access
- **WHEN** 输入表面上只有问题，但附带完整 `EvaluationCase`、gold chunk IDs、reference 文件路径或可返回标注的回调
- **THEN** 输入校验 MUST 拒绝该调用，而不是依赖提示词要求模型忽略这些信息

### Requirement: Question First Independent Annotation

题库制作 SHALL 记录 `authoring_method`；QASPER-inspired `abstract_only` 流程在提问阶段只向题目作者提供选定论文的 title、abstract 和预先声明的 scope，先记录 `author_input_manifest`、`question_digest`、`question_frozen_at`，再向独立标注会话或标注者提供全文并记录 `annotation_started_at`。系统 MUST 保留阶段及输入权限证据，不能只凭最终问题文本声称隔离。`real_user`、`fulltext_targeted` 和模板题 SHALL 分别记录实际来源及可见输入，不得伪称摘要盲提问；摘要未出现的 Table/Equation 编号不得被伪造为摘要中已有的信息。

题目起草和全文标注 MAY 使用 LLM 辅助，但 MUST 记录辅助模型、输入范围及生成配置，并由具名人工审核者终审问题、参考答案、证据和答案可得性。LLM 自评或自动规则通过 MUST NOT 替代人工终审。全文标注后需要改题时 MUST 重新建立题目版本和审核链，不得把修改后的题目伪装成原先已冻结的问题。

#### Scenario: Review an assisted question after freezing
- **WHEN** LLM 仅根据 title 和 abstract 起草问题，问题及 scope 已冻结后才开展全文标注
- **THEN** 系统 SHALL 保存两个阶段的输入摘要与时间顺序，并仅在人工完成终审后将该 case 标记为已审核

#### Scenario: Reject question generation from target chunks
- **WHEN** 作者先读取待测系统生成的 chunks、检索结果或全文答案，再反向生成问题，或直接采用 LLM 的审核结论
- **THEN** 系统 MUST 拒绝其独立制作及人工已审核资格，并保留暴露记录；该样本不得作为新增独立题或干净 heldout 题计数

### Requirement: Source and Exposure Provenance

每个 case 及其语料 SHALL 保留可核验的来源链，包括原始题库或新增制作来源、`paper_id`、`document_version`、`source_uri`、`source_sha256`、获取时间、创建方式和人工审核身份。使用第三方题库、已有 79 题或 LLM 辅助内容时 MUST 标明来源与派生关系，不得通过改写文本抹去继承关系。

`exposure_history` SHALL 记录论文全文、问题、标注或评分反馈何时向何种作者、模型配置或调参流程暴露，以及用途和证据引用。项目内无法确认的暴露史 MUST 明确标为 `unknown`，不得等同于 `clean`；系统 MUST NOT 声称该记录能证明模型预训练从未见过公开论文。关键来源缺失 MUST 进入 `needs_review`，未知或已用于调参的来源不得获得 heldout 资格。冻结后的新暴露事件 SHALL 追加到既有审计工件并影响后续使用资格，不得回写冻结内容以掩盖暴露。

#### Scenario: Preserve inherited development exposure
- **WHEN** 某题从已有题库导入并曾用于 prompt、阈值或检索参数调整
- **THEN** 系统 SHALL 保留原题标识、派生关系和调参用途，并限制为开发或回归用途，即使问题措辞已改写

#### Scenario: Treat unknown history as unknown
- **WHEN** 一篇论文的来源可核验，但无法证明其问题或反馈是否已参与调参
- **THEN** 系统 MUST 显式记录未知暴露史并拒绝将其声明为干净 heldout，不得以缺少日志作为未暴露证明

### Requirement: Human Reviewed Answerability

`answerability` SHALL 区分 `answerable`、`unanswerable` 与 `needs_review`。`answerable` MUST 有经人工终审的参考答案、带稳定 `claim_id` 的原子 `reference_claims` 和充分证据，且 `expected_behavior` SHALL 为 `answer`；`unanswerable` MUST 由人工在声明 scope 的完整可用原文中完成核查，记录核查范围、全文完整性及无答案理由，`expected_behavior` SHALL 为 `abstain`，且 `reference_answer` SHALL 显式为 `null`。可用于支持拒答的原文证据 MAY 被标注；没有此类证据时 MUST 显式表示空证据组，而不是缺失字段。

检索未命中、标注者暂未找到、PDF 或表格解析缺失、文档不可访问、scope 不明及审核分歧 MUST 记录为运行错误或 `needs_review` 原因，MUST NOT 自动改标 `unanswerable`。未解决的 `needs_review` case 不得进入已审核冻结题集；运行中发现的问题 SHALL 保留原标注、记录异常并启动版本化纠错，不得就地改动答案可得性。

#### Scenario: Approve a genuinely unanswerable question
- **WHEN** 人工已检查 scope 内完整论文并确认所问实验或结论未被报告，且记录了核查依据
- **THEN** 系统 SHALL 接受经终审的 `unanswerable` 标注，而不要求为了类别配额编造答案或证据

#### Scenario: Keep missing parsing separate from no answer
- **WHEN** 原论文可能包含答案，但目标表格尚未解析，或本次检索没有返回相关段落
- **THEN** 系统 MUST 分别记录解析待审核或检索未命中，且 MUST NOT 将该事实转换为参考标注中的无答案

### Requirement: Source Anchored Evidence Groups

参考证据 SHALL 锚定版本化原文，而不是以某次系统的 chunk ID 作为唯一标识。每个 `anchor_id` MUST 指向 `paper_id`、`document_version`、`source_sha256` 和受支持的 `claim_id`，并包含可复核的位置、原文摘录及内容摘要；位置 SHALL 按内容类型使用页码、章节、段落或字符范围，表格或图形证据还 SHALL 指明表格/图形及行列、单元格或 caption 等必要定位信息。依赖规范化全文位置时 MUST 同时固定该全文工件摘要。PDF 页序号与印刷页码 SHALL 分开记录；LaTeX 来源 SHALL 使用 section/element/span 等真实定位，不得伪造 PDF 页号。

`required_evidence_groups` SHALL 表达必须共同满足的事实或条件：所有必要 group 之间为 `all_of`，每个 group 内的 `alternative_sufficient_sets` 之间为 `any_of`，每个充分集合的 `required_anchor_ids` 又为 `all_of`。每个 group MUST 关联其支持的 `reference_claims`。系统 MUST 保留这三层结构，不得把必要组与替代集合展平成“命中任一即可”。`answerable` case 的每个必要 group MUST 至少有一条经人审核实充分的证据路径，全部必要组共同支撑完整参考答案。

#### Scenario: Accept an alternative complete evidence path
- **WHEN** 必要组 `method` 的替代充分集合为 `[anchor_a, anchor_b]` 和 `[anchor_c, anchor_d]`，必要组 `result` 需要 `[anchor_e, anchor_f]`，结果完整覆盖 `anchor_c`、`anchor_d`、`anchor_e`、`anchor_f`
- **THEN** 系统 SHALL 将其识别为每个必要组都满足一条充分路径，不得因未覆盖 `method` 的另一条替代路径而认定证据必然不足

#### Scenario: Reject mixed partial evidence
- **WHEN** 同一标注下，`method` 仅覆盖 `anchor_a` 与 `anchor_d`，或 `method` 完整但 `result` 缺失，或只给出表格数值而缺少必要行列标题
- **THEN** 系统 MUST 保留证据不充分状态，不得通过跨组拼接命中数或脱离表格语义的文本重合判为完整证据

### Requirement: Index Snapshot Bound Evidence Mapping

原文锚点到检索单元的映射 SHALL 是独立的版本化派生工件，固定 `dataset_version`、`dataset_digest`、`corpus_snapshot_id`、`index_snapshot_id`、`mapping_version`、parser/chunker 配置摘要及映射内容摘要。映射 MUST 基于该 index snapshot 的实际 chunks 生成，支持一个锚点覆盖多个 chunks、多个锚点落入同一 chunk，并记录 `exact`、`equivalent`、`ambiguous` 或 `missing` 状态、完整/部分覆盖及匹配依据。

系统 SHALL 根据原文范围和内容覆盖验证映射，不得将仅有重叠视为完整证据。改变 parser、chunker 或 index snapshot 后 MUST 重新生成并验证映射，不得复用旧 chunk IDs 或为提高命中率改写 gold。冻结锚点本身失效或映射依据存在歧义 MUST 标记该 snapshot 下的数据/映射 `needs_review`，不得伪装成有效检索失败。若原文及锚点有效，但待测 parser/chunker 丢失证据，映射 MUST 记录 `missing` 及对应的 `system_failure`，保留该题在适用集合和端到端分母中；不得删除题目、自动修正 gold 或改标无答案。

#### Scenario: Map an anchor across chunk boundaries
- **WHEN** 一个原文证据跨越两个实际 chunks，二者合并才能覆盖其必要上下文
- **THEN** 映射 SHALL 记录两者及覆盖关系，并仅在必要范围被完整覆盖时认定该锚点已被覆盖

#### Scenario: Reject stale mappings
- **WHEN** 新索引使用不同 chunker，却提供旧 snapshot 的 gold chunk IDs，或原文锚点本身不能被核验
- **THEN** 系统 MUST 拒绝旧映射或标记数据待审核，不得将无效标注依据直接记为有效的 recall 为零

#### Scenario: Retain system caused evidence loss
- **WHEN** 原文及已审核锚点有效，但待测 parser 未提取必要表格，导致当前索引没有相应 chunk
- **THEN** 系统 MUST 将其记录为可归因的解析/索引系统失败并保留整题分母，不得删题或改标 `unanswerable`

### Requirement: Complete Frozen Corpus Coverage

`corpus_snapshot_id` SHALL 对应完整语料 manifest，记录声明检索范围内全部论文及版本、源文件摘要、完整全文工件摘要、解析完整性和访问状态。索引输入 MUST 包含范围内的非 gold 内容；范围包含干扰论文时也 MUST 保留这些论文，不得只索引参考证据摘录、含 gold 的段落或由答案反推出的文档子集。

语料范围与每题 `scope` SHALL 在参考标注和系统检索结果之外独立确定。正式运行 MUST 使用与 manifest 一致且满足完整性要求的源材料；源文件本身缺失必要正文、附录、表格或图形 SHALL 显式记录并阻止该输入被宣称为完整生产评测语料。源材料完整而待测 parser/chunker 输出遗漏的情形 MUST 保留为系统失败及解析完整性观察，不得以完整性预检为由删题、删失败结果或缩小分母。语料准备及证据映射 SHALL 与逐次运行分离，运行不得根据待测 chunks 重新造题或重建参考答案。

#### Scenario: Retain nongold content during ingestion
- **WHEN** 论文只有少数段落被标为证据，但 scope 包含整篇论文及其已声明附件
- **THEN** 入库 manifest SHALL 覆盖该范围的完整可用内容，索引 SHALL 同时保留未被标为 gold 的段落

#### Scenario: Reject a gold only corpus
- **WHEN** 执行输入仅由 gold snippets 拼接，或先利用参考答案筛出文档再将其称为全库
- **THEN** 系统 MUST 拒绝其完整语料资格；即使检索指标很高，也不得作为正式生产检索效果证据

### Requirement: Paper Family Splits and Untuned Heldout

数据划分 SHALL 先建立 `paper_group_id`，将同一论文的全部问题、版本、修订稿、重复发布及近重复论文归入不可跨 split 的 group，再固定 `dev` 与 `heldout` 成员。`split_manifest` MUST 记录分组依据、分组方法版本、人工争议处理结果和确定性划分参数；随机打散问题不能替代论文族划分。

heldout SHALL 来自未参与当前方案调参的独立论文族，其问题、标注、检索反馈及评分结果 MUST NOT 用于 prompt、检索、reranker、生成器选择或阈值设定。heldout 论文全文 MAY 作为合法 RAG 检索知识进入声明 scope 的索引，这不等于允许使用其题目、标签、派生答案笔记、运行记忆或评分反馈调参；系统 MUST 区分正常语料可见性与上述评测泄漏，不得因全文合法入库就取消或虚构保留资格。heldout 执行 SHALL 在待验配置与阈值冻结后显式启用；若其反馈随后用于调整方案，系统 MUST 追加暴露记录，并使用新的独立 heldout 验收后续方案，不得继续声称旧集合未参与调参。

#### Scenario: Keep related versions in one split
- **WHEN** 两篇文档是同一论文的预印本和修订稿，或经审核属于近重复论文
- **THEN** 系统 SHALL 将两者及其全部问题置于同一 group 和 split，避免版本改名形成跨集合泄漏

#### Scenario: Reject heldout driven tuning
- **WHEN** 团队根据 heldout 失败题修改 prompt 或阈值后，再次用同一集合申请未参与调参的验收资格
- **THEN** 系统 MUST 根据暴露记录拒绝该资格，不得仅以本次 run 使用新名称来恢复独立性

### Requirement: Immutable Frozen Releases and Versioned Corrections

数据集 SHALL 经可审计的 `draft -> annotated -> reviewed -> frozen` 生命周期发布；进入 `frozen` 前 MUST 完成逐题人工终审、契约与引用完整性校验、语料检查及 split 检查，并记录批准者和冻结时间。`dataset_digest` SHALL 覆盖题目、参考标注、来源与暴露史快照、审核、split 和语料身份。冻结后的这些内容 MUST 不可变。

纠错 SHALL 创建带新 `dataset_version` 与新摘要的版本，记录 `supersedes`、改动范围、理由及重新审核结果；旧 run 和 score MUST 继续绑定旧版本。索引映射和后续暴露事件 MAY 作为独立、可追踪的派生或审计工件追加，但 MUST NOT 改写冻结 manifest。历史版本发现问题时 SHALL 明确标注受影响的使用资格，不得静默替换其内容或覆盖历史评分。

#### Scenario: Publish a reviewed correction
- **WHEN** 人工发现已冻结参考答案或原文锚点有误
- **THEN** 系统 SHALL 建立并复核新版本、记录替代关系，同时保留旧版本及其 run/score 的原始可重现绑定

#### Scenario: Reject an in place repair
- **WHEN** 某次执行或审核试图在原 `dataset_version` 下修改问题、答案、split 或源文件摘要
- **THEN** 系统 MUST 拒绝写入；通过修改摘要或覆盖文件使同名版本变动也不得被接受

### Requirement: Reviewed Dataset Delivery Milestones

交付 SHALL 区分候选审计、试点和正式目标：现有 79 题只能作为 `legacy_dev_candidate` 开发/回归候选起点，在逐题审计和人工终审前 MUST NOT 声称已审核，也不得因数量足够而充抵新增独立题目标。约 20 道已审核题的试点 SHALL 只作为契约、标注、映射和执行流程的中间验收，不代表完整交付。

除非通过显式范围调整另行批准审核目标，正式题库目标 SHALL 为 50–100 道新增、独立编写且逐题人工终审的 heldout 保留测试题，并按论文族隔离且未参与调参。约 20 题试点 SHALL 保持 dev 身份，MUST NOT 充抵 heldout 数量；既有 79 题的逐题审计去向 SHALL 单独交付。验收 SHALL 分别列出候选数、终审数、冻结数、dev/heldout 数、待复核数和实际题型覆盖；不得通过凑题、重复改写或强行构造 `unanswerable` 达到数量或类别配额。

#### Scenario: Complete a pilot without claiming final delivery
- **WHEN** 约 20 道题已完成终审并跑通流程，但新增独立题目标与 heldout 审核尚未完成
- **THEN** 验收记录 SHALL 标记为试点完成、正式目标未完成，并明确剩余数量与审核工作

#### Scenario: Reject inflated acceptance counts
- **WHEN** 交付尝试把既有 79 题、未经人工终审的模型生成题、同题改写或人为删掉证据造成的无答案题纳入新增独立 heldout 题计数
- **THEN** 系统 MUST 拒绝相应计数及完整交付声明，并保留候选或待复核身份；真实题库没有某类无答案题时 SHALL 如实报告覆盖缺口而非伪造
