## Context

本变更把已经讨论的完整 Paper RAG 评测范围落成可实施协议；当前只交付 OpenSpec，不运行题库制作、模型评测或业务代码迁移。

截至本次代码核对，已有可复用资产与需要补齐的边界如下。这里只描述代码能力，不声称当前配置已得到真实评测成绩。

| 资产 | 复用方式 | 当前缺口 |
| --- | --- | --- |
| `F:/github/NewsRoom/data/eval/golden_set.json` 与 `golden_corpus_snapshot.json` | 既有题目和固定回归来源 | 79 题需重新核验来源/暴露史/标准事实；最小 gold snapshot 不是完整检索语料 |
| `F:/github/NewsRoom/backend/research/rag/evaluation/paper_evidence_eval.py` | `EvidenceQAPair`、检索评分、证据元数据 | 原文锚点、题库冻结、独立 split 与按索引映射需要显式契约 |
| `F:/github/NewsRoom/backend/research/rag/evaluation/evidence_eval_runner.py` | 一个结构化 evaluator core 与现有 CLI | 固定答案、内存 hash embedding、真实模型和外部语料不能混称生产端到端 |
| `F:/github/NewsRoom/backend/research/rag/evaluation/paper_benchmark_suite.py` | 审查、生成、抽检、阈值及报告 | 当前会从 chunks 构建问题，部分生成使用 gold 组织上下文；正式运行需禁止 |
| `F:/github/NewsRoom/backend/research/rag/evaluation/paper_generation_eval.py` | claim/citation 诊断与人工校准基础 | 分数缺失不能经默认值或其它指标填成已测；需要可替换 judge 与显式状态 |
| `F:/github/NewsRoom/backend/research/rag/evaluation/paper_evaluation_compare.py` 与 `paper_benchmark_matrix.py` | A/B 差值、多数据集报告 | 扩展配置可比性、三轨/多方案、重复次数和统计不确定性 |
| `F:/github/NewsRoom/framework/rag/evaluation/` | 通用确定性指标与 `RAGEvaluationReport` | Paper 契约仍在 Research；不把业务评分迁入 framework |
| `F:/github/NewsRoom/interfaces/services/paper_rag_service.py` 与 `F:/github/NewsRoom/interfaces/composition/research.py` | 产品检索与 gated answer 的实际装配 | 通过外层注入接入评测，不从 Research 反向导入 interface/infrastructure |

`research-evidence-context-quality-foundation` 继续拥有输入完整性、Claim 资格 gate、候选检索收益与上下文连续性触发。本变更扩展其既有评测入口，不改其控制平面或默认关闭策略。工作区中其他未提交改动不属于本变更。

## Goals / Non-Goals

**Goals:**

- 建立一套冻结、人审、按论文隔离、可定位原文的题库，并保留题目和标注来源。
- 使用相同数据契约运行 `retrieval`、`oracle_generation`、`end_to_end`，区分生产与固定样例身份。
- 用真实 application/Harness 路径得到可重放结果，再执行规则、语义 judge 和人工复核。
- 给出检索、事实、引用、拒答、成本、尾延迟和失败原因；支持成对比较与只重评分。
- 最终交付开发/回归集、独立保留集及三轨验证证据；20 题纵向试点只作为中间里程碑。

**Non-Goals:**

- 不改生产 RAG 默认策略，不自动优化模型/prompt，不扩展跨论文 agent 工作流。
- 不建设新的 evaluator 平台、数据库、run/event store、Web 标注应用或自动 promotion 系统。
- 不把评分器变成在线 workflow gate，不训练 judge，不授权 LLM 发布、写 active skill 或改生产记忆。
- 不把原始论文支持的所有事实都宣称可确定性核验；不将文本子集成绩称为多模态整题通过。

## Decisions

### 1. 在现有 evaluator 上扩展协议，职责仍归现有层

外层 CLI 调用 Evaluation application service；composition 装配真实 Research application adapter、可选 judge adapter 和既有工件访问端口；Research-owned core 调度固定实验并聚合结果。

