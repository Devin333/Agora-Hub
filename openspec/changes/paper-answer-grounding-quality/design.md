## Context

本方案基于 `.tmp/qasper-answer-full-20261025070501`，直接对照 `.tmp/qasper-answer-full-20261025050648`。两轮代码与配置相同，重复运行只揭示波动，不是优化收益。当前 300 题已被用于调查和选方案，只能作为开发与回归集。

| 已确认现象 | 优化含义 |
|---|---|
| 引用 micro 174/213=81.69%；macro 107.05/129=82.98% | 两种聚合必须分开。固定 213 个引用需至少修复 35 个错误才能到 209/213；新答案分母会变化，不能把该算术当成改善预测。 |
| 去 45 道生成 502 后旧口径 125/233=53.65% | 125 包括 73 个正确实答和 52 个正确拒答；另排除 22 个 judge 错误。不能将它当成纯回答正确率或新口径基线。 |
| 正常生成且所有参考均可答的 172 题中误拒 51 题 | 必须区分未召回、预算裁剪、模型未使用可见证据；51/172 与包括系统失败的 51/209 是不同口径。 |
| 39 个错误引用分布于 31 个答案 | 主表现分组为复合事实混绑 19、限定/数值/推论 8、主体或背景归因错 6、无依据极性 6；并非单纯引用格式问题。 |
| 两轮同样的 45 个 case 生成 provider unavailable | 错误与 case、请求内容或顺序可能混杂，需要乱序重放及成功对照；尚不能归因于随机服务波动。 |

代码证据集中在以下边界：

- `framework/harness/rag/kernel_evidence_adapter.py` 将正文放入 `summary=evidence.text[:1200]`；Research 的 `paper_chunk_adapter.py` 没有另外保留可供回建的完整正文，`answer_worker.py` 的 fallback 再以 summary 构造内容。
- `framework/harness/rag/context_pack_assembler.py` 将 token 估计封顶记账，不能证明最终请求真的在预算内；回答生成器还存在后续独立的字符预算和选句。
- `answer_worker.py` 将整答构造成一个保守 claim，并挂载全部引用证据/span；`answer_gate.py` 主要检查字段和归属。合法 ID 不支持事实时，现有 supplemental 路径通常无法获得真实支持缺口。
- `qasper_answer_evaluation.py` 从原始完整 chunk 渲染 judge 引用，不能证明生成模型实际看到相同内容。部分历史 prompt 可精确重建，但未保存的实际输入不能追认成原始快照。

复用已完成的 observability、retrieval-recall、faithfulness calibration 工具。`qasper-end-to-end-answer-evaluation` 的人工校准 4.8 尚未完成；`research-rag-evaluation-protocol` 的大部分任务也尚未完成，本方案只桥接已有可用的合同与评分资产，不以其全量实现为前置工程。

## Goals / Non-Goals

**Goals:**

- 从完整来源到最终可见片段建立可核对的内容与 span 身份，消除无意的正文前缀丢失，并实际执行请求预算。
- 将每个可独立核验的事实与其具体可见依据绑定，保留限定、数值角色、主体、极性及问题要求的关键内容。
- 在既有 Harness 状态机内完成验证、受控修复和持久化；生成器和 verifier 均不拥有通过、路由或发布权限。
- 以独立论文族上的引用支持 micro 至少 98% 为硬目标，同时提高固定样本正确实答产出、降低误拒并保持关键事实覆盖。
- 每阶段有单独身份、配对对照、可重算工件及停止条件；将生成质量与 provider/judge 可用性分别计量。

**Non-Goals:**

- 本次规划不实现产品代码、不调用付费模型、不启动新 300 题运行或生产索引写入。
- 不改历史结果、官方 Answer-F1 或原始 gold；盲审修订作为独立且冻结的审计层，同时保留官方评分。
- 不训练模型、下载新的 reranker、重建评测平台或让普通业务运行修改 active skills。
- 不把 fixture 检索改善直接等同于 Qdrant 生产收益，也不承诺当前证据无法支持的准确率或工期。

## Decisions

### D1. 先冻结测量协议和独立数据划分

建立版本化 `quality_policy` 与实验 manifest，保存数据/映射/缓存/样本及 paper-family 划分、代码身份、prompt/schema、生成器/verifier/judge、检索 profile、所有预算和允许差异。开发、verifier 校准、未调参验收的论文族互斥；原 300 题和其同论文族不能再次作为独立验收。已有导出的 50 题可用于校准资产的起点，但不能自动视为独立 held-out。

