## ADDED Requirements

### Requirement: Versioned Score Observation Contract

Research 评分层 SHALL 使用共享的 `EvaluationScore` 契约承载规则、语义 judge 和人工复核观察；其范围仅包括评分、评分修订、比较及报告证据，不接管题库制作、RAG 执行、在线 workflow 或发布。评分 SHALL 依次核验冻结计划及输入身份、产生观察、校验并保存观察、核对完整性、汇总比较，最后向既有确定性 gate 提供证据，不建立平行 evaluator 或 run/event store。

每个观察 MUST 包含 `record_type=score`、`schema_version`、`protocol_version`、`metric_id`、`version`、nullable `value`、`status`、`numerator`、`denominator`、`source`、`provenance`、`evidence` 和 `reason`，并关联 `dataset_id`、`dataset_version`、`dataset_digest`、`run_id`、`case_id`、`attempt_id`、`track`、`execution_profile`、`variant_fingerprint`、`repetition`、`scope` 与 `scoring_revision`。`version` SHALL 标识指标语义，即 `metric_version`，不得与 schema/protocol/scoring revision 混用；指标注册信息 MUST 声明输入要求、适用集合、计分单位、聚合方式、方向、取值域和缺失策略。`source` SHALL 区分规则、语义 judge、人工及真实 usage 等来源；`provenance` SHALL 保留实际版本和不可变输入指纹，`evidence` SHALL 指向可复核的原文、上下文、claim、判断或执行工件，而非仅给出不可追踪的总分。

`status` MUST 限于 `ok`、`error`、`not_applicable`、`needs_review`。`ok` 只表示有效测量，不表示质量通过；其 `value` MUST 是指标取值域内的有限数值。其余状态的 `value` MUST 为 `null` 并携带原因；禁止将缺失、异常或不适用值默认为 `0` 或 `1`，或用另一指标补齐。比例指标 SHALL 保存实际分子、分母及计数口径；空分母 SHALL 保留 `denominator=0`、`value=null`，不得产生满分。非比例指标的分子、分母 SHALL 为 `null` 并由指标定义说明，不伪造计数。

通用 `MetricValue` SHALL 只承载有效数值；Research-owned 逐例结果和报告 metadata MUST 保留完整 nullable/status 契约及无数值的观察，不要求 framework 引入论文业务状态。

#### Scenario: 有效比例保留证据和计数
- **WHEN** 某指标在同一冻结范围内确认五项可核验主张中三项得到支持
- **THEN** 评分 SHALL 保存 `status=ok`、`value=0.6`、`numerator=3`、`denominator=5`、版本及逐项证据；是否通过 SHALL 另由已配置 gate 判定。

#### Scenario: 缺失评分不变成有效数值
- **WHEN** judge 超时或观察需要人工裁定
- **THEN** Research metadata SHALL 分别保存 `error` 或 `needs_review`、`value=null` 和原因，不生成值为零或一的 `MetricValue`，也不复用其他指标的值。

#### Scenario: 同名指标的语义版本不同
- **WHEN** 两个结果使用相同 `metric_id` 但不同分母、证据组或缺失处理版本
- **THEN** 报告 SHALL 保留各自版本并拒绝直接混合汇总或排名，除非先对兼容的不可变输出采用同一评分版本重评分。

### Requirement: Gold Group Aware Retrieval and Context Metrics

检索证据指标 SHALL 以已审核 `answerability` 为可答、且具有有效原文证据标注的题为适用集合。`required_evidence_groups` 中不同必要组 MUST 共同满足；每组内部允许多个替代充分证据集合，每个集合内的证据 MUST 共同满足。评分 SHALL 使用冻结原文锚点、相关性标注及绑定 parser/chunker/index 版本的映射，不按本次命中结果重建 gold，不将所有替代证据简单并成分母。

令必要组数量为 `m`，第 `i` 组的替代充分集合为非空 `S_ij`，`covered(X)` 表示输入 `X` 中按版本化锚点规则实际覆盖的原文证据。评分 SHALL 定义 `coverage_i(X) = max_j (|covered(X) ∩ S_ij| / |S_ij|)`；逐题 `evidence_recall_at_k` SHALL 为前 `k` 个检索结果的 `coverage_i` 平均值，再按声明口径对题目聚合。一个组的替代集合不能抵消另一个必要组的缺失；只有全部必要组均完整覆盖才能宣称证据充分。重复 chunk、重叠原文和重复引用 MUST NOT 重复增加同一证据的覆盖。