```text
scripts.dev / CLI
    -> Evaluation application service
        -> Research evaluation core
            -> injected production application adapter
                -> existing Research service -> Harness -> existing ports
            -> deterministic scorers / injected semantic judge
            -> existing report, transcript and artifact facilities
```

`backend/research` 仅定义所需协议/业务语义，不导入 `interfaces` 或 `infrastructure`。框架继续提供通用指标，Research 负责论文、证据组与答案口径。新增对象用于版本化配置和投影，不建立第二套执行状态机或事件权威源。

**替代方案：**拒绝整体迁入另一套 RAG 框架、直接调用生产 store/executor、或为每条实验轨编写一套独立 runner；这些方案会导致运行路径与评分语义漂移。

### 2. 四份版本化契约，执行输入与答案册分开

| 契约 | 必要字段 |
| --- | --- |
| `DatasetManifest` | `schema_version`、`dataset_id/version`、`corpus_snapshot_id/hash`、来源/许可引用、论文版本集合、split/group 清单、annotation 版本、冻结状态与校验值 |
| `EvaluationCase` | `case_id`、问题、论文/scope、题型、出题方式/可见输入记录、可答性、参考答案/原子事实、原文证据、人工审核及暴露史 |
| `EvaluationRun` | `run_id`、dataset/case-set/语料/索引身份、track/profile/variant、配置及代码指纹、模型/prompt、预算、环境、重复轮次、状态和原生 transcript 引用 |
| `EvaluationScore` | `run_id/case_id/attempt`、metric ID/version、nullable value、status/reason、分子/分母、判分来源、证据引用及评分版本 |

四类记录均声明 `record_type`、`schema_version`、`protocol_version`；案例与评分引用固定版本和内容摘要，拒绝可漂移的 `latest`。统一现有 `EvidenceQAPair` 的序列化与消费边界，避免维护互不相通的第二种论文 QA 模型。冻结题库可导出两个投影：`QuestionInput` 只有问题与合法来源范围，case ID/预算可放在独立且不含标注的运行控制参数；`ReferenceLabel` 包含期望行为、答案/事实、gold 和人工标注。A/C adapter 不能接收完整 case 或可访问答案册的回调。

机器绝对路径不是数据身份；内容哈希、论文修订、原文定位和索引指纹才是身份。工件路径按既有根目录解析并验证，不将机器本地路径写入可比性哈希。

**替代方案：**拒绝把全量 JSON row 传给生产调用方并约定其“不读 gold”；在 DTO/端口边界只投影允许的字段。

### 3. 冻结题库是独立的人机审核流程

生命周期为 `draft -> annotated -> reviewed -> frozen`，无法裁定的题为 `needs_review`，不进入正式适用集合。

- `abstract_only` 提问者只能收到标题、摘要和可审计输入指纹；先冻结问题，再向另一个会话/标注者提供全文。
- `real_user`、`fulltext_targeted` 和模板合成题分别记录来源。Table/Equation 编号若未出现在摘要，不得伪称来自严格的摘要盲提问。
- 标注者输出答案、原子事实和最小充分证据；人工确认可答性、原文支持及具体位置。LLM 产物仅是候选，不能自动标记为人工审核通过。
- 未找到答案、解析缺页、看不懂表格和全文确实没有所问信息分别处理。只有检查完整材料且被复核的缺证题才标为 `abstain`。
- 79 道既有题的默认身份是待审计开发/回归材料，不因重新随机划分而获得未暴露 held-out 身份。
- 先完成约 20 题的全流程试点，再完成既有题审计并新增 50–100 道未参与调参的保留题。题型覆盖方法、实验、表格、公式、跨段落、无答案；数量是交付目标，不是统计充分性的保证。低质量候选不为凑数入库。

split 按论文及版本族/近重复 group 划分。语料可包含待评测论文全文，这是 RAG 的正常知识输入；禁止的是标签、答案、题目派生笔记与运行记忆的泄漏。纠正 frozen gold 产生新 annotation/dataset 版本，所有对比方案在相同修订上重新评分或运行，旧记录不覆盖。

**替代方案：**拒绝把自动题生成器直接放在正式 benchmark 内；保留现有 builder 作为显式候选制作或 fixture 功能。

### 4. 原文证据与检索切块独立

