## ADDED Requirements

### Requirement: Frozen evaluation identity and immutable artifact lineage

质量验证 SHALL 在候选结果可见之前冻结评测 manifest，记录原始 run、case 清单及顺序、dataset 与 evidence mapping、corpus、基础及补充 embedding cache 的内容身份，以及 retrieval profile、实际 policy、模型、prompt、候选协议、gate、verifier、judge、指标和阈值版本。manifest MUST 区分评测协议身份、代码身份与实际尝试身份；影响测量的必填身份缺失时不得以当前默认值补齐。源 report、answers、sample 和已有失败工件 MUST 保持不可变；恢复、重判、诊断和候选比较 SHALL 写入独立工件并记录父工件 digest、case 来源及全部尝试归属。

#### Scenario: Missing measurement identity
- **WHEN** 比较输入缺少必填 cache、policy 或 metric 身份，且无法通过明确的旧格式适配验证
- **THEN** 系统 MUST 阻断同口径比较并记录缺失项，不得将两个输入的共同缺失视为身份相等

#### Scenario: Derived evaluation preserves its parents
- **WHEN** 系统执行恢复、重判或候选比较
- **THEN** 系统 MUST 保留原工件字节不变，在新工件中记录父 digest、完整 case 账本和每次尝试的来源、状态及成本

### Requirement: Evaluation against the actual visible evidence

每次生成的 faithfulness 与 citation support 评测 SHALL 使用该次最终模型请求的 actual-visible evidence snapshot，包括真实裁剪后的文本、编号、来源 span 和内容 digest；被选中但未送达模型的正文不得作为支持证据。正确性评测 SHALL 使用冻结 reference 与答案规则，并与仅依据实际可见证据的支持性评测分开。完整源文上的支持情况 MUST 使用独立字段和指标报告，不能替代 actual-visible 支持率。生产 verifier MUST 不接收 gold answer 或 gold evidence mapping。

#### Scenario: Supporting text exists outside the visible snapshot
- **WHEN** 完整源文包含支持某 claim 的段落，但该段落不在生成实际可见的文本中
- **THEN** actual-visible 支持评测 MUST 不将该段落计为支持；系统 SHALL 单独报告 full-source 支持情况

#### Scenario: Correctness and faithfulness disagree
- **WHEN** 回答符合 reference，但实际可见证据不支持其中的事实
- **THEN** 系统 MUST 分别保存正确性和不受支持的结论，不得用 reference 正确性覆盖 faithfulness 或 citation support 结果

### Requirement: Explicit historical metric migration

系统 MUST 保留历史运行的原始定义和结果，区分 micro、macro、评分有效范围及失败排除规则。历史 300 题运行的 citation micro 为 `174/213 = 81.69%`，macro 约为 `82.98%`；二者不得互换或混作同一个基线。新协议 SHALL 冻结 citation 计数单位、重复 marker 处理、claim 绑定、macro 聚合、有效范围及 unknown 处理规则。跨版本比较 MUST 提供同一批冻结答案的同版本桥接结果，或明确标记不可比较并建立独立的新协议基线；不得将协议迁移收益解释为产品质量提升。

#### Scenario: Historical visible evidence cannot be verified
- **WHEN** 历史工件未保存 actual-visible snapshot，且无法验证最终请求文本
- **THEN** 系统 MUST 标记历史可见证据为 unverifiable，不得将 full chunk、summary 或当前重建正文冒称历史实际输入，也不得据此声称已经完成 actual-visible 同口径比较

#### Scenario: Citation unit or judge version changes
- **WHEN** 候选协议、citation 计数单位、判定规则或 judge 版本发生改变
- **THEN** 系统 MUST 分别保留旧指标和新版本桥接指标；只有采用相同版本、相同证据口径和相同样本规则的结果才 SHALL 进入配对收益结论

### Requirement: Calibrated judgments and independent paper family separation

质量验证 SHALL 复用已实现的 judge 恢复、claim/citation 诊断、人工标注 schema、校准集导出及评分资产，补完尚未完成的人工标签与 judge 校准；本能力不得以重建完整通用 evaluation protocol 为前置条件。校准 SHALL 覆盖混绑复合事实、额外推论与数值限定、背景归因、无依据极性、受支持对照及误拒等风险分组。人工标注 MUST 独立于 judge verdict 和候选版本身份，judge MUST 不读取人工标签；分歧 SHALL 经过记录仲裁。用于冻结 verifier、judge 或阈值的样本 MUST 与最终验收按 paper family 隔离，包含同一论文的版本、别名和派生样本不得跨越该边界。