`hit_at_k` SHALL 判断前 `k` 至少命中一项有效 gold 证据，不能将命中等同于完整充分证据。`mrr` SHALL 按声明截止范围内首次有效证据命中的原始名次计算倒数，无命中为零；去重不得压缩原始排名来美化结果。`ndcg_at_k` SHALL 使用预先冻结的计分单位与相关性 qrels，按原始排名计算 `DCG@k = Σ gain_i/log2(i+1)`，并以相同计分单位、截止位置、gain 和去重规则，从完整相关性集合构造 `IDCG@k`；不得按本次命中或返回条数缩减理想集合。若另行计算组内取最佳替代集合、组间再平均的排名指标，MUST 使用独立 metric ID/version，不得将其冠以标准 `ndcg_at_k` 名称或与之混比。graded relevance SHALL 使用声明的等级与 gain；只有 binary 标注时 MUST 明确报告 binary 口径，不伪称 graded 测量。计分单位、组权重、相关性、去重与 ideal ranking 规则 MUST 属于指标版本；ideal 分母 MUST 来自完整 gold 及冻结理想候选，不能因本次检索或解析漏证据缩小。无法构造有效分母或缺失必需标注时 SHALL 留下显式状态，不生成默认满分。

`context_evidence_coverage` SHALL 用实际送入回答模型、经过选取、压缩及截断后的最终上下文计算相同组覆盖口径，并保存该上下文指纹；它 MUST 与 `retrieved` 阶段指标分开。仅检索到、但未进入实际上下文或已被截掉的证据不得算作上下文覆盖。

冻结锚点无效、来源版本不符或映射过程错误 SHALL 归为 `gold_mapping_error`，受影响评分为 `error` 或 `needs_review`，保留计划位置及诊断证据。原文锚点有效且可核对、但待测解析或切块确实丢失证据时 SHALL 归为 `parser_evidence_loss`：保留该 gold 分母并计入实际覆盖损失及模型路径失败，不改成数据集不适用。归因不明确时 MUST 保留待复核状态；两类问题均不得通过静默删题解决。

#### Scenario: 替代充分证据与共同必要证据分别计分
- **WHEN** 一个必要组允许 `{a,b}` 或 `{c}`，检索仅覆盖 `a`
- **THEN** 该组覆盖 SHALL 为 `1/2`，`hit_at_k` 可以为一但不得宣称证据充分；若仅覆盖 `c`，该组覆盖 SHALL 为一而不是 `1/3`；若另有必要组要求 `d` 且未覆盖，整题证据召回 SHALL 为 `1/2` 而不是一。

#### Scenario: 检索命中没有进入最终上下文
- **WHEN** 唯一充分集合为 `{a,b}`，检索结果包含两项，但实际模型上下文只保留 `a`
- **THEN** `evidence_recall_at_k` SHALL 为一，`context_evidence_coverage` SHALL 为 `1/2`，报告 SHALL 定位为上下文选择或截断损失，而非声称生成器已见到全部证据。

#### Scenario: 重复命中不提高排名收益
- **WHEN** 多个排名位置重复覆盖同一 gold 原文，而其他必要证据没有出现
- **THEN** 评分 SHALL 保留原始位置、只计一次该证据覆盖，按冻结去重规则计算 nDCG，不通过重复 gain 或缩减 ideal 分母产生虚高结果。

#### Scenario: 映射错误与解析漏证据分别归因
- **WHEN** 一题的来源指纹与 gold 锚点不符，而另一题原文锚点有效但 parser 输出遗漏了对应表格
- **THEN** 前者 SHALL 记录 `gold_mapping_error` 并保持评分未知；后者 SHALL 记录 `parser_evidence_loss`、保留原有证据分母并计入已确认的管线失败，两题均保留在报告中。

#### Scenario: 有效无命中与不适用题不混淆
- **WHEN** 已审核可答题在完整执行后未命中任何证据，另一题则为已审核应拒答题
- **THEN** 前者的适用检索指标 SHALL 按定义记零；后者的正例证据指标 SHALL 记录 `not_applicable` 及原因，不能用负例的空 gold 获得检索满分。

### Requirement: Independent Factual Faithfulness and Citation Assessments