`EvidenceAnchor` 绑定 source revision/content hash、原文片段、章节/段落及适用页码/坐标/表格单元格、支持的 fact ID。页码区分 PDF page index 与印刷页码；LaTeX 来源使用 section/element/span 等可重定位标识，不伪造 PDF 页号。

`required_evidence_groups` 表示必须共同满足的不同事实/条件；每组允许多个等价充分证据集合。映射产物绑定 anchor、parser/chunker/index snapshot，记录 exact/equivalent/ambiguous/missing 和匹配依据。禁止通过题目/答案给 chunk 增加生产检索特征。

冻结证据锚点失效属于数据/映射错误；原文完好但待测解析/切块漏掉了证据属于系统失败，不能删掉题目或自动改 gold。正式检索索引使用完整论文正文、允许的图表/附录与干扰内容，不把只有 gold chunks 的回归 snapshot 当完整语料。

**替代方案：**拒绝将旧 chunk ID 当永久 gold，也拒绝将整个论文视为一个正确证据而掩盖局部漏检。

### 5. track 与执行身份正交，只有一轨可接触 gold context

| track | 实际输入 | 必须记录 | 成绩用途 |
| --- | --- | --- | --- |
| `retrieval` | `QuestionInput` | 初始召回、重排、证据映射及可选最终上下文投影 | A：评检索，不生成答案 |
| `oracle_generation` | 问题 + 审核过的充分原文证据 | 实际送入模型的 text/image、裁剪与生成记录 | B：隔离生成能力，不参与生产方案排名 |
| `end_to_end` | `QuestionInput` | 产品路径召回、上下文、答案、引用、gate/transcript | C：真实问答效果 |

`execution_profile` 为 `fixture` 或 `production`；另记实际 component provenance、`gold_context_used` 和 adapter 身份。真实模型加 hash embedding 仍是 fixture；真实论文加仅 gold 的缩减语料也不具备正式 production 资格。不能只凭 manifest 自报 production，装配结果要检查。

B 只评已审核可答题，不提供 reference answer/facts；同一 generator/prompt/context budget 下只跑一组，不为每个检索 variant 重复付费。B 沿用相同 answer worker、输出合同及 budget，原生诊断工件隔离，不恢复已删除的无门禁 direct answer 产品路径。超过上下文窗口或缺少图像能力时明确 error/不可评范围，不静默裁掉必要证据后声称完整 oracle 通过。

C 调用产品使用的 Research application 路径；chunk RAG 使用既有 `rag_ask(generate=True, gated=True)` 语义。完整原文经实际解析器、embedding 和索引建立 corpus/index snapshot，建库成本与逐题在线时延分别记录。A/C 上下文补齐只能依赖生产来源关系，不能依赖 gold ID/答案标签。

**替代方案：**拒绝以统一的 `live=true` 混合以上模式，或将 oracle 分数并入正式总分。

### 6. 有界实验调度、隔离与恢复

实验单位为 `(dataset_version, case_id, track, variant_fingerprint, repetition)`；尝试另记 `attempt_id`。采用既有 Harness 的 bounded PLAN/EXECUTE/VERIFY、重试与 durable transcript，不由 LLM 决定下一轨、预算、skip 或发布。评测聚合本身是确定性 service，不新增 agent workflow。

manifest 必须声明总请求/token/费用预算、每次超时、并发、重试上限与恢复策略；未知供应商价格不伪造费用，保守 token/request 上限仍生效。达到预算后停止新请求，未执行项和原因保留，run 标为 incomplete。

外部调用前记录尝试，完成后保存响应及真实 usage。崩溃发生在供应商已接收请求但结果未落盘时，不能承诺 exactly-once 或自动把该题记为成功；记录不确定尝试，只有显式恢复策略允许且预算足够时再请求，重复成本可追踪。

使用独立评测 tenant/index/memory namespace；在线响应缓存和记忆初态按 manifest 固定并在方案/题目间一致恢复。允许写隔离评测工件与 trace，不写生产业务记忆、正式发布结果或 active skill。原始语料和 gold 不出现在日志中的凭据字段，第三方 judge 调用仅在相应数据可发送策略允许时执行。