#### Scenario: Calibration is unfinished or fails its frozen criteria
- **WHEN** 人工标签未完成，或 judge/verifier 未满足预先冻结的校准判据
- **THEN** 系统 MUST 保留诊断结果并将正式质量结论标记为 inconclusive，不得将模型自行判断当作可信标签

#### Scenario: A related paper appears in development and holdout
- **WHEN** 拆分发现同一 paper family 的样本同时出现在调参与独立验收集合
- **THEN** 系统 MUST 在验收之前修复拆分并冻结新 manifest，不得将该集合报告为独立 holdout

### Requirement: Complete citation denominator and the micro support target

新协议的正式引用目标 SHALL 为同口径 citation support micro `>= 0.98`。分母 MUST 覆盖冻结计数规则下所有最终输出的 citation 单位，并分别报告 supported、unsupported、invalid、unknown 和 missing judgment 的数量；只有确认 supported 的单位 SHALL 进入支持数分子。invalid、unknown、缺失评分及 judge error 不得被选择性移出分母或计为支持；任何最终输出引用缺少有效判定时，正式引用验收 MUST 不通过。无引用输出的 citation rate SHALL 为 not_applicable，不得记为 `1`，且仍须接受回答可用性与完整性验收。系统 MUST 同时报告 micro、macro、分题型结果及无引用数量。

#### Scenario: Unknown citations would otherwise raise the score
- **WHEN** 一部分最终输出引用因 judge error 或无法匹配可见 span 而未获得有效支持判定
- **THEN** 系统 MUST 将这些单位保留在引用账本和分母中，禁止发布正式通过结论，并分别展示已判定比例及保守支持比例

#### Scenario: All citations are removed
- **WHEN** 候选删除全部引用或仅输出拒答
- **THEN** citation rate MUST 为 not_applicable，且该结果 MUST 不能单独满足 98% 引用目标或使联合质量验收通过

#### Scenario: Historical repair count is used as a target illustration
- **WHEN** 报告以历史固定 213 个引用说明修复规模
- **THEN** 报告 SHALL 说明至少 209 个受支持引用才能达到 98%，并 MUST 明确该计算不能替代引用数量变化后的新运行真实分母

### Requirement: Preregistered joint quality and cost acceptance

正式通过 MUST 同时满足引用目标、固定题集总体正确率改善、固定可答题集上的正确实答产出改善、误拒率下降，以及关键事实完整性非退化。总体正确率与正确实答产出 MUST 使用预先冻结的题集分母，系统错误和未评分结果不得进入成功分子；误拒和系统错误 SHALL 分列。关键事实 rubric 与权重 MUST 在候选结果可见之前冻结，不能通过删除必要事实、增加拒答或只保留易答题达标。验收配置 SHALL 预先给出非零的正确率、正确实答产出及误拒改善幅度、完整性非退化界限、统计判据、最低评分完成条件和调用/token/总成本上限；全部失败、恢复和 repair 尝试的实际成本 MUST 计入。

#### Scenario: Support improves by sacrificing useful answers
- **WHEN** 引用 micro 达到 98%，但正确实答产出未达到冻结改善幅度、误拒未下降或关键事实完整性退化
- **THEN** 系统 MUST 拒绝联合质量通过结论，并分别报告未满足的约束

#### Scenario: A threshold is missing or changed after outcomes
- **WHEN** 验收配置尚未填写任一必要阈值或统计判据，或观察结果后才提出修改
- **THEN** 当前比较 MUST 为 inconclusive；新阈值 SHALL 产生独立协议版本并用于随后冻结的实验，不得追溯应用以使当前结果通过

#### Scenario: Failures or extra attempts exceed the cost ceiling
- **WHEN** 正常调用加上诊断、恢复与有界 repair 的实际成本超出冻结上限，或未评分结果被当作成功
- **THEN** 系统 MUST 拒绝正式通过，并保存全部尝试成本及缺失判定数量

### Requirement: Ordered experiments with frozen offline retrieval replay

优化验证 SHALL 依次经过离线合同与回归验证、小样本 single-factor 配对实验、未用于调参的 paper-family holdout 验收，以及既有完整 300 题回归。进入下一阶段的条件、比较因素、样本、模型配置、预算和重复规则 MUST 预先冻结；失败阶段不得通过扩大 live 运行规模绕过。检索比较 SHALL 复用已有 profiles、固定 cache 和 retrieval-only replay，分别报告候选、最终上下文及 actual-visible 证据覆盖，且保持最终上下文预算受控。gold 仅 SHALL 用于离线评价、开发集选择及冻结 rubric，不得进入运行时检索、重规划、拒答或放行决策。既有 300 题已用于分析与优化，MUST 标记为开发/回归集。

#### Scenario: A candidate has only passed development replay
- **WHEN** 候选通过离线 replay 或已观察过的 300 题，但尚未通过冻结的小样本实验及独立 holdout
- **THEN** 系统 MUST 只报告对应阶段证据，不得宣布独立验收通过或将 300 题改名为 holdout