评分 SHALL 分别保存事实正确性、事实完整性、`faithfulness`、精确 `citation_support` 与 `citation_coverage`，不得以一个总分、字符串相似度或另一个指标的默认值替代。事实正确性 SHALL 检查回答主张与参考事实及其原文依据的一致性；完整性 SHALL 检查所需事实、条件及证据组是否得到覆盖。`fact_precision/recall/f1` 的主张划分、匹配和分母 MUST 版本化。参考事实之外但可能获得原文支持的额外主张 SHALL 进入复核，而非仅因不在参考答案中自动判错。

`faithfulness` SHALL 只检验可核验回答主张是否得到实际模型上下文支持，不借用未送入模型的 gold 或其他论文补证。引用评分 SHALL 将稳定 `claim_id`、回答跨度与实际引用的精确原文位置绑定，分别核验引用能否解析、是否支持绑定主张及其完整条件、需要引用的主张是否获得充分引用。同论文、同关键词、有效 URL 或正确引用格式 MUST NOT 等同于主张支持；核验不得改用任意检索内容替代被引用位置。

确定性规则 SHALL 仅在可确证范围内判定来源、ID、数值、单位转换及显式条件，记录规范化值、允许误差及规则版本。不同实验条件、对象、统计口径或不可确认的图表语义 MUST NOT 因数值相同而判正确；无法确定的语义 SHALL 转交具备相应能力的 judge 或标为 `needs_review`。LLM judge SHALL 仅产生评分观察，不决定 workflow routing、quality gate、memory write、tool authorization、promotion 或 publication。

#### Scenario: 事实正确但上下文不支持
- **WHEN** 回答事实与已审核参考事实一致，但实际输入上下文未包含支持该事实的证据
- **THEN** 事实正确性 SHALL 与 faithfulness 独立记录，不得因事实正确而自动给 faithfulness 满分。

#### Scenario: 忠实回答仍然可能不完整
- **WHEN** 回答中的主张均由实际上下文支持，但遗漏了问题要求的另一项事实或条件
- **THEN** faithfulness SHALL 按已有主张的支持情况计分，完整性 SHALL 反映遗漏，不将忠实度直接复制为完整性。

#### Scenario: 引用存在但不支持绑定结论
- **WHEN** 引用可解析到同一论文的方法段，但绑定主张声称某测试条件下的实验数值
- **THEN** 引用定位检查可以通过，精确支持检查 SHALL 不因此通过；缺少实际支持时 SHALL 记录不支持或待复核及具体原因。

#### Scenario: 单位换算不能掩盖条件变化
- **WHEN** 规则比较 `0.95` 与 `95%`，且另一个答案在不同样本或实验条件下使用相同数值
- **THEN** 只有语义量纲和显式条件均确认一致时 SHALL 判定前一数值等价；条件不同或不可确认的答案 MUST NOT 仅凭换算得到事实正确结论。

### Requirement: Replaceable and Bounded Semantic Judge Adapters

Research 评分层 SHALL 定义可替换的语义 judge port，由外层 composition 注入实现；首个第三方实现 SHALL 为可选 Ragas adapter，核心不得直接依赖第三方库、interfaces 或 infrastructure。不同实现 SHALL 输出同一评分契约，同名指标只有语义版本一致时才可比较，不能互相填补缺失值。adapter 依赖 SHALL 锁定实际验证的版本，但普通离线契约测试不得要求安装该可选依赖或访问网络。

每次 judge 观察 MUST 记录 provider、`model_id`、可获得的 `model_revision`、adapter/library version、prompt 内容指纹及版本、rubric 指纹及版本、metric version、实际输入 scope/模态、生成参数、原始判断工件及解析状态。供应商未提供的版本信息 SHALL 显式为 `null` 并记录可复现性限制，不伪造固定修订。输入 SHALL 按指标隔离：事实看参考事实与回答，faithfulness 看实际上下文与回答，引用支持看精确被引原文与绑定主张。

judge 调用 MUST 遵守已声明 timeout、request/token/cost 和重试预算，通过既有 Harness 控制执行并记录每次尝试。依赖缺失、凭据缺失、能力不足、超时、限流或无效 JSON SHALL 产生结构化错误，包含阶段、原因、尝试、可重试性及预算结果；禁止无限重试、静默切换为假评分或将失败视为模型回答正确或错误。必需 judge 不可用时 SHALL 记录 `status=error`、`value=null`、`reason=judge_unavailable` 或更具体原因，报告标明 `unavailable`，正式 decision 为 `inconclusive`。