**替代方案：**拒绝无上限 parallel calls、隐式多次重跑取最好答案、以及沿用用户生产会话中的残留记忆。

### 7. 指标口径、错误和分母必须可审计

所有指标注册 `metric_id/version`、输入要求、适用题目、聚合粒度、方向、缺失策略。确定性核验只用于可验证的 ID、来源、数值/单位和显式条件；不把字符串匹配当成普遍的语义正确性。

| 指标 | 基本口径 |
| --- | --- |
| `hit_at_k` | 可答题中前 k 至少命中有效证据的题数 / 适用可答题数 |
| `evidence_recall_at_k` | 按预声明的必要证据组计算每题覆盖率，再对题目平均；替代证据不重复计分 |
| `mrr` / `ndcg_at_k` | 使用冻结相关性与去重规则；graded nDCG 需显式等级，否则使用声明的 binary 口径 |
| `context_evidence_coverage` | 最终模型输入覆盖的必要证据；不能用检索命中替代 |
| `fact_precision/recall/f1` | 回答事实与参考事实的语义正确性/完整性；额外但有原文支持的事实转审核，不因参考答案不完备自动判错 |
| `faithfulness` | 实际模型上下文支持的回答主张 / 可核验回答主张 |
| `citation_support/coverage` | 精确引用是否支持绑定结论，以及需要引用的结论是否得到充分引用 |
| `abstention_recall` / `false_abstention_rate` | 正确拒答 / 应拒答题；误拒答 / 可回答题，分开报告 |
| `end_to_end_success_rate` | 已满足该题行为、事实/证据、引用及执行要求的次数 / 全部适用次数；明确重复次数不是新增独立题 |

空答案、没有 claims、没有引用不会得到自动满分。空分母为 `not_applicable`；应有引用却无引用是质量失败，不是 NA。缺少必需评分输入、judge timeout 或输出不合约为 error/needs_review，不用 0 或其他指标补齐。

汇总保留 `planned/applicable/valid/error/not_applicable/pending_review`。运行超时、解析丢证据等属于系统失败并留在端到端分母；judge 失败属于评分未完成，不冒充模型回答错误或正确。必须同时报告已知成功、已知失败、未知数和全集分母；未知未裁定前不形成正式资格结论，可显示已核验成功率下界并明确标记。

门禁支持 `min`、`max`、`delta_min`、`delta_max`、最低样本与有效覆盖要求。未配置业务阈值表示尚未获得质量资格，不是默认通过；fixture 既有阈值不得照搬为真实产品标准。

### 8. 可选语义 judge、独立校准和审核版本

在 Research 端定义语义评审 port，外层提供 Ragas adapter，保持核心无第三方库依赖；依赖锁定实际测试过的版本。已有 GenerationEvaluator 可作为另一实现，但同名分数必须共享语义版本，不能互相填补空值或混合比较。RAGChecker 不作为首版必需依赖，保留接口替换空间即可。

judge 输入按指标分开：事实评分看问题/参考事实/回答；faithfulness 只看实际上下文与回答；引用核验看被引用的具体原文与结论，而不是任意检索内容。记录 provider/model revision、prompt/rubric、adapter/metric version、raw judgment、解析状态和预算。图像/表格仅在可验证的文本表示或有声明能力的 multimodal judge 下评分，否则明确范围不足。

校准集来自非保留集，先用约 30 条人工标注结果校准规则和 judge；有条件时双人独立判分并裁决。单人审核必须标记 single-review，不伪造 inter-annotator agreement。随机校准样本与定向冲突样本分别报告一致性，不将高冲突抽样率当总体准确率。

人工审核导出稳定 case/run/score revision ID、盲化方案标签、原文/上下文/答案和理由。导入时验证当前版本、审核者及目标；过期或未知 ID 拒绝合并。审核只能形成标注/评分新修订，不能原地改运行输出，也不能授权发布。

只重评分复用不可变运行输出；缓存键含问题、来源/上下文、答案、judge/rubric/metric 指纹。prompt、原文或 gold 修订变更后，旧缓存不命中，所有并列方案使用一致评分版本。