#### Scenario: Retrieval replay encounters an uncached query
- **WHEN** 固定缓存的 retrieval-only replay 缺少所需查询向量
- **THEN** 系统 MUST 复用已有阻断与诊断行为，不得静默调用 live embedding、切换 profile 或发布部分覆盖作为完整比较

#### Scenario: Holdout results motivate further tuning
- **WHEN** 研发依据独立 holdout 的结果修改 prompt、policy、verifier 或阈值
- **THEN** 该 holdout MUST 转为已观察的开发证据，后续正式独立验收 SHALL 使用另一个预先冻结且未用于调参的 paper-family holdout

### Requirement: Bounded provider failure diagnosis separate from recovery

对历史两次出现于同一批 case 的 45 道生成 502，诊断 SHALL 使用预先冻结的有限尝试预算、乱序失败 case 和匹配的成功 control，区分顺序、请求内容与长度、provider 路由及服务状态的影响；不得以盲目增加 retry 代替诊断。每个 attempt MUST 保存 case、顺序、阶段、模型/配置身份、请求 fingerprint、安全的长度统计、脱敏限长的状态与错误摘要及成本，不得落盘凭据或未筛选的原始请求。诊断工件 MUST 与既有故障恢复及正常质量比较分开；所有成功和失败尝试 SHALL 保留，不得按正确性或成功与否择优拼接。

#### Scenario: A diagnosed case succeeds on one of several attempts
- **WHEN** 同一 case 在有限诊断尝试中产生不同结果
- **THEN** 系统 MUST 保存全部尝试和 control 对照，按预注册规则解释故障，不得仅选成功或较正确的答案替换原运行

#### Scenario: Diagnosis changes request configuration
- **WHEN** 诊断调整模型、prompt、上下文、检索、预算或 provider 路由等影响测量的配置
- **THEN** 系统 MUST 将其标为独立诊断比较，不得冒充既有配置下的失败恢复，原恢复身份与资格校验 SHALL 继续适用

### Requirement: Independent Qdrant transfer validation

cached-fixture 的支持率、覆盖、耗时或正确率结果 MUST 明确限定为 fixture 路径。任何关于真实 Qdrant Research 运行路径的质量结论 SHALL 另行提供 transfer 检查，使用冻结语料、模型与检索配置身份，记录实际索引、过滤、候选 provenance、actual-visible snapshot 和运行错误，并按相同联合指标报告。transfer 检查 SHALL 聚焦本变更必要的运行路径差异，不要求实现无关的完整评测平台；未完成时只 SHALL 将生产泛化结论标为未验证，不得用 fixture 结果替代。

#### Scenario: Fixture passes without a real index check
- **WHEN** cached-fixture 达到联合指标，但真实 Qdrant transfer 检查尚未完成
- **THEN** 系统 SHALL 保留 fixture 范围的验收结果，并 MUST 将真实 Qdrant 质量及生产泛化标为 unverified

#### Scenario: Transfer loses or changes supporting evidence
- **WHEN** transfer 检查发现 Qdrant 的过滤、索引或候选差异导致可见证据、支持率或正确实答产出变化
- **THEN** 系统 MUST 独立报告该差异及其身份，不得将 fixture 指标合并为生产通过结果

### Requirement: Cluster aware uncertainty and auditable conclusions

报告 SHALL 以固定题集的配对结果和预先冻结的统计方法判断改善，区间与重采样 MUST 按 paper 或更大的 paper family 聚类，并披露实际采用的聚类单位、样本量、有效 paper 数、失败及未评分数。不得将同论文的题目或多个引用假设为相互独立以缩窄区间。`>=98%` SHALL 明确表示指定冻结样本、指定协议下的点估计；总体或生产保证 MUST 有独立证据，不得由该点估计直接推导。最终结论 SHALL 区分 passed、failed、inconclusive 与 unverified 范围，并提供可重算的分子、分母、阈值、版本和成本来源。

#### Scenario: Sample support reaches the target with wide uncertainty
- **WHEN** 冻结样本的 citation micro 达到 98%，但聚类区间或独立 transfer 证据不足以支撑总体保证
- **THEN** 系统 MUST 报告样本点估计及区间，只 SHALL 在其实际验收范围内作结论，不得宣称总体支持率已被保证达到 98%

#### Scenario: A comparison excludes failed or ungraded cases
- **WHEN** 报告提供共同有效题上的条件正确率或已判定引用支持率
- **THEN** 系统 MUST 同时提供固定计划题集和全部输出引用的账本、失败与未评分数量，并明确条件指标不能替代正式联合验收