adapter SHALL 声明支持的模态。只有可验证的文本表示或具备声明能力的多模态 judge 才能核验相应证据；仅能评文本时 SHALL 使用显式 `text_subset` scope，不声称完成图像、公式或表格全题验证。运行后发现能力不足 MUST NOT 通过缩小 scope 消除原计划必需项。纯离线测试 SHALL 允许标记为 `fixture` 的 mock judge；`production` MUST 使用真实、获准的评分路径，不将 mock 或固定输出冒充生产 judge。

#### Scenario: 可选 adapter 缺失不破坏离线合同测试
- **WHEN** 环境没有安装 Ragas，而离线测试显式注入 mock judge
- **THEN** 测试 SHALL 能验证共享合同并标记为 `fixture`；若生产计划要求 Ragas，则该项 SHALL 报 `judge_unavailable` 而不是沿用 mock 分数。

#### Scenario: 无效输出在预算内终止
- **WHEN** judge 连续返回不符合 schema 的 JSON 且重试预算已经耗尽
- **THEN** 系统 SHALL 停止新请求，保存原始输出、各次费用及结构化错误，将必需评分留为未知，正式 decision 为 `inconclusive`。

#### Scenario: 文本 judge 无法核验图像证据
- **WHEN** 原计划要求核验图像中的实验结论，而 adapter 只支持文本
- **THEN** 该必需项 SHALL 明确报告能力 `unavailable`；另行声明的文本子集结果 SHALL 标记 `text_subset`，不得填充整题或多模态指标。

### Requirement: Reviewed Answerability and Abstention Accounting

拒答评分 SHALL 使用冻结且已经人工审核的 `answerability`。`positive` SHALL 表示应能回答的题，`negative` SHALL 表示应拒答的题；不得根据待测系统是否找到证据、是否返回空答案或 judge 是否可用反推标签。未经审核的可答性 MUST 记录为待复核并阻止相应正式资格判断，不静默归入负例或删除。

`abstention_recall` 的分子 SHALL 为正确拒答的 `negative` 题数，分母 SHALL 为全部适用且已审核的 `negative` 题数；`false_abstention_rate` 的分子 SHALL 为发生误拒答的 `positive` 题数，分母 SHALL 为全部适用且已审核的 `positive` 题数。拒答识别、部分回答、夹带无依据结论等边界 MUST 由版本化 rubric 规定。空输出和 runtime error MUST NOT 自动算作正确拒答；输出失败 SHALL 另行记录为模型路径失败。分母为零时 SHALL 为 `not_applicable`；存在未裁定评分时 SHALL 保留计划分母及未知数，不报告貌似完整的比例。

空答案、无可核验主张、NA 或没有引用 MUST NOT 获得真空成立的 faithfulness/citation 满分。没有可核验主张时 SHALL 记录相应 `not_applicable` 及原因，拒答是否正确另行计分；有应引用的事实主张但没有引用时，`citation_coverage` SHALL 为零，不能标为 NA。无引用答案的 faithfulness SHALL 仍独立检查实际上下文支持，不因缺少引用自动设为零或一。

#### Scenario: 拒答指标使用不同分母
- **WHEN** 一次完整评分包含八道已审核负例，其中三道正确拒答，以及十二道已审核正例，其中两道误拒答
- **THEN** `abstention_recall` SHALL 保存 `3/8`，`false_abstention_rate` SHALL 保存 `2/12`，不得共同除以全部二十题。

#### Scenario: 正确拒答不产生引用满分
- **WHEN** 已审核负例得到符合 rubric 的拒答，且答案没有事实主张或引用
- **THEN** 拒答指标 SHALL 记录成功；无适用主张的 faithfulness 和 citation 指标 SHALL 保持 `not_applicable`、`value=null`，不记一。

#### Scenario: 空答案与无引用事实答案分别处理
- **WHEN** 一个负例返回空内容，另一个正例返回有事实主张但无引用的答案
- **THEN** 前者 MUST NOT 算正确拒答；后者的引用覆盖 SHALL 为零，faithfulness SHALL 依据实际上下文独立评分，两者均不得因空集合获得自动满分。

### Requirement: Complete Denominators and Distinct Error Accounting

评分计划 SHALL 在运行前固定实验单元 `(dataset_version, case_id, track, variant_fingerprint, repetition)`、指标版本、适用性和 scope。报告 MUST 为每个计划槽位保留观察或显式错误，汇总 `planned`、`applicable`、`valid`、`error`、`not_applicable` 和 `pending_review`；其中 `pending_review` SHALL 对应 `needs_review`。计数 MUST 满足 `planned = valid + error + not_applicable + pending_review`、`applicable = planned - not_applicable`。有效覆盖 SHALL 明确为 `valid/applicable`，空适用集合为 null；未能确认适用性的项不得事后划为 NA 以缩小分母。