每个质量指标声明计算单位、分子分母、NA/unknown/error、题型及 answerability 切片。保留旧 micro/macro 和旧条件准确率作为历史诊断；新增真实可见证据口径时提升 metric/request 版本，形成桥接报告。缺少实际输入的旧 case 标记不可验证；新基线和候选采用同一冻结评分协议，不将源文重建默认为真实可见快照。

主要输出包括固定题集的 confirmed-correct yield、可答题 correct-answer yield、已回答条件正确率、正确拒答、误拒、关键事实完整性、引用 micro/macro/整答全支持比例，以及生成和 judge 故障。固定题集统计保留全部计划 case，故障和未评分不贡献成功；条件指标另报，避免因排除困难题而虚增成绩。

`citation_support_micro_visible` 对最终答案的全部去重引用编号逐一判定，该编号必须支持答案归给它的全部事实；额外记录每个 claim-binding 的支持与引用覆盖，不能用重复引用放大分母，也不能通过删 marker 掩盖无引用事实。无引用为 NA，结构非法、矛盾、证据不足与未知均不贡献支持；正式结论要求全部应评项完成判定。`citation_support_micro_source` 使用完整来源，仅作溯源对照，不替代 visible 指标。最终阈值采用同一新口径比较，不宣称旧 81.69% 已与新分母天然可比。

除固定的 98% 外，准确率最小改善、正确实答产出最小改善、误拒最小下降、关键事实非退化容限、校准误差上限、最小验收论文/引用数、配对统计规则、故障及成本上限在阶段 0 按基线和样本量冻结。改善幅度必须为正，不能以零变化满足优化；未填字段使资格判定为 `inconclusive`，不能在看到候选结果后补有利阈值。

### D2. 在最终模型请求处建立可见证据真值

通用 evidence 合同分别承载 `body` 与 `summary`，正文不借用摘要字段往返。完整正文保持原始来源、版本/hash 和坐标，Research 类型留在 Research；Framework 不能依赖 PaperChunk。过大的正文可以使用受校验的不可变 artifact 引用，生成前由已有应用边界解析，不能静默回退到 summary。

预算裁剪产生新的 passage view，不修改原 evidence 或 provenance。按现有检索分数和问题相关片段进行确定性选择，保留不连续片段到原文的 offset map；不固定只取前 1200 字。内容 hash 和重叠 span 可用于合并重复片段，但保留来源 alias，防止把同字面、不同版本或不同角色的证据错误合并。

最终 provider 请求序列化前，记录每次尝试的 `VisibleContextSnapshot`：context 编号、evidence/source/version、实际 passage text、字符范围/offset map、hash、渲染版本、预算及裁剪原因。生成器、结构 gate、语义观测和引用支持评测共用该快照。若 provider adapter 还会改变消息，快照/校验必须在改变之后完成。

按实际最终消息执行 item、字符及 token 预算：system/user/schema/问题/上下文/保留输出空间全部参与。使用与 provider 匹配的 tokenizer 或有明示上界的保守估算，不得 `min(estimate, limit)` 伪装未超限；无法证明在限额内时明确裁剪或拒绝。先保持当前配置上限，修复真实执行；扩大预算必须作为单独变量重新比较。

替代方案“直接把 summary 长度调大”无法消除后续预算错位、重复内容及 judge 可见范围问题，因此不采用。

### D3. 原子 claim 候选和确定性答案渲染

在生成器内部引入版本化候选：稳定 `claim_id`、事实文本、问题回答部分、引用 binding，以及引用的 snapshot/context、精确 quote 与可见坐标。日期、数字/单位/对象角色、范围条件、比较对象和极性在能够可靠表示时用 typed slots 承载；类型无法确认必须标为未知，不能猜值。

每个 claim 是保留必要限定的最小可核验事实，不进行只为通过校验的无意义拆词。混合数据集与指标的句子拆成各自有依据的事实；否定句不能因去掉限定变成肯定句。跨来源比较等需要多项前提的结论，保留显式前提和依据；可计算关系由受支持的确定性规则验证，其他联合推论保留为语义待核验，不能假设每个来源单独支持整项结论。

gate 检查 claim、binding、quote、offset、source version 和可见性，以及最终回答是否只来自已验证 claims。确定性 renderer 输出对外文本和邻接引用，并记录每个 claim 的最终回答跨度；模型不能在 renderer 之后再补自由文本。禁止用整答单 claim 加全量 evidence/span 自动填充使未知支持通过；“没有逐事实结构”的旧 worker 在启用严格 policy 后明确失败。