**替代方案：**拒绝用生成模型自评的一条总分替代规则与人工校准；拒绝为获得高分反复选择评分器输出。

### 9. 多方案对照与不确定性

首批配置为 `current_baseline`、`dense_only`、`hybrid`、`hybrid_reranker`，名称对应实际启用通道、权重和重排实现。若某方案不可用，报告 not_run/incomplete，不用近似实现顶替。比较 chunking/embedding 时允许相应索引指纹不同，但差异必须预声明且映射同一原文证据。

比较前检查 dataset/case set、原文、split、track/profile、scope、生成器/prompt、最终上下文 top-k/token budget、cache/memory 状态、并发与计时边界。代码身份同时记录 Git revision 与相关工作区内容指纹；正式发布证据要求可重建的已提交版本，不能只记录 HEAD 掩盖未提交补丁。`allowed_differences` 只允许待研究变量；不匹配则展示独立结果并标记 incomparable，不输出胜负。

可用 `k=[1,3,5,10]` 观察检索，生成时固定最终预算；回收候选数和 reranker 开销另行报告。生成配置建议重复 3 次作为首轮波动测量，实际次数与费用上限先写入 manifest。按同题成对计算差值；先在题内聚合重复，再按论文 cluster bootstrap 或预声明等价方法估计区间，不能把同一论文的所有题或重复回答当成独立样本放大置信度。

展示总体、题型/模态/难度切片、样本数、区间、逐题改进/退化与失败链路；小样本或区间不足以支持结论时标记 insufficient_evidence。oracle 只提供条件诊断，不进入生产方案排名。

### 10. 现有工件与报告扩展，不建立新 run store

沿用 `F:/github/NewsRoom/.newsroom/eval/` 下的实验工件根和既有 Harness 持久化机制。新增 manifest、case 输出、score revision、审核队列、比较结果均是同一 run 的派生工件，原生执行状态仍以 Harness transcript/event 为准。

- 输入身份：冻结 dataset/experiment/config 与实际代码/adapter provenance。
- 逐例输出：召回、重排、实际模型上下文、answer/citations、attempt/error、原生 trace 引用。
- 评分工件：逐例指标、理由、judge 原始记录、人工标注修订与有效覆盖统计。
- 汇总：继续生成 JSON/Markdown，增加 `protocol_version`、track/profile、completeness、comparability、decision、各成本账单及逐题差异。

正式决策为 `pass/fail/inconclusive/not_applicable`；执行状态与质量决策分开。旧 `passed` 字段在正式协议中仅当所有必需评分和资格条件明确 pass 时为 true。通用 `MetricValue` 继续承载已有效测量的数值，完整 nullable/status 记录由 Research metadata/逐例结果承载，避免 framework 引入论文语义。

费用分 `index/request/judge`；provider token/费用原始记录优先，缺失保持 null。估算费用记录价格表版本与计价时点，不写死过时价格；失败与重试不消失。P50/P95 明确样本量、冷热缓存、并发和范围，超时计入失败/SLO 违约，不能只用幸存成功请求声称整体快速。

### 11. CLI/CI、兼容边界与发布权

拟在现有 `scripts.dev` 增加一个 `rag-eval` 命令组，由现有 CLI/application/composition 边界承接；子操作为 `validate-dataset`、`run`、`score`、`export-review`、`import-review`、`report`。名称是本变更拟交付接口，当前未实现；内部只传结构化 options，不让库调用方拼接 CLI argv。

既有 `run_evidence_eval`、`run-live-answer-eval`、`test-rag-eval-gate` 参数与原始报告字段按本变更显式兼容要求保留。未选择新正式协议的调用保留既有诊断阈值行为，追加身份说明；有外部论文或真实 LLM 也不自动变成生产资格。

- PR：离线 fixture、契约、gold 输入隔离、证据映射、评分边界、报告与 CLI 测试，不读取 live 凭据。
- 定期/手动：显式 manifest 的生产开发集，有限预算并导出原始工件。
- 发布前：配置冻结后显式选择保留集，记录审批/评测用途与暴露史，不把保留集接入日常调参循环。