`end_to_end_success_rate` SHALL 在全部适用实验单元上保留 `known_success`、`known_failure`、`unknown` 及其全集分母，满足三者之和等于分母。runtime timeout、解析丢证据、检索/生成失败等已确认模型路径失败 MUST 留在分母并计入 `known_failure`，不能只报告成功请求。已确认 runtime 终止时，下游没有可评分答案的组件 SHALL 保存真实错误状态；端到端失败可以由执行证据确定，不需要伪造组件语义分数，也不得把该已知失败重新包装成 judge 未知。

模型路径已执行、但必需 judge 超时、不可用或输出不合约时 SHALL 单独计入评分错误及 `unknown`，不能默算模型答错、答对或正确拒答。未裁定未知项存在时，正式 decision MUST 为 `inconclusive`；报告 SHALL 同时展示已知成功、已知失败、未知、各类错误及 completeness。允许显示 `known_success / applicable` 的已核验成功率下界，但 MUST 明确标为下界，不能冒充完整成功率或用于绕过未知项的正式门禁。仅有效样本均值 SHALL 标记为条件性诊断，并同时展示完整计划分母。

#### Scenario: runtime 失败仍在端到端分母
- **WHEN** 十个适用实验单元中七个满足全部要求、两个正常执行但质量失败、一个生成超时
- **THEN** 报告 SHALL 保存 `known_success=7`、`known_failure=3`、`unknown=0`、分母十，完整成功率为 `7/10`，不得删除超时项后报告 `7/9`。

#### Scenario: judge 错误不是模型错误
- **WHEN** 十个适用实验单元中七个已知成功、两个已知失败，一个只有 judge 超时
- **THEN** 报告 SHALL 保存 `known_success=7`、`known_failure=2`、`unknown=1`、分母十，正式 decision 为 `inconclusive`；`7/10` 只能标为已核验成功率下界，不得把未知项归入已知失败或成功。

#### Scenario: 缺失结果不能从完整性检查消失
- **WHEN** 计划包含十个评分槽位，其中三项按计划不适用、四项有效、两项错误、一项待复核
- **THEN** 报告 SHALL 展示 `planned=10`、`applicable=7`、`valid=4`、`error=2`、`not_applicable=3`、`pending_review=1`、覆盖 `4/7`；若槽位没有工件，则 SHALL 先生成缺失记录，不得以未返回结果为由缩减计划数。

### Requirement: Auditable Rules and Independent Human Calibration

规则配置、judge 结构化输出与人工审核导入 SHALL 通过版本化 JSON schema 及跨字段校验，包括枚举、数值范围、nullable 状态一致性、分子分母、证据引用、case/run/score revision 身份。无效 JSON、过期审核目标、未知 ID 或不一致计数 MUST 被拒绝并形成可定位错误，不得自动补为有效评分。每条确定性结论 SHALL 保存 `rule_id`、`rule_version`、输入引用、必要的规范化过程、判断结果和可复核理由；仅有无来源的布尔值或总分不构成完整证据。

人工校准 MUST 从非 held-out、独立人工判分集开始，先形成不依赖 judge 建议的人工标签，再用于检查或校准规则、prompt 和 rubric。校准计划 SHALL 区分调校样本与冻结后验证样本，记录题目/论文来源、抽样方法、样本数和审核版本；held-out MUST NOT 用于调校 judge、阈值或选评分版本。审核 SHALL 盲化方案、模型版本及 judge 结论，并保留可核对的原文、实际上下文、回答和评分目标。

随机样本与定向冲突样本 SHALL 分别记录选择方式、样本数、状态及 agreement，不得只公布冲突样本的 agreement 并外推为总体一致性。单人审核 SHALL 明确标记 `single-review`，可以报告 judge 与该审核者的一致性，但 MUST NOT 伪造 `inter_annotator_agreement`；采用多人独立审核时 SHALL 保存独立标签、分歧和裁决记录。

人工判分与 gold 维护 MUST 分离。评分审核者可以读取被测回答进行判分，但 gold 修订流程 MUST NOT 把生产被测回答交给标注者用于修改参考事实或可答性。发现疑似 gold 错误时 SHALL 提交独立原文复核；确认纠错 SHALL 产生新的 annotation/dataset version，评分修订 SHALL 追加保存，并使比较方案对齐同一修订，不原地覆盖原始输出或旧 gold。