对外 `answer` 与引用字段保持兼容，内部候选/schema/prompt 使用新身份。该决策明确扩展旧 observability 变更“不新增复杂 schema”的范围；旧基线 profile 留作明确的实验对照，不作为严格路径静默 fallback。

### D4. 语义观测不拥有通过权限

优先确定性验证可验证的规则：引用范围和 quote 完全一致、数值与单位及所指对象一致、显式 typed 极性和条件不冲突、所要求回答部分未被直接丢弃。quote 存在、ID 合法或文字相似均不能证明自然语言蕴含；typed slots 的提取本身也须保留依据和未知状态。

独立 verifier 只在实际可见依据上产出版本化 observation：候选/来源事实投影、对应 quote、支持/矛盾/不足/不确定及原因。它不接触 gold、不返回可执行路由、不读取生成器自报支持度。先以 shadow 方式校准，复用人工标注工具，进行隐藏模型与 judge 判定的独立双人标注和争议仲裁；评测 judge 与生产 verifier 的配置、用途及校准集分开。

Harness 的确定性 gate 验证观察资格、冻结 rule/policy、字段与证据身份、允许事实类型和一致性，再形成 gate result。未校准配置、缺少可核对依据、结构/typed 检查失败、观测冲突或 unknown 不能因 verifier 的单个 `supported`/置信度值升级为通过；校准仅允许预先声明的事实类型和观察组合参与，不能把校准当作逐题正确的保证。未达联合验收的 verifier 保持 shadow，不启用严格路径推广。

语义错误反馈包括限定、主体、数值角色、背景归因和极性，作用于下一候选的修复约束。所有 claims 必须满足适用 policy，不能通过仅保留容易支持的非核心片段发布不完整答案。核心事实覆盖由独立离线评价检查，运行时不得读取参考答案。

### D5. 在现有 Harness 内有界修复

复用 `PLAN -> EXECUTE -> VERIFY`、generation attempt 与 supplemental round，不新增旁路循环。初始仍保持生产最多两次生成；本地修复和补检索竞争同一剩余 attempt/replan/费用预算，而不是各自再获得两次。

由确定性 policy 按失败类型选动作：可见依据充分但绑定/表述有误时，带失败 claim 与已验证依据重新生成；确有证据缺口时，按问题和缺口构造受控补检索；provider failure 按 typed 错误和 provider 预算处理，不伪装成证据不足。不得用 gold 答案或 gold evidence 构造运行时补检索查询。

每次 phase transition、candidate、snapshot、verifier observation、gate result、动作与剩余预算进入现有 durable transcript/event storage，包括失败和耗尽路径。离线 replay 只重放确定性决定，不调用模型或检索。发生语义支持错误且 ID 合法也能产生明确 failed claim，不再只把缺 evidence id 当成 unsupported。

### D6. 在修好可见性后优化检索与误拒

复用 `qasper-answer-retrieval-recall` 的 profiles、field index、lightweight reranker 和 retrieval-only replay。先校验全部缓存及受控补检索可能需要的 query vectors；缺缓存阻断比较，不静默降级或混入 live embedding。

按未召回、候选池有但最终未选、已选但预算不可见、可见却答错/拒答四个阶段分桶。分别报告候选池、最终 context 与实际 passage 的 Strict/Equivalent coverage，保留 unmapped/needs-review；覆盖只是诊断，不替代正确率。

先比较单个因素：正文投影、去重/选段、claims/验证、检索 profile；不同时改模型、prompt、检索与预算后归因。问题类型只能来自问题/请求，不读 gold。开发 case 2 的 stop-word 限定、17 的 reviews 数量、149 的组件列表、211 的极性及 56 的复合引用作为失败族示例；测试验证业务规则，不把这些题答案写进生产代码。

fixture 上的获胜 profile 先做固定样本端到端比较。若要推广到生产，必须另有真实 Qdrant 读路径检查和显式切换记录；fixture 不能代替生产检索质量证据。

### D7. 可靠性与质量实验分开

阶段 0 开始诊断那 45 个稳定生成失败 case，采用冻结清单、有限乱序回放及按题型/长度匹配的成功对照；先以非模型的 request/projection 检查缩小范围，再执行事先声明次数的真实请求实验。

逐 attempt 记录 case/request digest、模型/部署身份、请求长度与预算、顺序/时间、HTTP/provider code、finish reason（实际可得时）、重试及脱敏限长错误，缺字段写未知。区分请求形态、顺序/限流、超时和 provider 问题，不预设 502 来自何处。所有尝试计入成本，同一 case 不因结果更好被择优保留。