诊断模式缺少凭据可以 skipped，但正式必需阶段未执行时整个验收为 inconclusive 且返回非零；跳过的信息和工件必须保留。正式 run 没有冻结题库、完整 corpus、真实 adapter 或声明预算时，在付费调用前拒绝启动。

评测资格由版本化确定性规则计算；它只是既有发布/审批流程的输入。judge、人工标注导入和 benchmark runner 均不能直接 promotion、publication 或修改 active skill。

### 12. 交付证据与分阶段验收

里程碑依次是协议/契约、约 20 题纵向试点、三轨生产路径、评分与审核、多方案对照、完整题库与 CI。每阶段交付可运行测试和真实状态，不将后续 live/人工任务勾为完成。

最终至少提供：既有 79 题逐题迁移/审核去向；新增 50–100 道保留题或经显式范围调整后的审核目标；完整语料及版本；三轨与四种方案的适用性/执行状态；judge 校准与人工分歧记录；有界费用/时延报告；配置可比性与逐题差异；固定回归与真实测量分开的发布证据。若人工审核、模型凭据或预算未就绪，相关任务保留未完成，不生成伪造 gold、评分或通过结论。

## Risks / Trade-offs

| 风险 | 控制方式 |
| --- | --- |
| 既有题已用于优化或模型已见过公开论文 | 记录暴露史，新增论文级保留集，不声称可证明模型预训练完全未见 |
| 原文锚点跨 parser 难对齐 | 版本化映射和人工可核对定位，区分 gold 错误与系统漏证据 |
| 对同一题存在多套正确证据 | 保留替代充分集合，不简单取所有证据并集作分母 |
| 图表被文本 judge 误判 | 记录实际输入模态及评分能力；范围不足不产生全题通过 |
| 小数据集上的高分不稳定 | 报告论文组数、重复次数和区间，不预设行业万能门槛 |
| 多方案 × 重复 × judge 导致费用膨胀 | 先成本预检，B 按生成配置复用，保存输出后重评分，有预算再恢复 |
| 评测结果写入生产记忆造成污染 | 隔离 namespace、固定初态与副作用策略，验证无生产发布或 skill 写入 |
| 旧 scalar 分数与新语义混淆 | protocol/metric 版本与状态显式，不跨版本自动排名 |

## Migration Plan

1. 本次仅提交 proposal/design/specs/tasks；不迁移题库、不安装 Ragas、不运行收费调用。
2. 先实现版本化合同、验证、盲输入投影与显式模式；旧诊断入口继续可用但不授予 production 身份。
3. 核验既有题库与语料，建立候选标注/冻结流程和原文映射；先用约 20 题验证完整流程。
4. 用外层注入装配 A/B/C，保留原生 Harness gate、trace、budget；实现逐例恢复与费用记录。
5. 接入规则/语义/人工审核、只重评分、比较及报告；完成已批准的正式阈值与 judge 校准。
6. 扩充独立题库，接现有 CI，并完成实际授权的 live 验收；不能由 fixture 结果代替。
7. 对代码变更执行匹配单元/合同/架构测试及必需的 `python -m scripts.dev smoke`，执行 `openspec validate research-rag-evaluation-protocol --strict` 后逐阶段提交。

### Rollback

停止新正式评测任务或关闭其 CI 入口，保留既有离线 gate 与旧诊断命令。停止/取消在途任务由既有 Harness 控制，不删除历史题库、原始响应、评分修订或 transcript。回退应用版本时新协议工件只读保留，不伪装成旧协议结果；已冻结语料和题库不做原地降级。生产默认策略不在本变更内改变，因此无需回滚生产检索算法。

## Open Questions

以下是首次正式运行前需确认的运行输入，不阻碍合同与离线实现，也不能用默认值冒充已获批准：

- 新增保留论文清单、材料许可/第三方 judge 可发送范围，以及实际人工审核安排。
- 实际生产 embedding/reranker/generator/judge provider 与版本、硬件、并发、价格表及费用上限。
- 首轮 baseline 后在开发集上确定的业务阈值、最低样本/有效覆盖要求与保留集使用批准。
- 若仅单人复核，接受的审核覆盖和分歧处理方式；不将其报告为双人一致性实验。