#### Scenario: 不合约评分被拒绝而不是修正为通过
- **WHEN** judge 返回 `status=error` 却附带有效分数，或人工导入引用了不存在的 score revision
- **THEN** 校验 SHALL 拒绝该输入并记录具体字段和目标错误，不自动保留其分数或把审核并入其他运行。

#### Scenario: 随机样本与冲突样本分别报告
- **WHEN** 校准集合同时包含随机抽取的普通样本和定向选出的高分歧样本
- **THEN** 报告 SHALL 分别给出两组的抽样方式、数量和 agreement；总体结论 SHALL 依据具有代表性的独立样本，不能以冲突组结果冒充总体准确率。

#### Scenario: 单人盲审不伪装双人一致性
- **WHEN** 只有一名审核者在不知道方案版本和 judge 结果的情况下完成判分
- **THEN** 结果 SHALL 标为 `single-review`，保存审核者与 rubric 版本，`inter_annotator_agreement` SHALL 保持不可计算，而不是复制 judge-human agreement。

#### Scenario: gold 纠错不迎合被测回答
- **WHEN** 生产评测暴露疑似参考事实错误
- **THEN** gold 复核 SHALL 基于独立原文证据、隔离生产被测回答；确认修订后 SHALL 创建新版本并保留旧版本和旧分数，不在本轮原地改 gold 以提高成绩。

### Requirement: Immutable Score Only Replay and Scoring Revisions

`score-only` SHALL 消费已有 run 的不可变问题、检索排序、实际上下文、回答/引用、来源及执行工件，不重新调用 parser、embedding、retriever、reranker 或回答模型。重评分 SHALL 在同一题目与执行输入兼容的前提下允许更新评分器、rubric 或经过审核的 gold 修订；需要改变问题或实际模型输入时 MUST NOT 将其冒充原 run 的重评分。新评分 SHALL 追加 `scoring_revision`、父修订、变更原因和实际配置，历史输出与历史评分不得覆盖。

缓存键 MUST 至少包含题目及 dataset/gold 指纹、来源和映射版本、排序与实际上下文指纹、输出/引用指纹、track/profile/scope、metric/rule 版本、judge/provider/model 配置、prompt/rubric 指纹及版本；有影响的生成参数和人工裁决修订 SHALL 一并纳入。机器绝对路径不得替代内容身份。任何评分输入变化 SHALL 使相应缓存失效；fixture 与 production 不得共享冒充真实观察的缓存。原始工件缺失或哈希不符时 SHALL 明确失败，不能自动重新生成答案来补齐。

重评分中获准的新 judge 调用 SHALL 继续受预算约束并另记 judge 成本；缓存命中 SHALL 保留原观察和付费调用 provenance，不重复计费，也不作为新的独立 judge 观察。错误缓存 MUST NOT 作为成功分数返回。

#### Scenario: rubric 修订只重评分
- **WHEN** 相同不可变运行输出使用新的 rubric 版本重新评分
- **THEN** 系统 SHALL 创建新 scoring revision，保持旧输出和旧评分不变，相关缓存失效；只执行所需规则或获准 judge，不重复 RAG。

#### Scenario: 相同答案但上下文不同
- **WHEN** 两个已有运行返回相同答案，但实际上下文或其顺序不同
- **THEN** 其评分缓存键 SHALL 不同，faithfulness 不得仅按答案文本复用。

#### Scenario: 输出被改动不能自动补跑
- **WHEN** score-only 校验发现回答工件与记录的哈希不符
- **THEN** 该评分 SHALL 返回输入完整性错误并保留原 run 身份，不自动调用回答模型重新生成，也不覆盖原工件。

### Requirement: Comparable Sliced and Paired Statistical Reporting

JSON 与 Markdown 报告 SHALL 从同一评分工件投影，保留 protocol/metric/scoring 版本、dataset/split、track/profile/scope、配置、完整性及逐例理由。报告 SHALL 提供总体及题型、answerability、证据模态、必要证据复杂度、论文和失败阶段切片；每个切片 MUST 展示计划、适用、有效、错误、NA、待复核、独立题目/论文数及聚合口径，未知切片标注不得被静默删除。macro 与 micro、题内与题间聚合 SHALL 明确区分。