仅 judge 恢复冻结答案；生成器、prompt、上下文或检索变化的重答属于新行为比较，不能合并成旧运行纯恢复。故障诊断未得到可解释结果前，不用下一轮完整 300 题代替调查。

### D8. 分阶段联合验收

| 阶段 | 交付 | 出口条件 |
|---|---|---|
| 0：测量与诊断 | metric/policy manifest、数据划分、人工校准任务、502 诊断计划 | 分母、非零改善幅度、校准/样本/成本及统计门槛冻结；历史口径可重算。 |
| 1：证据与预算 | lossless body、去重/选段、最终请求 snapshot | 正文不会无意回退 summary；超限真实裁剪/拒绝；不可见后半段不能充当生成支持证据。 |
| 2：事实与引用 | structured candidate、exact binding、确定性 renderer/gate | 已知四类错误有成功及失败回归；没有自动整答挂载或未绑定正文发布。 |
| 3：校准及修复 | verifier shadow/校准、固定 gate policy、既有有界 repair | 校准达到冻结标准；未知/error/耗尽受控处理；所有 transition 可持久化重放。 |
| 4：检索与误拒 | 完整缓存 offline replay、单因素配对小样本 | 未召回/被裁剪/未使用可区分；候选改善不扩大最终预算，质量联合指标达到阶段门槛。 |
| 5：冻结验收 | 未调参 paper-family held-out、最后完整 300 题回归、资格报告 | 满足下列全部规则；失败后回到开发，已查看的验收集不再充当下次未调参验收。 |

联合通过要求：`citation_support_micro_visible >= 0.98`，同时固定题集总体正确产出与可答题正确实答产出达到预声明的正向改善，误拒达到预声明下降，关键事实覆盖非退化，生成/judge 故障和总成本在上限内。任何必要引用缺评、样本量不足、配置不合格或阈值缺失均为 `inconclusive`；不能只发布支持率达标。

以 paper family 为统计聚类单位，预先声明 paired comparison、重复次数和区间方法；一次冻结执行或预声明重复的全部结果进入报告，不取最优运行。98%首先是样本点估计；若声称总体至少 98%，还需事先设计样本量并使预声明单侧区间下界满足目标，不能把相同论文的引用当成独立试验。

## Risks / Trade-offs

- [严格核验降低有效回答率] -> 正确实答产出、误拒与关键事实覆盖联合 gate，不能靠增加拒答通过。
- [正文恢复后稀释上下文或增加成本] -> 实际请求预算、内容去重和单因素对照；记录所有 repair/verifier/judge 尝试成本。
- [语义 verifier 的错误被包装成确定性证明] -> 独立盲审校准、typed/span 检查及未知状态；单个模型布尔值不构成通过条件，独立评测检验残余错误。
- [claim 拆分遗漏限定或答案部分] -> 保存问题回答部分和最终回答跨度；限定/极性/数值角色及核心事实覆盖分别验证。
- [旧数据或标签争议改变分数] -> 历史工件不覆盖；官方与冻结审计评分并列，争议不因支持某方案而选择性修订。
- [重复使用验收集导致过拟合] -> 按论文族隔离；一经用于选方案即降为开发数据，补充新的未调参验收。
- [实验范围膨胀] -> 复用现有 service/ports/replay/标注工具；不把整个 evaluation protocol 的剩余任务捆绑为本次工程。

## Migration Plan

1. 提交测量、artifact 和 span 合同，再按上述顺序完成 evidence、claims、verification/repair 和检索变更；每个代码提交前运行匹配测试及 required smoke。
2. 新候选协议及严格 gate 先在具名实验 policy 内启用，生产默认不在实验过程中自动切换；全部行为变化具有独立身份。
3. 离线回归通过后，按冻结 manifest 执行有界小样本及校准实验；达到出口条件再进行独立验收，最后完成 300 题回归。
4. 运行 `openspec validate paper-answer-grounding-quality --strict`。正式推广需要联合资格报告；生产策略切换保留版本与回滚入口。
5. 回滚恢复先前 policy/schema 和相关提交，历史快照、失败尝试及资格报告保留。严格 policy 不可在运行中静默降级为只看 ID 的旧 gate。

## Open Questions

- 502 的最终归因、可支持的事实类型和最优检索 profile 需要上述对照实验回答；当前不预选结论。
- 阶段 0 必须确定独立论文族样本、校准人员/仲裁和各项非 98% 门槛；缺失时不能启动依赖这些条件的正式资格评测。
- provider 真实 tokenizer、内部消息变化及可得的 finish reason 由适配器检查确认；不可得时采用声明过的保守界限或明确阻断，不能虚报精确值。