多方案比较 MUST 核验同一冻结题集、来源、split、track/profile、评分版本、scope，以及生成器/prompt、最终上下文预算、cache/memory 状态、并发和计时边界。只有预声明 `allowed_differences` 才能作为研究变量不同，例如使用不同 index 但映射同一原文证据；其他不匹配 SHALL 标为 `incomparable`，不输出胜负。oracle 诊断轨和 fixture 不得参加 production 端到端方案排名。

比较 SHALL 对同一 `case_id` 成对计算差异，先按预声明策略在题内聚合重复，再进行题间比较。重复回答、重复缓存命中和相同模型输出 MUST NOT 作为新增独立题。已确认 runtime 失败 SHALL 保留为相应端到端失败观察；judge 未知或缺失配对 SHALL 保留在全量配对计划、覆盖及错误计数中，不能仅保留双方成功样本后声称完整优劣。

不确定性 SHALL 使用论文级 `cluster bootstrap` 或预声明、同样处理论文内相关性的方法；成对比较 MUST 对两方案同步重采样论文组，保留组内问题及重复结构，不能假设同一论文的问答独立。方法、置信水平、随机种子、重采样设置、最低独立论文/题目要求 SHALL 在观察结果前声明。报告 SHALL 给出效果差异、区间、样本数和逐题改进/退化；独立样本不足或方法不支持有效区间时 SHALL 标记 `insufficient_evidence`，只给描述性结果，不排名或声称统计优越。

#### Scenario: 同一论文的多题按一个相关组处理
- **WHEN** 一篇论文包含五道题且每题运行多次
- **THEN** 不确定性估计 SHALL 保留这一论文组内的所有题和重复结构，不把它们当成多篇独立论文或新增独立题目。

#### Scenario: 单题反复输出不能制造大样本
- **WHEN** 一个题目重复得到一百次相同回答
- **THEN** 报告 SHALL 记录一百次实际观察和相应成本，但独立题目数仍为一；不满足预声明样本要求时 SHALL 标为 `insufficient_evidence` 并禁止排名。

#### Scenario: 成对缺失保持可见
- **WHEN** 某题在方案 A 得到有效评分，在方案 B 发生 judge 错误
- **THEN** 该题 SHALL 留在配对计划并标记未知，不进入完整有效差值；报告 SHALL 显示缺失配对和覆盖，不能仅凭剩余配对给出完整正式结论。

#### Scenario: oracle 结果不冒充生产提升
- **WHEN** oracle_generation 的结果高于 end_to_end，但两者上下文来源不同
- **THEN** 报告 SHALL 展示条件诊断差异，不将其解释为同一生产评测下的方案胜负。

### Requirement: Stage Separated Cost and Latency Accounting

评测 SHALL 分开记录 `index`、`request`、`judge` 三类成本及延迟。`index` SHALL 包括解析、embedding、建库及刷新；`request` SHALL 包括请求时检索、reranker、生成及相关实际调用；`judge` SHALL 包括语义评分调用。共享索引一次性成本、请求边际成本和显式摊销成本 MUST 分别标识，摊销依据 SHALL 可追踪，不得将建库费用隐藏在 warm 请求之外后声称总成本更低。

每个实际 provider attempt SHALL 关联调用身份和原始 usage 工件，保留 provider 返回的 input/output/cache/reasoning/total token 等真实字段及其计数关系，不重复累计已包含的 token。未返回字段 MUST 为 `null`，不得补零；本地 tokenizer 推算 SHALL 另标估算来源，不冒充 provider usage。金额 SHALL 区分 provider 实报与估算；估算 MUST 包含价表标识、版本、生效/计价时点、币种、计价单位及计算依据，不写死无出处价格。用量或价格不完整时 SHALL 保留未知费用和完整性，已知小计不得冒充完整总额。

失败、超时、重试及恢复中已产生或可能产生的调用费用 MUST 保留，不能因没有可用答案而删除；无法确认实际 usage 或收费时 SHALL 标记未知而不是免费。缓存重放不得重复计入历史付费调用，新 judge 调用 SHALL 只新增 judge 账目。

计时 SHALL 声明 index/request/judge 边界，记录 `cold`、`warm` 或 `unknown`、缓存层与命中策略、索引复用、实际并发、批大小、重试和 timeout。P50/P95 SHALL 携带样本数和成功/失败范围；超时 SHALL 作为失败及适用的 SLO 违约单列，不能只用幸存成功请求宣称整体低延迟。

#### Scenario: provider 未返回 usage
- **WHEN** 调用返回回答但没有 token usage
- **THEN** 原始 token 字段 SHALL 保持 `null`；任何估算 SHALL 单独标记来源，报告不得将调用记为零 token 或零成本。

#### Scenario: 失败重试仍然计费
- **WHEN** 首次 judge 调用发生输出解析错误但 provider 已返回计费用量，第二次重试成功
- **THEN** 两次实际调用的 usage、价格依据与耗时 SHALL 均记入 judge 账目，只将成功观察用于有效评分，不丢弃第一次成本。

#### Scenario: 冷启动与复用索引不能混报
- **WHEN** 方案 A 包含冷启动建库，方案 B 复用已存在的 warm 索引
- **THEN** 报告 SHALL 分别展示 index/request/judge 账目、复用及并发条件；未经预声明的不同计费或计时范围 SHALL 阻止直接成本/延迟胜负比较。

### Requirement: Directional Thresholds and Deterministic Completeness Gates

正式质量 `decision` MUST 限于 `pass`、`fail`、`inconclusive`、`not_applicable`，并与执行状态、指标 `status` 和报告 completeness 分离。确定性 gate SHALL 核验计划槽位无遗漏、数据和评分身份一致、审核与模态 scope 满足要求、分母及配对完整性可复核，再应用已批准的阈值。runtime 失败 SHALL 按其真实模型路径失败参与适用门禁；必需 judge 失败或其他未知判定证据 MUST 导致 `inconclusive`，不得用下界、空值替代或缩减范围绕过。

阈值配置 MUST 绑定 metric/version、适用 track/profile/scope、聚合统计量、方向和审核版本，支持 `min`、`max`、`delta_min`、`delta_max` 以及显式 `min_coverage` 和最低独立样本要求。`min` SHALL 检查值不低于下界，`max` SHALL 检查值不高于上界；delta SHALL 明确使用 `candidate - baseline`、baseline/scoring revision 及方向，以 `delta_min` 或 `delta_max` 检查声明边界。使用点估计还是置信界 MUST 预先固定，不能看结果后更换。覆盖的分子、分母及目标 scope MUST 明确；最低覆盖不能豁免未记录槽位或必需评分未知。

阈值及统计资格 SHALL 来自开发集基线和人工批准的版本化计划，在 held-out 验收前冻结；本能力 MUST NOT 预设行业通用质量阈值、照搬 fixture 阈值或以 `none` 代表通过。必需阈值或覆盖/样本要求未配置时 SHALL 给出 `inconclusive` 及缺项原因。真正不适用的判定 SHALL 为 `not_applicable`，不等同于 pass；完整、适用且满足全部已批准条件才 SHALL 为 `pass`，完整且违反适用条件 SHALL 为 `fail`。

评分和 gate 结果 SHALL 仅作为既有报告及发布审批流程的证据，不直接发布模型、策略、论文结果或 skill；LLM judge 和人工评分导入不得获得 routing、promotion 或 active skill 修改权。本能力不重新定义既有 legacy/retired scalar 诊断入口，其兼容语义由对应既有 capability delta 承担。

#### Scenario: 不同方向的指标采用不同边界
- **WHEN** 已批准配置要求事实正确性满足 `min`、成本满足 `max`，并比较候选与 baseline 的误拒答率变化
- **THEN** gate SHALL 分别采用相应方向及声明的 delta 边界，保留比较证据，不对所有指标统一使用越大越好的规则。

#### Scenario: 没有阈值不是通过
- **WHEN** 指标已有效测量，但业务阈值为 `none` 或未声明必需 `min_coverage`
- **THEN** 正式 decision SHALL 为 `inconclusive` 并列出缺少的配置，不因有数值、fixture 通过或空阈值集合而返回 `pass`。

#### Scenario: 高分不能掩盖必需 judge 未知
- **WHEN** 已有效评分的题分数很高，但仍有必需 judge 错误或计划项没有结果
- **THEN** gate SHALL 保留全部槽位及未知数，正式 decision 为 `inconclusive`，不通过只统计有效分数、降低覆盖要求或重标 NA 获取通过。

#### Scenario: 完整证据下区分失败与不适用
- **WHEN** 完整且适用的评分违反已批准阈值，而另一个判定按冻结计划确实不适用
- **THEN** 前者 SHALL 为 `fail`，后者 SHALL 为 `not_applicable`；两者均不得被展示为 `pass`，也不触发自动发布。
