# 独立 heldout 题库：60 道中文审核材料

> 这是独立 heldout 自动化模型题库的中文阅读包，共 **60 道题、10 篇论文**，与 legacy 的 79 道题分开。每题保留原英文题目，并提供中文翻译、通俗问法、基于冻结 `reference_answer` 的候选答案、既有 自动化模型审核建议和限制说明。
>
> **重要：** `accepted` / `needs_review` 是既有 自动化模型复核记录 的状态，不是正式真人审核结论。本文件也不是外部审核者身份认证、正式 workflow approval 或 heldout qualification。请逐题填写你自己的决定：接受、要求复核或不接受；如果只阅读本文件而没有重新阅读论文，请如实保留这一范围。

## 总体统计

- 题目：60 道（`heldout-ai-001` 至 `heldout-ai-060`）
- 论文：10 篇
- 既有 自动化模型建议：54 道 `accepted`、6 道 `needs_review`、0 道 `retired`
- 正式认证真人审核：0 道
- 题目、参考答案、证据和原审核记录均来自冻结目录 `data/eval/llm-review/independent-heldout/v1/`；本中文包不修改冻结目录。

## 题目正文

## 01. `heldout-ai-001`

- **case_id**：`heldout-ai-001`
- **论文版本**：`2610.19780v1`
- **英文原题**：How was LabOPBench constructed so that each item connects laboratory evidence, procedural context, an open-ended decision, and a question-specific scoring rubric?
- **准确中文翻译**：LabOPBench 是如何构建的，才能让每道题都把实验室证据、程序性背景、开放式决策和题目专属评分标准连接起来？
- **通俗问法**：简单说：作者怎样把“看实验材料—理解实验过程—做判断—按这道题自己的尺子评分”串成一道题？
- **按 `reference_answer` 翻译的中文候选答案**：LabOPBench 由 22 名具有实验室经验的贡献者构建：他们把真实实验室材料转化为开放式决策题。每道题包含实验/程序背景、一个或多个证据来源以及所要求的判断；背景提供足以完成判断的最小必要信息，例如实验阶段、操作对象、观察到的现象、仪器类型或已知条件。出题者明确记录从证据到决策的路径、相关证据、可接受的替代解释/行动、关键错误，以及题目专属且单调的 3/2/1/0 评分标准。题目必须能够依据所给证据和相关科学知识作答，不能依赖隐藏的实验条件或作者私有信息。科学内容确定后，DeepSeek-V4-Pro 只负责语言标准化，不能改动判断、证据、可接受答案、评分锚点或安全要求；作者会核对标准化版本，另外两名专家独立检查证据可信度/充分性、提示与证据的一致性、科学正确性、确定性和可评分性。实质性分歧由第三名专家裁决；证据不足或存在未解决歧义的题目会被修订或排除。
- **证据页码**：2, 3, 4, 13
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The fixed paper describes the construction and review process, not a per-item authoring/reviewer log. Continuous PDF-native paragraph quotes are used because coordinate-sorted pages.json interleaves the two columns; no cross-column text has been fabricated or joined.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 02. `heldout-ai-002`

- **case_id**：`heldout-ai-002`
- **论文版本**：`2610.19780v1`
- **英文原题**：What normalized score did each evaluated multimodal model obtain overall, and how did model performance vary between organic synthesis and materials science items?
- **准确中文翻译**：每个被评估的多模态模型总体获得了多少归一化分数？模型在有机合成题和材料科学题之间的表现有什么差异？
- **通俗问法**：简单说：八个模型总分谁高谁低？它们做有机合成题和材料科学题时差多少？
- **按 `reference_answer` 翻译的中文候选答案**：各模型的总体分数 / 约加权有机合成分数 / 材料科学分数（%）为：Intern-S1-Pro 28.8 / 27.0 / 41.7；Gemini 3.1 Flash-Lite 36.8 / 35.3 / 47.6；Grok 4.3 37.5 / 35.3 / 53.7；MiniMax-M3 39.6 / 37.4 / 55.6；Qwen3.6-Plus 41.2 / 39.6 / 52.8；Gemini 3.1 Pro 41.9 / 40.3 / 53.5；GPT-5.5 43.6 / 42.4 / 52.9；Claude Opus 4.7 45.8 / 43.8 / 59.8。对每个模型，材料科学分数都高于加权有机合成分数，差距约为 10.5–18.4 个百分点。论文直接报告了总体分数和五个主要类别分数；上面的有机合成汇总值是按四个有机类别的题目数量 67/30/425/142 加权、根据表中保留一位小数的分数推导出的近似值，并非论文直接打印的精确领域汇总。论文报告材料科学的跨模型均值为 52.2%，而有机合成中的 postprocessing 是最困难的主要类别，分数为 34.9%。
- **证据页码**：3, 6, 7, 18
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The fixed paper does not print one combined organic-synthesis score per model. These organic scores and the domain score differences are explicitly derived from displayed one-decimal category scores and their item counts, not presented as exact author-reported domain aggregates.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 03. `heldout-ai-003`

- **case_id**：`heldout-ai-003`
- **论文版本**：`2610.19780v1`
- **英文原题**：On the controlled 100-question subset, what were the model, undergraduate, and PhD participant results, including the sample sizes, aggregation method, and uncertainty or significance statistics reported for the comparison?
- **准确中文翻译**：在受控的 100 道题子集上，模型、本科生和博士参与者的结果是什么？其中包括哪些样本量、聚合方法，以及论文为比较报告了哪些不确定性或显著性统计？
- **通俗问法**：简单说：100 道统一题里，8 个模型、5 名本科生和 5 名博士各自得分怎样？分数怎么合成，论文有没有给置信区间或显著性检验？
- **按 `reference_answer` 翻译的中文候选答案**：在匹配的、受控的 100 道有机合成题子集上，模型分数为：Claude Opus 4.7 43.7%、GPT-5.5 43.5%、Gemini 3.1 Pro 39.0%、Qwen3.6-Plus 39.0%、MiniMax-M3 37.8%、Gemini 3.1 Flash-Lite 35.4%、Grok 4.3 34.5%、Intern-S1-Pro 26.9%。共有 10 名真人参与者：5 名本科生和 5 名博士，每人回答全部 100 道题。两位审查者评分均值为：本科生 U01–U05 分别为 27.4、30.2、34.4、30.3、32.7%；博士 P01–P05 分别为 47.3、46.6、40.8、48.4、46.5%。本科生组均值为 31.0%（样本标准差 2.7 个百分点），博士组均值为 45.9%（样本标准差 3.0 个百分点）。每个回答由 GPT-5.5 和 Gemini 3.1 Pro 按同一题目评分标准独立评分；每位审查者的分数都在全部 100 个有效回答上归一化，报告的参与者/模型分数是两位审查者归一化分数的算术平均，人类组汇总则是 5 名参与者的均值和样本标准差。论文没有为这一比较报告置信区间或模型与人类之间的显著性检验；Claude 与博士组相差 2.2 个百分点只是描述性结果。
- **证据页码**：5, 22, 23
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The paper reports sample SD only for the human groups and no inferential model-human comparison. Four model rows are directly quoted; the remaining subset values were checked in Figure 5/Table 16 from the same complete PDF.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 04. `heldout-ai-004`

- **case_id**：`heldout-ai-004`
- **论文版本**：`2610.19780v1`
- **英文原题**：How do the benchmark design and the reported Evidence Misreading and Evidence-Decision Conflict error categories jointly explain what current multimodal models fail to do, and what limitations does the paper identify for generalizing this conclusion beyond the evaluated laboratory settings?
- **准确中文翻译**：基准测试的设计，以及报告中的“证据误读”和“证据—决策冲突”错误类别，如何共同说明当前多模态模型失败之处？论文指出哪些限制，使这一结论不能直接推广到评估之外的实验室场景？
- **通俗问法**：简单说：这些题和错误分类具体暴露了模型链条中的哪两类问题？为什么不能据此说模型已经具备真实实验室的闭环自主能力？
- **按 `reference_answer` 翻译的中文候选答案**：LabOPBench 要求模型依据特定案例的多模态证据和实验背景，判断状态、原因、风险或合适的操作，而不只是识别物体或回忆事实。“证据误读”指误读或遗漏与决策相关的实验证据；“证据—决策冲突”指作出与证据或评分标准不一致的诊断或行动建议。这两类错误暴露了证据到决策链条中相邻的两个失败环节：先识别哪些信息约束判断，再选择与这些信息一致的结论/行动。它们分别占模型平衡后的错误标签 40.0% 和 35.8%，合计 75.8%；这不是回答层面的错误率。该错误分析针对三个领先模型在 100 道受控有机合成题子集上的非满分回答，且错误分类法并不穷尽所有错误。更广泛地说，基准测试只覆盖所表示的有机合成/材料科学场景、一个静态决策点以及固定的背景和证据；它排除了连续实验轨迹、行动后的观察、机器人执行，以及干预失败后的重新规划，因此不能完整检验闭环实验自主性。这些输出错误类别也不能证明模型内部的因果机制。
- **证据页码**：3, 6, 7, 8, 23, 24
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The percentages are equally model-weighted shares of assigned labels from three leading models on the controlled organic-synthesis subset, not response-level rates or causal decomposition of model internals. The benchmark is static and the taxonomy non-exhaustive.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 05. `heldout-ai-005`

- **case_id**：`heldout-ai-005`
- **论文版本**：`2610.19780v1`
- **英文原题**：For one fully documented benchmark example, trace the evidence and procedural context through the required laboratory judgment, the item-specific rubric, and the error category assigned to an incorrect response.
- **准确中文翻译**：对于一个资料完整的基准测试示例，请追踪证据和程序性背景如何经过必需的实验室判断、题目专属评分标准，并最终得到错误回答的错误类别标注。
- **通俗问法**：简单说：能不能从论文中找出一道完整的题，展示材料、背景、正确判断、评分规则和错误分类？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目所要求的完整、可直接回答的示例答案）。
- **证据页码**：5, 8
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - A released benchmark item or supplementary artifact containing the full question, rubric, candidate response and expert error annotation would be required.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 06. `heldout-ai-006`

- **case_id**：`heldout-ai-006`
- **论文版本**：`2610.19780v1`
- **英文原题**：What exact inter-reviewer agreement statistics are reported for the expert authoring and independent review of all 756 questions, broken down by scientific domain and decision type?
- **准确中文翻译**：对于全部 756 道题的专家出题与独立审查，论文按科学领域和决策类型报告了哪些精确的审查者间一致性统计？
- **通俗问法**：简单说：论文有没有给出 756 道题按领域和决策类型拆分的专家一致性数字？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目所要求的、按科学领域和决策类型拆分的精确审查者间一致性统计）。
- **证据页码**：4, 19
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - No external review logs or benchmark-construction data may be used under the declared scope.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 07. `heldout-ai-007`

- **case_id**：`heldout-ai-007`
- **论文版本**：`2610.19432v1`
- **英文原题**：How was MULTIFINGROUND built from FY2025 English, French, and Greek disclosures, and how were reports aligned through the accounting taxonomy to produce traceable fact-level annotations?
- **准确中文翻译**：MULTIFINGROUND 是如何由 FY2025 的英文、法文和希腊文披露构建的？报告又是如何通过会计分类体系对齐，以产生可追溯到事实层面的标注？
- **通俗问法**：简单说：这 90 份三语年报怎样变成可追踪的事实数据，多个任务又怎样共享同一套会计概念？
- **按 `reference_answer` 翻译的中文候选答案**：MULTIFINGROUND 为 90 家发行人各收集一份 FY2025 ESEF 年报：英文、法文和希腊文各 30 份；每家发行人只出现一种语言，且都覆盖截至 2025 年 12 月 31 日结束的完整年度。发行人的 iXBRL 标签提供初始的事实到概念映射。报告被转换为保留表头的文本/Markdown 表格；事实通过可见文本偏移、概念、数据类型、期间类型和借贷方向保持可追溯，并附有来自 IFRS Accounting Taxonomy 2024 或发行人扩展 schema 的属性，数字还被分配标识符。发行人特定扩展只用于 T1；概念级 T2 任务使用标准的 ifrs-full 概念。共享概念连接的是不同发行人独立编写的报告上下文，而不是翻译文本。英文/法文/希腊文上下文中的所有目标出现位置都被标记；满足干扰项条件的 187 个概念产生 521 个配对组。T2b 要求找出所有对应事实，包括比较期数值；T2a 和 T2b 使用独立的无状态调用；T2c 在不提供标签的情况下测试概念辨别。两名金融专家分别审查了分层抽样的 10% 样本：131 个 T1 项、52 个共享 T2a/T2b 组和 53 个 T2c 项，覆盖语言、格式、数据类型和规模，然后讨论分歧。他们的修正/排除只作用于被审查的项目；最终标注集将这些结果与自动生成但未经审计的标注合并。
- **证据页码**：2, 3, 4, 5, 19, 20
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - The audit is not a full-dataset expert relabeling: only the stratified 10% subset is corrected/excluded by expert review. Different issuer contexts are concept-aligned but not parallel translated evidence.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 08. `heldout-ai-008`

- **case_id**：`heldout-ai-008`
- **论文版本**：`2610.19432v1`
- **英文原题**：For each of the 15 evaluated models, what were the concept-recognition accuracy and exhaustive fact-recovery results, and which models showed the largest gap between recognizing a concept and recovering all of its facts?
- **准确中文翻译**：对于 15 个被评估模型中的每一个，概念识别准确率和穷举式事实恢复结果分别是多少？哪些模型在“识别概念”和“恢复该概念的全部事实”之间表现出最大的差距？
- **通俗问法**：简单说：15 个模型“认出概念”和“把所有对应数字找全”分别考得怎样？谁认得出但漏得最多？
- **按 `reference_answer` 翻译的中文候选答案**：15 个模型的 T2a 概念识别准确率 / T2b 概念宏平均 F1 / T2b exact-set 准确率（%）为：GPT-5.6 Luna 97.3 / 88.3 / 65.8；Claude Haiku 4.5 97.5 / 86.2 / 60.1；Gemini 3.8 Flash 99.4 / 89.2 / 65.6；DeepSeek-V4.1-Flash 98.3 / 86.2 / 59.7；Qwen3.8-27B 98.5 / 62.5 / 57.0；Qwen3.8-Flash-Next 98.5 / 73.7 / 67.4；Gemma 4 31B IT 98.8 / 87.4 / 65.1；Qwen3.5-122B-A10B 99.0 / 90.4 / 71.8；Fin-R1 82.7 / 66.8 / 23.4；Fin-o1-8B 87.3 / 74.3 / 39.0；ODA-Fin-RL-8B 90.6 / 81.1 / 49.7；Plutus-8B-Instruct 77.5 / 10.7 / 0.2；Qwen2.5-7B-Instruct 88.3 / 57.0 / 7.5；Qwen3-8B 94.2 / 80.4 / 45.5；Llama-Krikri-8B-Instruct 84.1 / 44.9 / 1.0。以 T2a 正确识别为条件，至少漏掉一个目标的比例最高的是 Qwen2.5-7B-Instruct（88.7%）、Llama-Krikri-8B-Instruct（85.8%）和 Plutus-8B-Instruct（85.1%）；在 8 个 API/开放权重参考模型中，最高的是 Qwen3.8-27B（41.9%）。若采用更简单的 T2a 准确率减 exact-set 准确率差值，最大的三个差距是 Llama-Krikri 83.1 个百分点、Qwen2.5-7B 80.8 个百分点和 Plutus 77.3 个百分点。
- **证据页码**：5, 6, 7, 12, 15
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - T2b macro-F1 rewards partial recovery, while exact-set accuracy requires no omissions and no extras; the conditional miss rate permits extra selections and measures only omission among correctly recognized groups. These metrics should not be conflated.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 09. `heldout-ai-009`

- **case_id**：`heldout-ai-009`
- **论文版本**：`2610.19432v1`
- **英文原题**：How do concept discrimination and exhaustive fact selection vary by language, accounting concept, and model family, and what quantitative evidence supports the conclusion that the evaluated finance-specialized models provide limited gains?
- **准确中文翻译**：概念辨别和穷举式事实选择在语言、会计概念和模型家族之间如何变化？有哪些定量证据支持“被评估的金融专用模型带来的收益有限”这一结论？
- **通俗问法**：简单说：不同语言、概念和模型家族的结果有什么规律？金融专用模型真的明显更好吗？
- **按 `reference_answer` 翻译的中文候选答案**：论文报告了语言层面和模型层面的变化，但没有逐个会计概念给出 T2b/T2c 拆分；T2b 概念宏平均 F1 是先在每个概念内对组 F1 求平均，再跨概念求平均，不能误解为逐概念结果。联合英文/法文/希腊文证据下，各模型的 T2b 英/法/希腊 F1（%）以及当异常摘录为英/法/希腊文时的 T2c 准确率（%）分别为：API—GPT-5.6 Luna：85.0/88.3/83.9；40.3/49.4/63.4（总体 49.4）；API—Claude Haiku 4.5：85.3/85.4/83.6；57.5/62.5/60.7（总体 59.9）；API—Gemini 3.8 Flash：89.8/90.3/85.3；95.5/94.6/95.2（总体 95.1）；API—DeepSeek-V4.1-Flash：83.8/85.0/82.3；38.5/34.5/32.4（总体 35.6）；开放权重—Qwen3.8-27B：68.4/69.0/66.9；71.9/73.2/71.7（总体 72.3）；开放权重—Qwen3.8-Flash-Next：74.3/76.6/75.2；74.2/73.8/79.3（总体 75.5）；开放权重—Gemma 4 31B IT：84.6/86.2/80.4；86.9/90.5/90.3（总体 89.0）；开放权重—Qwen3.5-122B-A10B：87.7/89.1/85.8；91.9/91.1/91.7（总体 91.6）；金融专用—Fin-R1：72.4/65.2/53.8；27.6/24.4/31.0（总体 27.5）；金融专用—Fin-o1-8B：75.7/72.3/64.1；46.6/48.2/47.6（总体 47.4）；金融专用—ODA-Fin-RL-8B：81.2/78.2/73.7；42.5/48.2/47.6（总体 45.7）；金融专用—Plutus-8B-Instruct：24.6/17.0/18.2；18.1/29.2/38.6（总体 27.2）；金融基座—Qwen2.5-7B-Instruct：66.6/52.6/39.0；30.8/17.9/44.8（总体 30.5）；金融基座—Qwen3-8B：80.4/77.2/73.0；70.1/67.9/67.6（总体 68.7）；金融基座—Llama-Krikri-8B-Instruct：49.2/35.1/29.8；32.6/26.8/20.0（总体 27.3）。在 8 个 API/开放权重参考模型中，有 7 个模型的联合证据 T2b F1 在希腊文最低，例外是 Qwen3.8-Flash-Next；这些比较不是纯粹的语言效应，因为发行人、行业和文档风格也在变化。Table 10 报告的金融专用模型相对基座模型差值（T1 宏平均 F1 / T2a 准确率 / T2b 宏平均 F1 / T2c 准确率，单位为百分点）为：Fin-R1 对 Qwen2.5-7B-Instruct：−8.4 / −5.6 / +9.9 / −3.0；Fin-o1-8B 对 Qwen3-8B：−19.7 / −6.9 / −6.1 / −21.3；ODA-Fin-RL-8B 对 Qwen3-8B：−0.9 / −3.6 / +0.6 / −23.0；Plutus-8B-Instruct 对 Llama-Krikri-8B-Instruct：−24.7 / −6.5 / −34.2 / −0.2。因此，4 个金融专用模型在 T1、T2a 和 T2c 上都更低；T2b 中 Fin-R1 有提升、ODA 只有小幅提升，而 Fin-o1 和 Plutus 反而下降。这些是已发布模型之间的观察差异，不是对“金融专用化”因果效应的单独估计。根据四舍五入后的 Table 3 分数相减，会得到 Fin-R1 T2b +9.8、ODA T2b +0.7、Plutus T2a −6.6、Plutus T2c −0.1 个百分点，与 Table 10 的 +9.9、+0.6、−6.5、−0.2 不同 0.1 个百分点。固定 PDF 没有提供未四舍五入数值，无法解决这些差异；这里同时保留论文报告值和基于显示值复算的结果。
- **证据页码**：5, 6, 7, 9, 14, 16, 17
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - Per-accounting-concept discrimination/selection results are not reported. Language is confounded with issuer/sector/style; specialist/base comparisons do not isolate specialization. Four Table 10 versus rounded Table 3 differences of 0.1 pp cannot be reconciled from the fixed PDF, so this record explicitly retains needs_review for independent recheck rather than choosing hidden unrounded values.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 10. `heldout-ai-010`

- **case_id**：`heldout-ai-010`
- **论文版本**：`2610.19432v1`
- **英文原题**：Why can near-ceiling concept recognition coexist with incomplete numerical evidence recovery in this benchmark, and which dataset or evaluation limitations constrain how broadly that recognition-grounding gap can be interpreted?
- **准确中文翻译**：为什么在该基准测试中，接近上限的概念识别表现可以与不完整的数值证据恢复同时存在？哪些数据集或评估限制约束了我们对这一“识别—落地”差距的宽泛解读？
- **通俗问法**：简单说：模型知道数字属于哪个概念，为什么仍可能漏掉部分数字？数据和评分规则有哪些地方让结论不能过度外推？
- **按 `reference_answer` 翻译的中文候选答案**：在该协议中，概念识别和证据恢复是不同操作。T2a 在已经标出的数值事实中从四个候选概念里选择共享概念；T2b 在独立的无状态调用中提供该概念，并要求在相同的多语言上下文中返回所有匹配出现位置，包括比较期数值。每组平均有 7.8 个目标；漏掉一个目标就无法达到完整覆盖，exact-set 准确率还要求不能多选。因而语义概念识别正确，并不意味着能完整枚举事实。8 个 API/开放权重参考模型的 T2a 识别准确率为 97.3–99.4%，但在正确识别的组中，仍有 26.6–41.9% 至少漏掉一个目标。对 Gemini 而言，目标数为 3–6 时遗漏率为 27.7%，目标数为 7 个及以上时为 46.3%；这是相关性，不是目标数量的孤立因果效应。该差距受到标注和覆盖限制约束：初始发行人 iXBRL 映射可能有错误或概念选择不一致；专家只审查分层抽样的 10%，因此仍可能有残余错误；T1 不对未标记的类金额数字评分；大多数 T2 上下文是表格窗口，共享概念也往往是主表中的常见概念；数据只覆盖一个报告制度下的三种语言；语言还与发行人、行业和文档风格共同变化。评估本身也有影响：共享发行人使 T2 组存在依赖，因此没有报告 T2 bootstrap 区间；项目级 McNemar p 值只是探索性的，因为没有处理这种依赖。严格评分会把请求、预算、拒答和格式失败都算错，推理/生成设置也没有平衡计算量。这些限制意味着不能把该差距解释为孤立的语言效应或普遍的模型行为因果解释。
- **证据页码**：2, 4, 5, 6, 9
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - The paired gap describes this fixed benchmark and scoring protocol, not a causal mechanism or isolated language effect. Complete coverage permits extras, unlike exact-set recovery; target-count associations, incomplete audit, shared-issuer dependence, generation failures and unequal compute constrain generalization.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 11. `heldout-ai-011`

- **case_id**：`heldout-ai-011`
- **论文版本**：`2610.19432v1`
- **英文原题**：Using one aligned accounting concept from the benchmark, reconstruct how the corresponding numerical facts are identified across independently authored multilingual reports and how recognition, exhaustive selection, and discrimination are scored separately.
- **准确中文翻译**：使用基准测试中的一个已对齐会计概念，重建对应的数值事实如何在独立编写的多语言报告中被识别，以及识别、穷举式选择和辨别是如何分别评分的。
- **通俗问法**：简单说：能否从一个例子说明同一会计概念在不同语言年报里如何找出对应数字，并分别考“认概念、找全数字、排除干扰项”？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目要求的可重建答案）。
- **证据页码**：15
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - A released real benchmark group or supplementary item is required; external dataset access is outside the declared scope.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 12. `heldout-ai-012`

- **case_id**：`heldout-ai-012`
- **论文版本**：`2610.19432v1`
- **英文原题**：What exchange rates, currency-normalization dates, and inflation adjustments were applied when comparing numerical facts reported in different currencies across the 90 issuers?
- **准确中文翻译**：在比较 90 家发行人以不同货币报告的数值事实时，采用了哪些汇率、货币归一化日期和通胀调整？
- **通俗问法**：简单说：论文有没有说明不同货币之间到底用什么汇率、哪一天换算、是否做通胀调整？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目要求的可重建答案）。
- **证据页码**：17, 20
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - No external rates or economic data may be introduced to fill this absent method.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 13. `heldout-ai-013`

- **case_id**：`heldout-ai-013`
- **论文版本**：`2610.19223v1`
- **英文原题**：How does the definition-grounding process repeatedly judge and revise Lean definitions, and how are clarified informal definitions and unproved basic properties used at different stages of that process?
- **准确中文翻译**：定义落地过程如何反复判断并修订 Lean 定义？经过澄清的非正式定义和未经证明的基本性质，又分别在该过程的哪些阶段发挥作用？
- **通俗问法**：简单说：Lean 定义不合格时怎么循环修改？清理后的自然语言定义和后续未证明的基本性质分别什么时候加入？
- **按 `reference_answer` 翻译的中文候选答案**：定义落地循环会生成下一个 Lean 库状态，并询问 LLM 审查者这些定义能否继续兼容未来陈述；如果得到 False，就重构或重新生成该状态，直到审查者返回 True，之后才继续。ReuseBench 在生成阶段提供清理后的非正式定义：每个定义只介绍数学对象，并把捆绑的主张拆开，同时明确缺失的假设、约定和边界情况。在审查实验中，J0 接收 Lean 定义及其非正式定义，不接收下游证据。随后，标准代理会陈述而不证明下游陈述，其中 S1 包含基本性质。之后的 Ji 接收已经形式化的 S1…Si 及其对应非正式陈述，把观察到的兼容性作为未来可用性的证据。因此，清理生成器输入和向后续审查者加入未经证明的下游证据属于不同阶段，不能互相替代。
- **证据页码**：3, 4, 5, 7
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - The PDF text is multi-column and interleaved in places; supporting clauses are intentionally split into separately verifiable contiguous anchors.
  - The paper evaluates the proposed loop experimentally but does not report running the full iterative repair loop end-to-end as a production system.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["1-9", "12-14"], "bibliography_pages_skimmed": ["10-11"], "rendered_pages_checked": [4, 5, 7], "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 14. `heldout-ai-014`

- **case_id**：`heldout-ai-014`
- **论文版本**：`2610.19223v1`
- **英文原题**：What fraction of generated definition sets was compatible with all later statements for each evaluated model and experimental condition, and how much did clarification of the informal definitions change those rates?
- **准确中文翻译**：对于每个被评估模型和实验条件，有多少比例的生成定义集与所有后续陈述兼容？澄清非正式定义使这些比例发生了多大变化？
- **通俗问法**：简单说：原始定义和澄清后的定义，最终能一路通过后续陈述的比例分别是多少，提升了多少？
- **按 `reference_answer` 翻译的中文候选答案**：“全部通过”表示与每一层后续内容都兼容。Source 条件下：GPT 6 Astra 43.8%、GPT 5.6 Sol 31.2%、Claude Fable 5.1 50.0%、Claude Opus 5 50.0%、Claude Sonnet 5 25.0%，所有模型平均 40.0%。Cleaned 条件下：相应为 81.2%、75.0%、100.0%、87.5%、43.8%，所有模型平均 77.5%。Cleaned 减去 Source 的变化分别为 +37.4、+43.8、+50.0、+37.5 和 +18.8 个百分点；所有模型平均提高 +37.5 个百分点。
- **证据页码**：6, 7
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - Percentage-point changes are arithmetic differences computed from Table 2, not separately printed delta values.
  - Rates are averaged over eight theories and two generator repeats, as specified by the paper.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["4-8"], "rendered_pages_checked": [], "table_text_sufficient": true, "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 15. `heldout-ai-015`

- **case_id**：`heldout-ai-015`
- **论文版本**：`2610.19223v1`
- **英文原题**：How was the cost of stating basic properties without proving them measured, and what detection-quality gains and formalization-cost trade-offs were reported for every model or judge configuration?
- **准确中文翻译**：不证明基本性质、仅陈述它们的成本是如何衡量的？对于每个模型或审查者配置，论文报告了哪些检测质量提升和形式化成本权衡？
- **通俗问法**：简单说：只写出基本性质、不写证明要花多少钱？加入不同层的证据后，检测效果和成本怎样变化？
- **按 `reference_answer` 翻译的中文候选答案**：论文把成本衡量为陈述 S1 的平均货币形式化成本（美元），而不是证明 S1 的成本：标准下游代理只陈述命题、不提供证明。审查实验使用一次调用的 GPT 5.6 Sol 审查者，对每个 J0…J4 配置重复调用 5 次。Table 3 的列顺序为 Baseline（始终 True）、D/J0、+S1/J1、+S2/J2、+S3/J3、+S4/J4；准确率为 0.62/0.65/0.85/0.82/0.82/0.81，precision-recall AUC 为 0.62/0.79/0.85/0.85/0.84/0.87，precision 为 0.62/0.78/0.86/0.82/0.86/0.86。形式化成本行（美元）为 –/$4.32/$0.27/$0.30/$0.24/$0.26；重复 5 次的审查者成本行为 –/$0.17/$0.21/$0.27/$0.32/$0.35。这些是各层报告的形式化成本和特定上下文的审查成本，并不表示后续配置都具有相同的累计成本。加入未经证明的 S1 证据后，准确率从 0.65 提升到 0.85，AUC 从 0.79 提升到 0.85：S1 形式化加 5 次 J1 调用的成本为 $0.27+$0.21=$0.48，约为定义和辅助函数形式化成本 $4.32 的 11%，并在后续层之前标出 73% 的不兼容候选。加入 S2…S4 并没有进一步明显提高准确率，尽管 J4 的 AUC 最高，为 0.87。检测研究比较的是证据配置，而不是多个审查者模型。固定论文没有报告足以独立重算这些美元成本的 token 数量或计费/定价流程。
- **证据页码**：5, 7, 8, 9
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - All 14 fixed-paper page texts, including appendices, were searched for cost, USD, token, pricing and billing methodology. The paper reports average USD amounts but not token counts, a pricing schedule, or a procedure sufficient to independently recompute them; those dimensions remain not reported.
  - The question asks for every model or judge configuration: Table 3 varies J0…J4 evidence configurations of GPT 5.6 Sol, not a separate detection-quality/cost evaluation for every generator model in Table 2.
  - The reported formalization-cost row is not silently converted into cumulative costs for later J2…J4 contexts. The $0.48 S1 calculation includes five judge calls and is not the formalization-only $0.27 cost.
  - Multi-column text interference is retained in contiguous evidence sequences; the actual page 8 table and page 7 method were also rendered and read.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["7-9"], "rendered_pages_checked": [5, 7, 8], "table_text_sufficient": true, "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

## 1. `heldout-ai-016`

- **case_id**：`heldout-ai-016`
- **论文版本**：`2610.19223v1`
- **英文原题**：How does compatibility with later statements operationalize whether a formalization is reusable, and what limitations arise from the selected textbooks, mathematical fields, Lean libraries, or model-judge setup?
- **准确中文翻译**：与后续陈述的兼容性如何被操作化为判断某个形式化是否可复用的标准？所选教材、数学领域、Lean 库以及模型—评审器设置带来了哪些限制？
- **通俗问法**：它怎样用“后面的定理还能不能忠实地使用这些定义”来衡量可复用性？这个基准的材料、数学范围、Lean/Mathlib 环境和 自动化模型 评审设置又限制了结论能推广到哪里？
- **按 `reference_answer` 翻译的中文候选答案**：
  可复用性被操作化为“下游兼容性”，而不是证明成功：如果形式化某个后续陈述时，规范代理必须修改定义、增加包装器，或只能不忠实地表述该陈述，那么这个陈述就是一个 witness（反例见证）。代理需要声明这些变通方式，并据此统计受影响的陈述。L(D|S1…Si)=True 表示所纳入的各层中不存在 witness；完整的 L(D) 覆盖 S1…S5。这是一个受实验设计边界约束的、用于衡量 Lean 定义可复用性的代理指标。基准包含八个选定理论，主要来自数学/理论领域的入门教材，后面的一些层来自论文。它检查固定版本的 Mathlib：只有少量定义可以已有等价物，且 Mathlib 不能已经充分支持核心对象/结果，从而绕过所提交的定义来陈述和证明下游陈述；反过来，所需基础又必须在现有 Mathlib 的可达范围内，不能要求构建大规模缺失理论。下游代理是固定的 GPT 5.6 Sol，按顺序陈述各项而不进行证明；评审器研究也使用 GPT 5.6 Sol。因此，该研究并未证明可复用性能够推广到任意数学领域、不同 Lean/Mathlib 环境、实际的下游证明义务、其他代理或其他评审模型；这些是根据已记录设计得出的限制性推断，而不是分别测试得到的结果。
- **证据页码**：`4, 5, 6, 7`
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制**：
  - 兼容性是“忠实陈述不出现 witness”的标准，不是端到端定理证明保证。
  - 入门来源、八个理论、固定版本 Mathlib 的支持边界以及固定的 GPT 5.6 Sol 形式化器/评审器，都是已记录的选择或设置限制；关于可推广性的含义明确标为推断。
  - 所选来源和 Mathlib 相关段落跨越交错栏；原记录保留了连续引文中的全部过渡文本，并以第 4、5、7 页的渲染页交叉核对。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 14 页，已加载并检索第 1–14 页；重点细读第 3–9、12–14 页；渲染核查第 4、5、7 页；表格文本足够；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 2. `heldout-ai-017`

- **case_id**：`heldout-ai-017`
- **论文版本**：`2610.19223v1`
- **英文原题**：What exact formal criterion determines that a generated definition set is compatible with all later statements, and how is that criterion checked when ReuseBench imports and uses the definitions and helper lemmas?
- **准确中文翻译**：什么精确的形式标准决定生成的定义集合是否与所有后续陈述兼容？ReuseBench 导入并使用这些定义和辅助引理时，如何检查这一标准？
- **通俗问法**：怎样算“后面的所有陈述都能不出问题地使用这套定义”？ReuseBench 实际怎么检查？
- **按 `reference_answer` 翻译的中文候选答案**：
  形式上，L(Ḋ | S1,…,Si)=True 当且仅当 S1,…,Si 中没有任何陈述是针对 Ḋ 的 witness；因此完整标准 L(Ḋ)=L(Ḋ | S1,…,S5)=True 要求所有后续层都没有 witness。ReuseBench 先生成 Lean 定义和辅助引理，再由一个 canonical GPT 5.6 Sol agent 在累积的库之上，按顺序加入 S1…S5。如果它必须修改定义、增加包装器，或不忠实地陈述某个下游项目，就声明该变通方式；受影响的陈述会被机械记录为 W(s|Ḋ)=True 的 witness。这检验的是所提交定义和辅助引理在陈述下游结果时的导入/使用兼容性；实验并不要求证明这些下游陈述。
- **证据页码**：`5, 6`
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制**：
  - 基准检查下游陈述能否忠实且符合惯用方式地写出；这些陈述的证明被有意排除在实验之外。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 14 页，已加载并检索第 1–14 页；重点细读第 4–7 页；未进行渲染页核查；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 3. `heldout-ai-018`

- **case_id**：`heldout-ai-018`
- **论文版本**：`2610.19223v1`
- **英文原题**：How many expert Lean-reviewer hours were required per ReuseBench field to adjudicate semantic faithfulness independently of whether the generated code compiled?
- **准确中文翻译**：为了独立于生成代码是否编译成功来裁定语义忠实性，每个 ReuseBench 领域需要多少专家 Lean 评审小时？
- **通俗问法**：论文有没有报告每个领域花了多少专家时间来人工判断语义是否忠实，而且不把“能否编译”当成判断依据？
- **按 `reference_answer` 翻译的中文候选答案**：
  `reference_answer` 为 `null`；因此没有中文候选答案可翻译。
- **证据页码**：`6`
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制**：
  - 原记录扫描了全部 14 页（包括补充来源/许可证表），检索 hour、reviewer、semantic-faithfulness、compilation、expert、audit 等术语；没有找到评审小时数。
  - “未报告”仅限于完整固定版本论文；外部项目记录和代码/数据不在范围内。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 14 页，已扫描第 1–14 页标准化文本；重点细读第 1–9、12–14 页；浏览参考文献第 10–11 页；未进行渲染页核查；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 4. `heldout-ai-019`

- **case_id**：`heldout-ai-019`
- **论文版本**：`2610.19159v1`
- **英文原题**：How does Into the Unknown turn a source paper, an unpublished follow-up direction, and scientist-provided necessary conditions into an agent task and a rubric-based evaluation without a conventional reference answer?
- **准确中文翻译**：Into the Unknown 如何把一篇来源论文、一个未发表的后续研究方向以及科学家提供的必要条件，转化为一个 agent 任务和基于 rubric 的评估，而不依赖传统参考答案？
- **通俗问法**：它怎样把“原论文 + 科学家想做但尚未发表的后续方向 + 成功条件”包装成 自动化模型 可以执行、也可以按规则评分的任务，而不是拿标准答案对照？
- **按 `reference_answer` 翻译的中文候选答案**：
  每个实例包含原论文 P、其代码 C 和数据 D、任务说明 T、方向特定 rubric 以及完整性 rubric。科学家先提出一个据其所知尚未发表的后续方向，并说明什么条件下可算成功。一个 自动化模型 agent——在 claude-code-cli 中以最高 effort 运行的 Claude-Opus-5——起草 T 和两套 rubric；科学家随后审阅并编辑这些文件，附录 C 记录了 112 次编辑。执行任务的 agent 接收 P/C/D/T，并提交后续论文、代码和执行日志。它不使用传统参考解，而使用定制的叶级问题评估：完整性（C1：所有完整性节点）、完整度（C2：方向要求的交付物节点）以及相对于科学家标准的充分性（C3：成功条件节点）。两套 rubric 所形式化的全部条件都写在 T 中，因此该任务测试的是执行一个已披露的科学方向，而不是发现隐藏答案或隐藏要求。
- **证据页码**：`3, 5`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - “该方向未发表”以科学家所知范围为限定，不是本 annotator 独立完成的穷尽式新颖性检索。
  - 该记录描述论文协议和科学家编辑过程，不是对生成科学结果的独立验证。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已加载并检索第 1–163 页；重点细读第 1–7、15–18 页；未进行渲染页核查；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 5. `heldout-ai-020`

- **case_id**：`heldout-ai-020`
- **论文版本**：`2610.19159v1`
- **英文原题**：How did each of the nine evaluated agents perform across the submitted follow-up papers, including counts that satisfied all rubric conditions, passed the integrity check, or failed for unsupported work and evidence contradictions?
- **准确中文翻译**：九个受评估 agent 在所提交的后续论文中分别表现如何？请包括满足全部 rubric 条件、通过完整性检查、以及因无支持的工作和证据矛盾而失败的数量。
- **通俗问法**：九个 自动化模型 各提交了多少篇？每个 自动化模型 有多少篇完全过关、通过完整性检查、声称做完了但没有依据，或报告的数字与证据冲突？
- **按 `reference_answer` 翻译的中文候选答案**：
  每个 agent 提交 15 篇论文。按 Claude-Opus-4.8、Claude-Opus-5、Claude-Fable-5、Claude-Opus-5.5、GPT-5.6-Sol、GPT-6-Astra、DeepSeek-V4-Pro-0813、Kimi-K3、GLM-5.3 的顺序：严格的全部条件通过数对每个 agent 都是 0；严格完整性通过数分别为 0、0、0、4、6、11、0、0、0；被标记为虚假声称已完成工作的提交数分别为 9、11、9、2、4、1、9、12、11；被标记为报告数字与证据矛盾的提交数分别为 12、14、13、5、3、0、13、14、11。失败类别不是互斥的。
- **证据页码**：`6`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - Figure 3 的数字及行/列标签来自图像内容，而不是 pages.json 文本。证据保存了经 SHA256 固定的实际渲染左侧面板、分开的数字行锚点、原 PDF 边界框、准确 agent 顺序和单位；每个 evidence_quote 都是实际连续 caption，不是伪造的图像单元格文本。
  - 正文 Baselines 段落说有九个 agent，但只列出七个；Table 2 和渲染后的 Figure 3 提供了完整九 agent 名单。该内部不一致被保留，没有默默编造名称。
  - 失败类别不互斥。“虚假声称已完成工作”和“报告数字与证据矛盾”是来源中的命名类别，不能等同于所有完整性失败。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已加载并检索第 1–163 页；重点细读第 1–10 页；渲染核查第 6 页的 Table 2 和 Figure 3；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 6. `heldout-ai-021`

- **case_id**：`heldout-ai-021`
- **论文版本**：`2610.19159v1`
- **英文原题**：How were outcomes distributed across quantum computing, plasma physics, computational chemistry, materials science, and scientific machine learning, and which agent-domain combinations had the highest rates for each major failure category?
- **准确中文翻译**：结果如何分布在量子计算、等离子体物理、计算化学、材料科学和科学机器学习这五个领域？对于每个主要失败类别，哪些 agent—领域组合的比例最高？
- **通俗问法**：这五个领域各自有多少成功或失败？哪个 自动化模型 在哪个领域最容易出现每一类失败？
- **按 `reference_answer` 翻译的中文候选答案**：
  `reference_answer` 为 `null`；因此没有中文候选答案可翻译。
- **证据页码**：`4, 6`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - 全部 163 页文本都检索了按领域、领域级别以及五个领域名称；没有找到按领域交叉统计结果。
  - Table 1 的计数是每篇论文的任务领域，不是按领域的结果率。
  - Figure 3 做过视觉检查，但没有声称对 163 页 PDF 完成完整视觉验证。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已扫描第 1–163 页标准化文本；重点细读第 1–10、15–18 页；检索了 domain-wise/by-domain/domain-level 及五个领域名称；渲染核查第 6 页；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 7. `heldout-ai-022`

- **case_id**：`heldout-ai-022`
- **论文版本**：`2610.19159v1`
- **英文原题**：How do the lack of reference answers, use of unpublished research directions, and scientist-defined rubrics affect the validity, reproducibility, and generalizability of the benchmark conclusions?
- **准确中文翻译**：缺少参考答案、使用未发表研究方向以及采用科学家定义的 rubric，分别如何影响该基准结论的有效性、可复现性和可推广性？
- **通俗问法**：没有标准答案、方向还是未发表的、评分规则又由科学家定，这会让结论在“是否有效、能否复现、能否推广”上受到什么影响？
- **按 `reference_answer` 翻译的中文候选答案**：
  没有已解出的参考答案时，有效性是操作性意义上的，而不是对科学真值的证明：定制 rubric 检验的是科学家预先声明的必要条件，而且这些条件都披露在任务中。研究方向是“据科学家所知未发表”，并不独立保证其新颖性。专家元验证支持的是狭窄目标：Q1 对全部 41 个方向判断都实现了覆盖和一致。对于 Q2 的审稿人指出的弱点，按论文平均的覆盖率/一致率为 96.97%/80.34%，合并统计为 93.98%/83.20%；对于 Q3 的科学质量/影响，分别降为 89.05%/68.68% 和 75.34%/63.64%。因此，满足必要条件不等于总体科学价值。主评估对每个节点聚合四次 GPT-6-Astra 判断，采用 3-of-4 多数；2–2 平局判失败。在另一个包含 52 个提交、每个提交运行四次的稳定性研究中，GPT-6-Astra 与 Claude-Opus-5.5 的整体等级相同率为 86.5% 对 57.7%，G1 为 86.5% 对 69.2%，G1∩G2 为 100.0% 对 78.8%，最终 G1∩G2∩G3 为 100.0% 对 98.1%。作者明确提醒，后续 gate 的完美一致性部分反映了 GPT-6-Astra 对 G1∩G2 的判断普遍为负；一致性不等于正确性，观察到的 100% 也不保证新提交会有同样结果。可推广性受限于 15 篇经过有意选择、可用代码执行、与 DOE 相关的论文，受限于有一名参与的第一/末位作者科学家，也受限于所选 rubric、agent、harness 和评审器。其中有一篇没有国家实验室作者。八篇论文在 2024 年或更早已确立，七篇于 2026 年提交。合理的设计推断是，未发表方向可能减少直接抄袭已发表答案的机会，但论文没有证明不存在污染，也没有证明在所有科学领域都普遍有效。
- **证据页码**：`3, 4, 5, 6, 15, 16, 17`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - 有效性/可推广性以及降低直接抄袭机会，都是根据已记录设计作出的、且原记录明确标为推断；不能据科学家限定的“未发表方向”推断出独立的新颖性或无污染审计。
  - 52 个提交的重复判断研究与主研究的 135 个提交没有混淆。Table 2 分别报告 G1、G2、G3；单独通过 G2 不自动等于通过 G1∩G2。
  - 附录 B 明确说明，后续 gate 的完美一致性部分归因于 GPT-6-Astra 的 G1∩G2 判断普遍为负；这不等于科学判断完美，也不保证新输入上的可复现性。
  - 专家比较衡量的是在已覆盖批评中的匹配程度；对于总体科学质量/影响，其力度弱于对既定方向目标的判断。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已加载并检索第 1–163 页；重点细读第 1–10、15–18 页；渲染核查第 6 页；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 8. `heldout-ai-023`

- **case_id**：`heldout-ai-023`
- **论文版本**：`2610.19159v1`
- **英文原题**：For one evaluated submission, trace a claimed scientific contribution to the recorded evidence, the integrity-check decision, the relevant necessary conditions, and the final rubric assessment.
- **准确中文翻译**：对于一个受评估的提交，请把其声称的科学贡献追溯到记录中的证据、完整性检查决定、相关必要条件以及最终 rubric 评估。
- **通俗问法**：选一篇提交，看看它声称做出了什么、证据记录了什么、完整性检查怎么判、满足哪些必要条件，以及最后 rubric 到底怎么评。
- **按 `reference_answer` 翻译的中文候选答案**：
  `reference_answer` 为 `null`；因此没有中文候选答案可翻译。
- **证据页码**：`6, 48, 128, 129, 130, 131`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - 全部 163 页文本都扫描了 P9、kernel_plan、Claude-Fable-5 轨迹、完整性/rubric 决定和保存的 completeness 结果；本次修订重读了第 47–49、127–132 页。固定论文包含轨迹摘录和聚合结果，但不包含 P9 完整性叶子级“证据→决定”记录。
  - 聚合的 G1 失败不能证明某个具体科学主张或遗漏实验导致了该完整性失败；直接记录的联系是 completeness，而不是凭空添加的 integrity leaf。
  - 部分追踪保留已报告内容，不把缺失的 grader 记录细节转换为“没有该材料”，也不声称科学主张为真。外部代码和基准工件仍不在范围内。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已加载并检索第 1–163 页；重点细读第 1–10、15–18、127–132 页；渲染核查第 6 页；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 9. `heldout-ai-024`

- **case_id**：`heldout-ai-024`
- **论文版本**：`2610.19159v1`
- **英文原题**：What experimental laboratory budget and elapsed time would be required to physically replicate and validate every proposed follow-up result produced in the benchmark?
- **准确中文翻译**：要在实体实验室中复现并验证该基准产生的每一个后续研究结果，需要多少实验预算和耗时？
- **通俗问法**：如果真的做实验来复现所有后续结果，要花多少钱、多久？
- **按 `reference_answer` 翻译的中文候选答案**：
  `reference_answer` 为 `null`；因此没有中文候选答案可翻译。
- **证据页码**：`2, 4, 5`
- **source_pdf_sha256**：`69baf9bc753f5f57de496d45a74faee9f3afaee13df50e7b8228db1a9aa769fb`
- **原记录限制**：
  - 全部 163 页文本都检索了 laboratory budget、physical replication、elapsed time、validation budget、wall-clock 等词及相关表述。论文包含物理科学任务描述，但没有规定汇总的实体复现实验方案、成本或时长。
  - 没有科学上有依据的方式把计算小时数换算成实验室资源。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：共 163 页，已加载并检索第 1–163 页；重点细读第 1–10、15–18 页；检索了相关预算、实体复现、验证耗时、wall-clock、compute resources、no time limit 等词；未进行渲染页核查；完整视觉覆盖未验证；排除外部代码或数据。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 10. `heldout-ai-025`

- **case_id**：`heldout-ai-025`
- **论文版本**：`2610.19398v1`
- **英文原题**：How do RCER’s inter-round relational evidence acquisition, reasoning-context reconstruction, and cross-evidence compositional reasoning interact during iterative retrieval and answer derivation?
- **准确中文翻译**：RCER 的轮间关系证据获取、推理上下文重建和跨证据组合推理，在迭代检索和答案推导过程中如何相互作用？
- **通俗问法**：RCER 每轮怎样利用已有证据继续找资料、重建实体和关系，最后又怎样把不同证据拼成答案？
- **按 `reference_answer` 翻译的中文候选答案**：
  RCER 将迭代获取阶段与最终推理上下文阶段、答案组合阶段分开。在检索轮次 t，控制器分析问题、已累积的段落 A_t、最新批次以及查询历史 H_t，重建实体对应关系和未解决关系，再把该分析转成下一条查询。新段落按检索顺序追加，下一轮会重新分析扩大的候选池。获取过程受段落预算 B 限制，也可以在查询重复时提前停止。检索完成后，独立的 LLM selector 对最终候选池 A_T 重新分析，并联合输出分析 z 和 K 个选中段落索引 J；它利用跨证据分析选择互补来源。选中文本 S 提供事实前提，而 z 可以保留整个候选池（包括未选段落）中的推断。答案生成器同时以 S 和 z 为条件，对照选中文本检查实体对应关系，使属性继续附着于其来源实体，保持关系方向，并组合这些关系形成答案。
- **证据页码**：`3, 4, 11`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - 页面文本提取会交错两栏；每组证据上下文把一个连续原文跨度完整分割成不超过 60 词的锚点，未静默删除无关的另一栏文本。
  - 该流程是论文所述的 LLM/controller/selector 协议，不是对外部代码的断言。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 11. `heldout-ai-026`

- **case_id**：`heldout-ai-026`
- **论文版本**：`2610.19398v1`
- **英文原题**：What EM and F1 results did RCER and each baseline achieve on each of the three multi-hop QA benchmarks, and how were the reported average improvements computed?
- **准确中文翻译**：RCER 和每个 baseline 在三个多跳 QA 基准上的 EM 与 F1 结果分别是多少？论文报告的平均提升是如何计算的？
- **通俗问法**：在 HotpotQA、2WikiQA、MuSiQue 上，所有方法的 EM/F1 分数是多少？RCER 比最强 baseline 高多少，怎么算出来的？
- **按 `reference_answer` 翻译的中文候选答案**：
  在 HotpotQA / 2WikiQA / MuSiQue 上，Table 2 报告的（EM，F1）为：Qwen3.8-27B：22.80/32.23、29.60/34.34、7.50/16.39；NV-Embed-v2：63.90/76.15、55.50/61.21、38.10/48.17；HippoRAGv2：66.10/77.97、60.50/67.69、38.00/48.18；OriginRAG：68.00/80.23、62.50/69.79、43.80/54.84；LogicRAG：64.80/77.59、61.60/67.44、40.60/50.97；RankCoT：64.20/76.93、60.70/68.09、40.10/52.97；IRCoT：65.00/78.31、65.80/75.98、39.90/48.24；RetroRAG：67.40/80.77、70.50/77.48、52.30/62.75；MGranRAG：67.40/80.04、72.00/79.27、49.70/58.63；STRIDE：63.10/76.13、68.30/77.14、46.00/57.43；CIRAG：58.70/72.39、70.40/79.28、46.50/57.13；S2G-RAG：67.30/79.74、71.20/78.63、51.80/61.42；K自动化模型R：67.60/79.84、69.10/77.06、49.10/60.06；RCER：68.60/81.46、74.90/82.20、55.30/66.52。RCER 在三个数据集上的宏平均为 EM 66.27、F1 76.73。报告的提升是把每个指标与最强 baseline 的宏平均比较：S2G-RAG 的 EM 为 63.43，因此提升 2.83；RetroRAG 的 F1 为 73.67，因此提升 3.06。
- **证据页码**：`5, 6`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - 本题只询问三个多跳基准，因此数字列表排除了 NarrativeQA 和 StrategyQA 列。
  - 数值按论文显示为百分比/百分点。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 12. `heldout-ai-027`

- **case_id**：`heldout-ai-027`
- **论文版本**：`2610.19398v1`
- **英文原题**：What results and ablations isolate RCER’s gains on narrative and implicit-reasoning tasks, including the separate contributions of relational acquisition, context reconstruction, and compositional reasoning?
- **准确中文翻译**：哪些结果和消融实验分离出了 RCER 在叙事任务和隐式推理任务上的增益？其中关系获取、上下文重建和组合推理分别贡献了什么？
- **通俗问法**：在 NarrativeQA 和 StrategyQA 上，去掉 RCER 的三个组件后分数掉多少？哪个组件最重要？
- **按 `reference_answer` 翻译的中文候选答案**：
  在叙事任务 NarrativeQA 上，完整 RCER 得分为 23.10 EM / 39.83 F1。去掉关系获取后为 21.80/38.14（EM −1.30、F1 −1.69）；去掉上下文重建后为 20.10/35.01（−3.00、−4.82）；去掉组合推理后为 22.40/38.59（−0.70、−1.24）。在隐式推理 StrategyQA 上，完整 RCER 的准确率为 87.40%；相应消融为 86.00（−1.40 个百分点）、79.90（−7.50）和 81.20（−6.20）。因此，上下文重建在两个任务上的贡献都最大；在 StrategyQA 上组合推理居次，关系获取带来较小但为正的下降。
- **证据页码**：`6`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - 论文把 StrategyQA 称为隐式推理任务，并报告 accuracy，而不是 EM/F1。
  - 由于表格值显示到两位小数，四舍五入后的相减结果可能与正文描述相差 0.01。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 13. `heldout-ai-028`

- **case_id**：`heldout-ai-028`
- **论文版本**：`2610.19398v1`
- **英文原题**：How does reconstructing entity correspondences and inferential dependencies address failures that emerge as evidence accumulates, and what remaining failure cases or limitations does the paper report?
- **准确中文翻译**：重建实体对应关系和推理依赖如何应对证据不断累积时出现的失败？论文还报告了哪些剩余失败情形或限制？
- **通俗问法**：资料越找越多时，RCER 怎样避免把事实错配到错误实体或关系上？它自己还承认哪些问题？
- **按 `reference_answer` 翻译的中文候选答案**：
  随着证据累积，新段落可以解决早先事实涉及哪些实体，以及这些事实如何支持后续查询；因此 RCER 在获取阶段重新解释历史，而不是只把历史留给最终选择。检索后，独立 selector 重新分析完成的候选池，把互补证据连接成推理上下文。在答案组合时，实体对应关系使一个段落中的关系可以成为另一个段落的前提；对应关系会对照选中文本检查，属性继续附着于其来源实体，关系方向得到保持。这回应了论文的警告：迭代检索可能已经取得所需事实，却仍保留错误的关系解释，并被答案一致性进一步强化。在 Houston 地质示例中，RCER 识别出 erosion，而 STRIDE 追问城市地基变化，RetroRAG 则在同意 salt-dome formation 后停止；这些是对比方法的失败示例，不是 RCER 残余错误率分类。剩余限制包括：保留证据与组合事实之间仍有差距；检索内容和生成答案可能有事实错误；以及多轮过程中推理质量与计算成本之间的权衡仍未解决。
- **证据页码**：`4, 5, 6, 7, 9, 11, 14`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - Houston 示例说明的是对比方法的失败，不是 RCER 的量化失败率。
  - 交错页面文本被保留在完整的上下文组中；所有必要限定都分布在各组连续证据中，而不是从孤立片段推断。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 14. `heldout-ai-029`

- **case_id**：`heldout-ai-029`
- **论文版本**：`2610.19398v1`
- **英文原题**：What scoring or selection objective is used to jointly choose complementary passages, derive cross-evidence inferences, and establish entity correspondences, and how are its terms applied over successive retrieval rounds?
- **准确中文翻译**：论文使用什么评分或选择目标来联合选择互补段落、推导跨证据推断并建立实体对应关系？这些项如何应用于连续的检索轮次？
- **通俗问法**：它有没有一个把互补性、推理和实体匹配加权成分数的目标函数？每轮检索和最后选段分别怎么做？
- **按 `reference_answer` 翻译的中文候选答案**：
  论文没有定义一个带有独立互补性、推断和实体匹配项的加权标量分数。它定义了一个 LLM 选择映射 x_s=(q, A_T, K)，(z,J)=S_θ(x_s)，其中 A_T 是最终候选池，z 是跨证据分析，J 包含 K 个选中段落的索引。公式 (6) 将联合生成分解为 p_θ(z,J|x_s)=p_θ(z|x_s)p_θ(J|z,x_s)：模型先生成分析，再以该分析为条件选择段落。该分析建立实体对应关系和关系依赖；selector 利用段落的互补角色满足问题的信息需求。这不是每轮重新优化的 selector 目标。轮间控制器反而根据不断扩大的段落池和查询历史重建关系，以生成下一条查询。在所述配置中，它从 4 个段落开始，最多再进行 4 轮、每轮增加 2 个段落，候选池上限为 12 个；查询词集合的 Jaccard 相似度达到 0.95 时可以停止。只有获取阶段结束后，独立的 final selector 才返回分析和 5 个互不相同且递增的索引。单独的 answer call 接收这些段落和 z，其中 z 可以包含从未选候选段落中保留的关系。
- **证据页码**：`3, 4, 11`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - 该分解描述的是 LLM 输出生成，不是经过测量的优化损失或数值排名分数。
  - 这些配置值是论文所述实验设置，不是抽象形式中 K 个段落的普遍要求。
  - 完整的 selector 和循环周围文本按有序上下文组保留，尽管原文存在两栏提取问题。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## 15. `heldout-ai-030`

- **case_id**：`heldout-ai-030`
- **论文版本**：`2610.19398v1`
- **英文原题**：What inter-annotator agreement was measured for human labels of the entity correspondences and inferred cross-evidence relations used in the evaluation?
- **准确中文翻译**：对于评估中使用的实体对应关系和推断跨证据关系的人类标注，测量了什么标注者间一致性？
- **通俗问法**：论文有没有让人给实体对应关系和跨证据关系做人工标注，并报告标注者之间的一致率？
- **按 `reference_answer` 翻译的中文候选答案**：
  `reference_answer` 为 `null`；因此没有中文候选答案可翻译。
- **证据页码**：`3, 4, 5, 12`
- **source_pdf_sha256**：`bbd29e710cda3569416d44e0a863a90d587f198640a9d84312e31ef586ff0df7`
- **原记录限制**：
  - 实体对应关系和跨证据关系是模型生成的中间分析，不是论文发布的人类标注评估集。
  - 该缺失结论来自完整文档审阅，而不是一次关键词漏搜。
  - 补充的上下文锚点说明了评估内容和中间关系的生成方式；没有任何一处被当成“明确写着不存在 IAA 研究”的原文。
  - 现有数据集的 supporting-passage 标注用于证据召回，这不同于对内部实体对应关系和跨证据关系进行人工标注。
  - 原记录的 自动化模型 阅读范围并非完整视觉审计：完整固定版本论文含附录，共读第 1–14 页；不使用外部代码/数据；视觉核查了第 4 页公式与选择分解、第 6 页 Table 2 和 Table 3。
  - 参考答案由 自动化模型 编写；没有经过认证的人类复核，也不构成生产资质。

---

## heldout-ai-031

- **case_id**：`heldout-ai-031`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：How does the document-aware generation pipeline profile intelligence reports before generating questions, and how are expert annotation and the LLM-as-judge protocol used to validate the resulting benchmark?
- **准确中文翻译**：文档感知的生成流水线在生成问题前如何对情报报告进行画像？又如何利用专家标注和 LLM-as-judge 协议验证所得基准？
- **通俗问法**：这条问题想知道：系统先怎样把报告的范围、重点和可回答边界整理出来，再怎样自动出题，并用专家和 自动化模型 评分确认题目与答案是否靠谱。

### 按 `reference_answer` 翻译的中文候选答案

报告首先用八个语义探针对其进行画像：范围、关键发现、参与者、地点、时间线、风险、建议和定量主张；每个探针检索 top-4 chunks。随后通过一次调用，将代表性 chunk 集合转换为文档类型、用途，以及明确的 `can_answer`/`cannot_answer` 范围。出题阶段接收该画像、这些 chunks 和 16 个分析师 persona；系统只选择有证据支持的 persona，最多生成 20 个原子问题，标注为事实型、推理型、比较型或风险型，并附理由和来源 chunk grounding 指针。对每个问题重复执行 hybrid top-8 检索；LLM 只能依据这些 chunks 作答，推理型/风险型答案加入行内引用和情态限定，必要时返回 `INSUFFICIENT_CONTEXT`。

验证使用六个维度、每项 1–3 分：groundedness、文档用途匹配度、原子性、清晰度、答案忠实性和总体评价。三名领域分析师各评估约 150 个条目，共享 100 条重叠题块，并各自评估约 50 条独有题，因此共有 250 个不同条目。在重叠题块上，成对平均的二次加权 Cohen’s κ 为 0.24，三方完全一致率为 63%，至少 2/3 一致率为 99%。候选评审器 GPT-5.4、Claude Sonnet 5 和 DeepSeek-V3.2 使用同一量表；最终选用 GPT-5.4，它相对于 A1、A2、A3 的六维合并完全一致率分别为 80%、63% 和 75%。在 250 条人工标注题上，论文报告 low-effort 评分与默认评分在完全一致率和 macro-F1 上等效，总体均值仍为 2.5。之后，GPT-5.4 通过 Batch API 进行 low-effort 评分，为全部 5,525 条题返回有效分数，没有拒答或丢失的评审调用。

- **证据页码**：4, 5
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - The human sample has 250 distinct items but only 100 three-way-overlap items.
  - The annotators are authors/colleagues, not an independent panel; the modest agreement limits how strongly LLM-as-judge validation can be interpreted.
  - The paper states statistical equivalence for low effort but does not supply an equivalence margin or test statistic in the cited passage.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-032

- **case_id**：`heldout-ai-032`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：How are the 5,525 question-answer pairs distributed across the 415 reports, the four question categories, document lengths, publishers, and geopolitical regions?
- **准确中文翻译**：5,525 个问答对如何分布在 415 份报告、四类问题、文档长度、发布方和地缘政治区域之间？
- **通俗问法**：这条问题想知道问答题主要来自哪些报告和题型，以及论文明确报告了哪些分布、哪些分布其实没有给出。

### 按 `reference_answer` 翻译的中文候选答案

论文报告了 5,525 个问答对，覆盖 413 份具有可检索 chunks 的报告（收集的 415 份中跳过了 2 份），按使用的报告计算平均每份 13.4 个问答对。按问题类型分为：事实型 3,805（68.9%）、推理型 789（14.3%）、比较型 696（12.6%）、风险型 235（4.3%）。

在 415 份报告的机构类别分布中，regional/local think tanks 为 129；specialised domains 为 64；official & multilateral 为 63；global think tanks 为 51；index & data-narrative 为 31；parliamentary services 为 29；humanitarian 为 20；academic policy 为 15；human rights 为 13，共来自 122 个发布方。论文没有提供逐报告问答数量、报告长度分布、逐发布方数量或实际地缘政治区域分布。论文只报告了 chunk 长度：55,353 个 chunks，平均 1,138 个字符，中位数 723 个字符（约 285/181 个 token）；这些数值不能替代文档长度。

- **证据页码**：2, 4
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - Institutional category is not equivalent to geopolitical region.
  - The mean 13.4 supports only an aggregate average; it does not determine the per-report distribution.
  - Chunk character/token lengths are not report lengths.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-033

- **case_id**：`heldout-ai-033`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：What retrieval scores did all 15 systems obtain at document and chunk level, and what exact evidence shows that bge-m3 matched larger encoders and outperformed the evaluated commercial embedding APIs?
- **准确中文翻译**：全部 15 个系统在文档级和 chunk 级取得了哪些检索分数？哪些确切证据表明 bge-m3 达到了更大编码器的水平，并优于所评估的商业 embedding API？
- **通俗问法**：这条问题要求把 15 个检索系统的表格分数列全，再说明 bge-m3 和大模型、商业 API 的具体比较证据。

### 按 `reference_answer` 翻译的中文候选答案

表 6 的列为 `doc R@1`、`doc R@10`、`chunk R@10`、`chunk nDCG@10`、`chunk MRR@10`。各系统分数如下（按上述列顺序）：

- BM25：0.840 / 0.974 / 0.877 / 0.787 / 0.801
- bge-m3：0.836 / 0.974 / 0.885 / 0.785 / 0.793
- Qwen3-Emb-8B：0.839 / 0.975 / 0.892 / 0.784 / 0.788
- me5-large：0.831 / 0.974 / 0.884 / 0.778 / 0.786
- e5-large-v2：0.828 / 0.970 / 0.870 / 0.761 / 0.768
- e5-base-v2：0.811 / 0.969 / 0.868 / 0.756 / 0.761
- Qwen3-Emb-0.6B：0.815 / 0.963 / 0.854 / 0.738 / 0.743
- bge-large：0.808 / 0.963 / 0.831 / 0.723 / 0.735
- gte-base：0.792 / 0.957 / 0.836 / 0.720 / 0.726
- Open自动化模型-3-large：0.777 / 0.944 / 0.845 / 0.717 / 0.715
- gte-large：0.792 / 0.956 / 0.825 / 0.711 / 0.718
- bge-base：0.788 / 0.955 / 0.816 / 0.702 / 0.713
- mistral-embed：0.778 / 0.944 / 0.831 / 0.701 / 0.699
- Open自动化模型-3-small：0.769 / 0.941 / 0.834 / 0.700 / 0.695
- MiniLM-L6：0.738 / 0.930 / 0.743 / 0.629 / 0.637

在 chunk nDCG@10 上，bge-m3（568M）基本匹配 Qwen3-Emb-8B：0.785 对 0.784，差值 Δ=0.001，95% CI 为 [−0.007, 0.008]。它超过了所有被评估的商业 API；与 Open自动化模型-3-large 相比，差距为 0.068，95% CI 为 [0.059, 0.076]。

- **证据页码**：6, 7
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - BM25 and e5 components are marked in-construction and are shown for reference, not ranked as winners.
  - Significance claims concern chunk nDCG@10 over cited-gold queries, not every metric.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-034

- **case_id**：`heldout-ai-034`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：How do report heterogeneity, analytical complexity, question-generation choices, and the mix of expert and model-based validation limit conclusions about real-world geopolitical intelligence analysis?
- **准确中文翻译**：报告异质性、分析复杂度、出题选择，以及专家与模型验证的组合，如何限制我们对现实世界地缘政治情报分析的结论？
- **通俗问法**：这条问题不是问系统得分，而是问：数据来源、题目生成、人工/自动化模型 评测和实验范围有哪些地方会让结论不能直接代表真实情报工作。

### 按 `reference_answer` 翻译的中文候选答案

语料异质性是范围限制，不是具有代表性覆盖的证据：报告来自不同机构视角，分析目的也不同；同时，这个语料库受英语语言、94% 的报告自 2022 年以来发布、可访问性和来源选择影响，不能代表全球或历史上的地缘政治分析。来源材料本身带有框架和偏见，也没有被验证为关于事件的 ground truth。

即使检索成功，分析综合仍然困难：在 250 个已标注条目的 gold-context reader 评估中，最佳 reader 的答案在 1–3 分量表上总体得分 ≤2 的比例为：事实型 28%、比较型 38%、推理型 47%、风险型 70%。这些是该协议下评出的答案质量比例，不是现实世界情报错误发生率。问题生成和作答由模型介导；事实型问题占多数，既反映文档本身提供的内容，也反映流水线设计。

验证使用了规模相对较小、且一致性一般的人类样本；标注者是参与设计该流水线的作者/同事，可能使质量判断偏高。reader 每个条目只解码一次，每个答案也只由一个经过验证的 judge 评分，而非评审小组；bootstrap 区间覆盖的是条目差异，不包括解码或 judge 的随机性。gold 由 LLM 撰写的参考答案所引用的 chunks 构成，因此继承了生成器的引用偏差。最后，研究覆盖的是第一阶段检索和单次阅读，不包括 reranker、迭代式/agentic 检索或长上下文 reader。这些边界限制了向实际运行中的地缘政治情报分析外推。

- **证据页码**：3, 5, 6, 7, 8, 9
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - The flawed-rate definition includes both score-1 and score-2 answers; it is not a decomposition into only severe hallucinations, refusals, or factual errors.
  - Correctness in the reader benchmark is exploratory; faithfulness and overall are the human-validated dimensions.
  - The low human agreement and possible author involvement bias are limitations of validation, not evidence that every benchmark label is wrong.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-035

- **case_id**：`heldout-ai-035`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：For one inferential or risk-oriented benchmark item, trace the question from the report profile through its gold evidence, document- and chunk-retrieval judgments, and answer-generation scoring.
- **准确中文翻译**：请追踪一个推理型或风险型基准问题：它如何从报告画像经过 gold evidence、文档级和 chunk 级检索判断，最终到达答案生成评分？
- **通俗问法**：这条问题想沿着一个具体风险题，看看论文到底展示了哪些证据和评分步骤，以及哪些逐题细节没有公开。

### 按 `reference_answer` 翻译的中文候选答案

可以对表 10 中 Geopolitical Researcher 的风险型条目作部分追踪：“该文档认为美国与刚果民主共和国（DRC）和乌克兰就关键矿产达成的双边协议带来了什么具体风险？”论文给出的参考答案是：这些协议因潜在的安全援助与资源获取挂钩而被批评为不对称，并引用了 `[1]`。附录 D 说明，类似 `[1]` 的方括号标记指向该报告检索到的 chunk 集合，被引用的 chunks 构成 answer-cited gold。这表明 `[1]` 是所展示答案的 gold-evidence 指针，而不是底层 chunk 的文本，也不是全局唯一 ID。

在一般协议层面，文档检索在某个 source-document 的 chunk 出现在 top-k 时给该文档计分；chunk 检索则以答案引用的 chunks 为判断依据，并对内容 hash 相同的 chunks 计分。reader judge 在 gold context 下，以 1–3 分评估 faithfulness、correctness 和 overall，其中参考答案只用于 correctness 比较。但是，固定 PDF 没有打印该条目的实际报告画像、底层 `[1]` chunk 的文本/唯一 ID、文档级或 chunk 级排序检索结果/判断、生成的 reader 回答或该 reader 的单项分数。因此，完整的端到端逐项追踪没有被报告；画像和评分提示只是模板，汇总表也不能填补这些条目级缺口。

- **证据页码**：6, 12, 13, 14
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - This is a partial answer. Generic retrieval/scoring rules are not evidence of the displayed item’s actual retrieved ranks, judgments, generated reader output, or score.
  - Citation [1] is a local excerpt pointer, not the report text or a globally unique gold-chunk identifier.
  - The sample reference answer reports a criticism; it is not independently verified as a claim about the deals.
  - External released per-item artifacts remain outside the complete fixed-PDF scope.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目“部分可答、明确保留逐题缺口”的 自动化模型复核结论？

---

## heldout-ai-036

- **case_id**：`heldout-ai-036`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：What publisher-specific permissions were obtained to redistribute the complete text of each of the 415 intelligence reports as part of the benchmark?
- **准确中文翻译**：为了将 415 份情报报告的完整文本作为基准的一部分重新分发，研究者获得了哪些逐发布方的许可？
- **通俗问法**：这条问题询问论文有没有逐家出版社的完整文本再分发许可清单。

### 按 `reference_answer` 翻译的中文候选答案

由于 `reference_answer` 为 `null`，原记录没有提供可翻译的候选答案。

- **证据页码**：10
- **source_pdf_sha256**：`1f159a5296d9203b722d7a94142d836ddc0c3ef9fbfb0dc9f8bb5f79d878ea5a`
- **原记录限制**：
  - No publisher-by-publisher license or permission matrix is present.
  - The collection may be shared on request, but that is not evidence that complete-text redistribution permission was obtained from each publisher.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pdf_pages_read": "1-15 inclusive", "appendix_included": true, "external_code_or_data_used": false, "source_artifacts": ["source.pdf", "pages.json", "source.json"], "visual_checks": ["page 2 Table 1", "page 4 Table 2 and pipeline", "page 7 Tables 6–8", "page 13 Table 10 and judge prompts"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该题作为“不可回答”负例，并保留其 自动化模型复核记录 verdict？

---

## heldout-ai-037

- **case_id**：`heldout-ai-037`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：How does KnowForge divide work among the knowledge-point index, Locator agent, bridge judge, bounded evidence pool, Deep Reader, synthesizer, and format checker, and what information can each component access?
- **准确中文翻译**：KnowForge 如何在 knowledge-point index、Locator agent、bridge judge、有界 evidence pool、Deep Reader、synthesizer 和 format checker 之间分配工作？每个组件可以访问哪些信息？
- **通俗问法**：这条问题要求用流程图式的方式说明每个 KnowForge 组件做什么、能看到什么、不能看到什么。

### 按 `reference_answer` 翻译的中文候选答案

KnowForge 是一条由代码调度、访问边界彼此分离的链路。离线构建阶段把 PDF/HTML/Markdown 转换为有类型的文本、图像、表格和示例片段，加入 VLM 描述、原子知识点和有类型关系，并建立 hybrid retrieval。Locator 只能看到问题、语料概览以及 search/list/table 工具，不能阅读内容。由代码调度的 bridge judge 接收问题、已经搜索过的查询和所收集材料的前 8K 个字符；它检查每一跳，指出缺失的 bridge entity，并且最多发起两次后续搜索，这些搜索会与已有查询去重。

排名后的列表被去重并合并为一个有类型的 30K-token pool，其中包括 3K 图像、3K 表格、2K 示例，其余为文本。Deep Reader 可以看到问题和编号后的 pool，但看不到 Locator 对话；它拥有只读的上下文/媒体工具，没有搜索工具，其输出会返回 pool。synthesizer 根据 pool 和 reader notes 起草答案。format checker 只看到问题和草稿，看不到证据，并负责规范化基准格式。

- **证据页码**：3, 4, 5, 13, 15
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - Page 4 is two-column interleaved; exact evidence is split rather than silently reconstructed.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 3, 4, 6, and 16 rendered with PyMuPDF and visually inspected; the Figure 1 pipeline and Tables 2, 3, and 6 were mapped against pages.json. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-038

- **case_id**：`heldout-ai-038`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：What results did KnowForge obtain on M3DocVQA under each reported model configuration, and how do those results compare with M3DocRAG and one-shot retrieve-and-answer on the same index and model?
- **准确中文翻译**：KnowForge 在完整 M3DocVQA 上的各个报告模型配置取得了什么结果？这些结果与同一索引和模型上的 M3DocRAG 及 one-shot retrieve-and-answer 相比如何？
- **通俗问法**：这条问题要求比较 KnowForge 的不同模型/流程配置，并与 M3DocRAG 和一次检索一次回答的基线对照。

### 按 `reference_answer` 翻译的中文候选答案

在完整 M3DocVQA 上，Qwen3.6-35B-A3B 的 M5 得分为 EM 39.5 / F1 47.5。同一模型和同一索引使用官方模板进行 one-shot retrieve-and-answer，得分为 33.1 / 40.4，即 F1 提高 7.1 个百分点。已发表的 M3DocRAG 得分为 31.4 / 36.5；在同一索引上用 Qwen2-VL-7B 做 one-shot 复现，得分为 31.7 / 38.0。DeepSeek-V4-Flash 的 M5 得分为 44.6 / 53.5，而其较早的 M4 流程得分为 41.7 / 50.8。

- **证据页码**：2, 3, 5, 16
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - The 7B row is a same-index one-shot reproduction, not a full M5 run.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 3, 4, 6, and 16 rendered with PyMuPDF and visually inspected; the Figure 1 pipeline and Tables 2, 3, and 6 were mapped against pages.json. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-039

- **case_id**：`heldout-ai-039`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：Across MultiHop-RAG, MMLongBench-Doc, and M3DocVQA, what do the per-job ablations, model comparisons, and Locator round-budget sweep show about the contribution of reading, searching, and additional rounds?
- **准确中文翻译**：在 MultiHop-RAG、MMLongBench-Doc 和 M3DocVQA 上，各任务消融、模型比较和 Locator 轮数预算扫描说明了 reading、searching 及增加轮数各自的贡献是什么？
- **通俗问法**：这条问题要比较三个数据集上去掉阅读、减少搜索或增加轮数会发生什么，并看不同模型是否一样。

### 按 `reference_answer` 翻译的中文候选答案

各任务的效果取决于语料的视觉性。M3DocVQA 的 S4/P1/P0/M5 F1 分数为 40.4/43.7/45.6/47.5：迭代搜索有帮助，但移除 reader 相比 M5 损失 3.8；P0 找到的文档略多，却回答得更差。MMLongBench-Doc 的 S4/1+1/M5/P1 judge accuracy（%）为 34.8/41.5/47.0/37.2，因此从 1+1 增加到 4+4 提高 5.5 个百分点，而移除阅读损失 9.8 个百分点。MultiHop-RAG 的官方 accuracy（%）为 68.2/70.8/73.5/74.1；由于短文本文章适合放入 pool，P1 与 M5 在统计上持平。

在 mid300 上，Qwen3.6 从 43.1 提升到 50.9，Flash 从 47.2 提升到 54.1/53.3；Qwen2-VL-7B 则从 40.6 降到 37.4，Qwen3-VL-8B 仅持平。Locator 轮数上限扫描（依次对应 0+0、1+0、1+1、2+2、3+3、4+4、5+5、6+6）为 43.1、44.7、46.4、46.7、46.2、50.9、49.2、50.6：检索效果平稳上升，但回答 F1 只在 4+4 时出现台阶式提升；更大的上限增加了调用，却没有带来收益。当另一个角色固定为 4 时，Locator 从 4 降到 3 损失 2.3 F1，Reader 从 4 降到 3 损失 0.6。

- **证据页码**：6, 7, 8, 15
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - Benchmarks use different metrics; cross-corpus magnitudes are not directly interchangeable.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 3, 4, 6, and 16 rendered with PyMuPDF and visually inspected; the Figure 1 pipeline and Tables 2, 3, and 6 were mapped against pages.json. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-040

- **case_id**：`heldout-ai-040`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：How do corpus visuality, the separation of search from reading, and latency scaling with model calls explain where KnowForge helps or fails, and what limitations remain for other document collections?
- **准确中文翻译**：语料的视觉性、搜索与阅读的分离，以及随模型调用次数变化的延迟，如何解释 KnowForge 在何处有帮助或失效？对于其他文档集合还存在哪些限制？
- **通俗问法**：这条问题要解释 KnowForge 为什么在图表/视觉文档上更有用、为什么分离搜索和阅读有帮助，以及调用模型带来的速度和泛化限制。

### 按 `reference_answer` 翻译的中文候选答案

KnowForge 在同时需要检索和视觉阅读的场景中帮助最大。Deep Reader 在多模态 M3DocVQA 上带来 +3.8 个 token-F1 点，在图表密集的 MMLongBench-Doc 上带来 +9.8 个 judge-accuracy 百分点；在纯文本 MultiHop-RAG 上则没有显著收益（P1 为 74.1%，M5 为 73.5%，均为 official accuracy）。将搜索与阅读分开修复了调度失败：one-agent 基线多搜索 34%，阅读次数只有一半，在 74% 的问题上跳过阅读，找到的文档略多，但 F1 低 1.9。

在前 100 个 mid300 问题上，按每次一个问题串行运行，延迟主要随模型调用次数变化：pipeline 的 p50 延迟，Qwen3.6 为 27.3 秒、Flash 为 31.9 秒；one-shot 为 4.6/6.0 秒，pipeline 约有九次调用。PDF 数量从 80 增加到 3,368 时，单次检索轮次只从 0.113 秒增加到 0.121 秒。限制包括跨页拼接较弱、错误拒答、小模型无法遵循协议、只在 Qwen3.6 上观察到的轮数平台、Wikipedia 记忆、对 model judge 的敏感性、单一延迟配置，以及本实验中保持中性的图扩展在更密集的跨文档集合中可能表现不同。

- **证据页码**：1, 6, 8, 9, 10, 11
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - Latency is for one hardware configuration/client; MMLongBench-Doc is model-judge scored.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 3, 4, 6, and 16 rendered with PyMuPDF and visually inspected; the Figure 1 pipeline and Tables 2, 3, and 6 were mapped against pages.json. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-041

- **case_id**：`heldout-ai-041`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：For one multi-hop multimodal question, reconstruct the execution path from Locator queries through the bridge judgment and typed evidence-pool updates to Deep Reader evidence use, synthesis, and format validation.
- **准确中文翻译**：针对一个多跳多模态问题，请重建从 Locator 查询、bridge 判断、有类型 evidence pool 更新，到 Deep Reader 使用证据、综合答案和格式校验的执行路径。
- **通俗问法**：这条问题要求复述论文中的一个具体案例：查询怎样逐步扩展，证据池怎样更新，阅读器怎样找到答案，最后怎样规范化输出。

### 按 `reference_answer` 翻译的中文候选答案

对于“哪位 1979 Trenton Twin Indy +1-lap 车手赢得 Indy 500 次数最多？”这个 M3DocVQA 示例问题，Locator 发出四个逐渐聚焦的查询；所有查询都落到赛事页面。bridge judge 正确要求为 Indy 500 胜场数进行第二跳，但从 prose 中推断出的候选人有误，随后通过后续查询检索 winners list。有类型 merge 保留了所有排名后的材料。

M5 随后打开表格图像；Race 6 表格给出 Wally Dallenbach、Rick Mears、Tom Sneva 和 Danny Ongais 四名候选人。阅读结果被追加到一个 26.6K 字符的 pool 中，没有发生 eviction。synthesizer 将这些姓名与 winners list 比较后回答 Rick Mears，共四次获胜。checker 在看不到证据的情况下，把草稿规范化为最短的精确片段“Rick Mears”。

- **证据页码**：5, 15, 16
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - The trace says six searches/two read calls overall but narrates several table-image openings; both are preserved without forced reconciliation.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "No page render was required for the cited claims; full extracted text including appendices was read", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-042

- **case_id**：`heldout-ai-042`
- **论文版本**：`2610.13128v1`（paper_id: `2610.13128`）
- **英文原题**：What was the exact monetary infrastructure cost per answered question for each benchmark and model configuration, including indexing, retrieval, visual reading, and generation?
- **准确中文翻译**：对于每个基准和模型配置，包含索引、检索、视觉阅读和生成在内，每个已回答问题的确切货币基础设施成本是多少？
- **通俗问法**：这条问题询问论文是否给出了每道题的美元成本，而不只是 token、延迟或调用次数。

### 按 `reference_answer` 翻译的中文候选答案

由于 `reference_answer` 为 `null`，原记录没有提供可翻译的候选答案。

- **证据页码**：9, 17
- **source_pdf_sha256**：`98768de77db8f95a47af8c4d1595bc1f22fbbfd6d685f9893abf1fcd00d51038`
- **原记录限制**：
  - External pricing and hardware/electricity assumptions are outside the fixed-paper scope.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 17 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "No page render was required for the cited claims; full extracted text including appendices was read", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该题作为“不可回答”负例，并保留其 自动化模型复核记录 verdict？

---

## heldout-ai-043

- **case_id**：`heldout-ai-043`
- **论文版本**：`2610.20335v1`（paper_id: `2610.20335`）
- **英文原题**：How does Read-Parse-Describe define a unified page-level output over bounding boxes, semantic labels, reading order, recognized text, and visual-element descriptions, and how does the space-awareness enhancer modify the base vision-language model?
- **准确中文翻译**：Read-Parse-Describe 如何定义一个统一的页面级输出，使其包含 bounding boxes、语义标签、阅读顺序、识别文本和视觉元素描述？space-awareness enhancer 又如何修改基础 vision-language model？
- **通俗问法**：这条问题要说明页面输出字段是什么，以及 SARA/space-awareness enhancer 如何在不改变输出协议的情况下接入基础模型。

### 按 `reference_answer` 翻译的中文候选答案

页面输出是一个按顺序排序的列表 `Y=[e1,…,en]`。每个元素为 `{id,bbox,category,order,text,html,caption_id,alt_text}`：`bbox` 是渲染页面坐标中的 `[x1,y1,x2,y2]`；`category` 是语义标签；`order` 是规范化的人类阅读顺序；`text` 是 OCR 结果；`html` 是表格结构；`caption_id` 将视觉对象绑定到其 caption；`alt_text` 是事实性的视觉描述；不适用的字段为 `null`。

SARA 冻结基础 VLM，从第 `{2,5,8,11}` 层取出特征，将其拼接并通过 LayerNorm/GELU 投影；利用 grid metadata 和 average pooling，把每个 crop 对齐到原生合并 token，然后通过 `T̂=T+λG′` 注入 guidance。Decoder、labels、schema 和 metrics 都保持不变。

- **证据页码**：2, 3, 4, 6, 7
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - The paper alternates “Read-Parse-Describe” and “Read-Parsing-Describe,” but the schema is consistent.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-044

- **case_id**：`heldout-ai-044`
- **论文版本**：`2610.20335v1`（paper_id: `2610.20335`）
- **英文原题**：How are the documents, continuous pages, layout elements, and visual instances distributed across source domains, languages, page types, and train, validation, and test splits?
- **准确中文翻译**：文档、连续页面、布局元素和视觉实例如何按来源领域、语言、页面类型以及 train、validation、test 划分分布？
- **通俗问法**：这条问题要求整理数据集规模、领域、页面位置、视觉类别、布局类别和数据集划分，并指出论文没有报告的交叉分布。

### 按 `reference_answer` 翻译的中文候选答案

总量为：2,402 份文档、125,218 个布局元素和 11,338 个视觉实例。关于连续页面数，固定论文内部不一致：摘要写 18,163，而正文和附录 A 写 18,164；这里不静默选择其中一个。

对于 2,402 份源文档，图 1b 给出的领域百分比分布为：自动化模型 & ML 11.3%、Accessibility 10.6%、AR/VR/XR 9.8%、Design/HCI 6.2%、Privacy 4.3%、Health 4.0%、Demographics 3.0%、Games/EdTech 2.7%、Ethics/CSCW 2.5%、Hardware 2.3%、Other Domains 43.3%。附录表 6 描述的是另一个单独识别的、更广泛的分析池：2,151 篇 ACM 文章（2020–2024），分布为 自动化模型 & ML 271（12.60%）、Accessibility 255（11.85%）、AR/VR/XR 235（10.93%）、Design/HCI 149（6.93%）、Privacy/Social 103（4.79%）、Health 95（4.42%）、Demographics 71（3.30%）、Games/EdTech/Creativity 65（3.02%）、Ethics/Fairness/CSCW 61（2.84%）、Hardware/Fabrication 56（2.60%）、Other 790（36.72%）。这两个分母不能互换。

整理时保留每篇论文的前八页；如果论文更短，则保留全文。表 7 是在一个全文子集上估计各元素出现位置的比例，不是页面类别计数：Pages 1–8/Pages 9+ 中，title/metadata 为 100.00%/0.00%，equations 为 78.42%/21.58%，figures/captions 为 69.15%/30.85%，body paragraphs 为 62.30%/37.70%，tables/algorithms 为 54.80%/45.20%，references 为 8.25%/91.75%。

视觉实例的主题类别（数量、百分比）为：CS/自动化模型 6,443（56.8%）、social science 2,181（19.2%）、engineering 1,359（12.0%）、medicine/health 652（5.8%）、other 245（2.2%）、biology 110（1.0%）、physics 106（0.9%）、mathematics 98（0.9%）、economics/finance 72（0.6%）、environmental science 50（0.4%）、chemistry 22（0.2%）。视觉形式为：tables 2,758（24.3%）、diagrammatic 2,086（18.4%）、UI/screenshots 1,743（15.4%）、photos 1,529（13.5%）、flowcharts 1,000（8.8%）、bar charts 942（8.3%）、line graphs 466（4.1%）、other 371（3.3%）、scatter 205（1.8%）、heatmap 147（1.3%）、pie 27（0.2%）、anatomical 27（0.2%）、circuit 21（0.2%）、radar 12（0.1%）、molecular 4（0.0%，按论文四舍五入）。

布局分组为：headers 44,617（35.64%）、academic metadata 28,381（22.66%）、scholarly objects 12,336（9.85%）、captions/references 21,381（17.08%）、fragments 17,564（14.03%）、other 939（0.74%）。数据按论文去重后以 90:5:5 划分为 train/validation/test。完整 PDF 没有报告语言分布、逐 split 的精确文档/页面/元素/视觉数量，以及所有这些轴的联合交叉表；不能把 90:5:5 比例转换成虚构的精确数量。

- **证据页码**：1, 2, 4, 5, 15, 16, 17
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - The 18,163/18,164 page-total conflict is retained. Figure 1b uses 2,402 source documents; Appendix Table 6 explicitly uses a separate 2,151-article pool. Table 7 is a full-text-subset estimate of element position, not a benchmark-wide page-class count. Figure 1b labels were checked on the rendered PDF because pages.json interleaves Figure 1a text.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

---

## heldout-ai-045

- **case_id**：`heldout-ai-045`
- **论文版本**：`2610.20335v1`（paper_id: `2610.20335`）
- **英文原题**：What scores did every evaluated end-to-end model and pipeline parser achieve for layout, OCR, reading order, visual description, and joint performance, and what ablations quantify the space-awareness enhancer’s contribution?
- **准确中文翻译**：每个被评估的端到端模型和 pipeline parser 在 layout、OCR、reading order、visual description 和联合表现上取得了什么分数？哪些消融量化了 space-awareness enhancer 的贡献？
- **通俗问法**：这条问题要求把所有模型和管线的六类指标列出来，再解释组件消融显示 space-awareness enhancer 带来了什么变化。

### 按 `reference_answer` 翻译的中文候选答案

指标顺序为 `F1/mIoU/OCR/RO/METEOR/BERTScore`。

- GOT-OCR 2.0：--/--/0.8652/0.5330/--/--
- Nougat：--/--/0.7327/0.4640/--/--
- Dolphin-v2 zero-shot：0.9124/0.9265/0.9307/0.8221/--/--
- MonkeyOCR：0.6030/0.8949/0.9684/0.6254/--/--
- VGT：0.6795/0.8540/0.6304/0.3833/--/--
- PaddleOCR-VL：0.7564/0.9458/0.9043/0.6619/--/--
- PP-StructureV3：0.6375/0.8870/0.9558/0.5054/--/--
- MinerU：0.7171/0.9151/0.9671/0.6757/--/--
- ChatGPT-5.4：0.6978/0.6454/0.9796/0.7253/0.3506/0.8751
- ChatGPT-5.6：0.7489/0.7781/0.9448/0.8967/0.3793/0.9004
- Gemini 3.6 Flash：0.6909/0.8658/0.9544/0.8183/0.3744/0.8841
- Gemini 3.1 Pro：0.7421/0.8551/0.9633/0.8337/0.3785/0.8961
- Gemma4-E4B zero-shot：0.5473/0.7674/0.8977/0.5256/0.1679/0.8455
- DeepSeek-OCR zero-shot：0.5987/0.6074/0.9064/0.5003/0.2562/0.8280
- DeepSeek-OCR2 zero-shot：0.5806/0.5963/0.8688/0.4830/0.2604/0.8332
- Qwen3.5-0.8B full FT：0.7177/0.8345/0.9475/0.9605/0.3666/0.8904
- Qwen3.5-2B full FT：0.7336/0.8350/0.9408/0.9628/0.3741/0.8928
- Gemma4-E4B full FT：0.9358/0.8836/0.9452/0.9628/0.3047/0.8821
- DeepSeek-OCR full FT：0.9644/0.9492/0.9679/0.9825/0.3998/0.8968
- DeepSeek-OCR2 full FT：0.8783/0.9189/0.9416/0.9231/0.3229/0.8094
- Dolphin-v2 full FT：0.9826/0.9588/0.9751/0.9857/0.3396/0.8838
- Gemma4-E4B + LoRA：0.7687/0.7238/0.9164/0.9451/0.2871/0.8753
- Gemma4-E4B + SARA：0.9437/0.9136/0.9474/0.9288/0.3009/0.8746
- Qwen3.5-2B + LoRA：0.7004/0.6845/0.9673/0.9304/0.3586/0.8652
- Qwen3.5-2B + SARA：0.9189/0.8919/0.9636/0.9774/0.3834/0.8946

组件消融（`F1/mIoU/OCR/RO`）为：Full FT：0.7336/0.8350/0.9408/0.9628；Grid align only：0.7541/0.8440/0.9566/0.9762；Projector only：0.6830/0.8139/0.9134/0.9262；Grid align + projector：0.9189/0.8919/0.9636/0.9774。论文没有定义单独的 joint composite score；这里的 joint performance 是并列呈现的指标契约。

- **证据页码**：8, 9
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - “--” means the system does not produce that field; metrics are separate rather than one deployment-specific scalar.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

## heldout-ai-046

- **case_id**：`heldout-ai-046`
- **论文版本**：`2610.20335 / 2610.20335v1`
- **英文原题**：Why does evaluating document-understanding components separately fail to capture structural and semantic accessibility, and what dataset or model limitations remain after unified evaluation?
- **准确中文翻译**：为什么分别评估文档理解组件无法捕捉结构和语义可访问性？在统一评估之后，数据集或模型还存在哪些局限？
- **通俗问法**：不要只看 OCR、框、顺序或替代文本中的某一项；要看它们在同一页解析中是否彼此对应，并说明这种统一方法的覆盖和模型限制。

### 按 `reference_answer` 翻译的中文候选答案

分别测试组件，无法检验这些字段在同一个可访问的页面解析中是否彼此一致。正确的框可能搭配错误的 OCR、HTML、标题或描述；OCR 很高但阅读顺序分数很低，仍可能把多栏顺序弄乱；没有替代文本的高 F1 并不完整；替代文本很强但定位很弱，可能描述了错误的裁剪区域。统一评估把身份、几何位置、顺序、文本/表格结构、标题链接和描述绑定在一起并共同报告。局限包括：数据集只含开放获取的 ACM 科学 PDF，且主要覆盖前八页；后部附录和参考文献代表性不足；替代文本由 LLM 辅助并经人工润色；无法排除闭源预训练数据暴露；生成的元数据尚未达到部署就绪；SARA 不能替代专门监督或表格感知解码；在 Qwen3.5-2B 上，训练数据为 10% 时四项结构指标落后于全量微调，30% 时结果混合，优势在 50% 和 100% 时才显现；无边框复合图可能使框和标题发生偏移，密集表格可能增加解码压力、截断序列化输出或引入少量无关伪影。

### 证据与限制

- **证据页码**：2, 7, 8, 9, 21
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - The paper has automatic benchmark metrics and annotation audits, not deployment/user accessibility validation.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-047

- **case_id**：`heldout-ai-047`
- **论文版本**：`2610.20335 / 2610.20335v1`
- **英文原题**：What is the precise serialized output schema for a page, how are reading-order and spatial relationships represented, and how does the joint evaluation metric combine structural fidelity with visual-semantic description quality?
- **准确中文翻译**：页面的精确序列化输出模式是什么？阅读顺序和空间关系如何表示？联合评估如何同时衡量结构保真度与视觉—语义描述质量？
- **通俗问法**：把一页输出的字段、阅读顺序、框和标题关联，以及各项评分规则说清楚；特别确认所谓“联合”是否真的合成了一个总分。

### 按 `reference_answer` 翻译的中文候选答案

目标是 Y=[e1,…,en]，其中 ei={id,bbox,category,order,text,html,caption_id,alt_text}。bbox 是渲染页面坐标中的 [x1,y1,x2,y2]；order 是规范的人类阅读顺序；text/html 承载 OCR 和表格结构；caption_id 把视觉对象绑定到其标题；alt_text 是事实性的视觉描述；不适用字段为 null。列表按 order 排序；如果列表位置与 order 不一致，评估会先按 order 字段归一化，重复或缺失的索引会在匹配和序列指标中受到惩罚。空间落地由 bbox 加 caption_id 表示，而不是单独的成对关系图。评估使用 category 相同且 IoU≥0.5 的 F1、mIoU、用于 RO/OCR 的归一化编辑相似度，以及用于描述的 METEOR/BERTScore。没有一个公式把结构和语义分数合成单一总分；这些字段被有意并列报告。

### 证据与限制

- **证据页码**：3, 4, 7, 8
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - “Joint” means evaluation under one schema, not reduction to one scalar.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-048

- **case_id**：`heldout-ai-048`
- **论文版本**：`2610.20335 / 2610.20335v1`
- **英文原题**：What usability scores and qualitative findings were obtained from blind or low-vision participants who evaluated the generated alternative text in realistic document-reading tasks?
- **准确中文翻译**：盲人或低视力参与者在真实文档阅读任务中评估生成的替代文本时，取得了哪些可用性分数和定性发现？
- **通俗问法**：问题想知道论文有没有让盲人或低视力用户实际试读，并报告他们的分数和体验。

### 按 `reference_answer` 翻译的中文候选答案

无。reference_answer 未提供盲人/低视力参与者在真实文档阅读任务中的可用性分数或定性发现。

### 证据与限制

- **证据页码**：5, 9, 17
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - Such an answer would require external study material outside the fixed-paper scope.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "PDF pages 1 and 4 rendered and visually inspected; relevant tables/formulas were also checked against extracted page text", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-049

- **case_id**：`heldout-ai-049`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：How does Gewu Insight create persistent, stateful research objects that unify venue-, topic-, and author-based discovery records while accumulating versions, resources, metadata, and research context in a local library?
- **准确中文翻译**：Gewu Insight 如何创建持久、有状态的研究对象，把按 venue、主题和作者发现的记录统一起来，并在本地库中不断积累版本、资源、元数据和研究上下文？
- **通俗问法**：说明同一篇论文怎样成为一个持续存在的对象，并把不同发现来源、版本、文件、元数据和后续分析放在一起。

### 按 `reference_answer` 翻译的中文候选答案

Gewu Insight 为每项规范科学工作分配稳定的 Gewu Key；该键标识论文对象而不是文件。Journal、Subject 和 Author Monitor 的发现，以及研究者或获授权 MCP 导入、只要解析到同一工作，就复用同一身份，同时保留各自的来源和观察上下文。随后由四个相互关联的领域持续积累信息：身份；带来源归属的描述性证据（元数据、来源关系、发表与引用证据）；资源关系（版本、补充材料和附件，以及 Default 选择、来源关系和本地路径）；上下文/状态（发现来源、Library 文件夹、Spaces、My Publications 和运行状态）。SQLite 保存结构化状态以及普通本地文件的路径。通过 自动化模型 界面保存的分析，会追加带有 Gewu Key 和起始连接信息的本地 Markdown 资源，保留源文件、较早分析和已有的 Default，而不是替换该工作。

### 证据与限制

- **证据页码**：3, 4, 10, 12
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - The answer is limited to the fixed PDF and excludes behavior only observable in external code or data.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [1, 2, 3, 4, 10, 11, 12, 16, 17, 18, 21, 24], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["Gewu Key", "persistent paper object", "Journal Monitor", "Subject Monitor", "Author Monitor", "provenance", "research state"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [1, 3, 4, 10, 12, 21], "revision_2_visual_inspection": [{"page": 3, "purpose": "Revision 2: Figure 2 lifecycle and object-domain text rendered with PyMuPDF at 1.6x and viewed."}], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-050

- **case_id**：`heldout-ai-050`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：What candidate-precision results were obtained for each of the three discovery modes in the bounded evaluation, including sample counts, confidence intervals, and error categories?
- **准确中文翻译**：在有界评估中，三种发现模式各自取得了什么候选精确率结果，包括样本数、置信区间和错误类别？
- **通俗问法**：分别列出期刊、主题和作者三条发现渠道的样本、筛选、命中比例，以及论文是否给了置信区间。

### 按 `reference_answer` 翻译的中文候选答案

Task A 在筛选后评估了 493 个选定的 monitor-work 候选中的 421 个（被排除的候选没有补充）。Journal：选定 150 个，初步排除 5 个，评估 145 个；其中 137/145（94.5%）同时满足期刊、日期和研究文档标准。初步排除包括 2 个社论/策划合集项目和 3 个新闻/评论/摘要项目；保留的候选中还有 8 个后来被判定为非研究文档，并计为失败。因此，94.5% 是以筛选后的分母为条件的比例，不是全部 150 个输出上的精确率。Subject：110 个选定候选全部评估；严格相关为 78/110（70.9%），同一队列上的补充宽松相关为 104/110（94.5%），所有严格正例也都是宽松正例。严格相关要求主题是主要科学问题、方法或结果；宽松相关允许实质性的方法学、应用、综述或邻近研究联系，而不是偶然提及。通过相减可知，32 个未被严格确认，6 个未被宽松确认；后者包括 1 个尚未解决的判断，不能说是 6 个已确认错误。Author：选定 233 个，因文档范围排除 49 个，因无法核验精确来源排除 18 个，剩余 166 个接受评估；其中 156/166（94.0%，未加权）确认归属于目标作者。范围排除包括摘要、海报/会议日程、学位论文、专利和其他范围外文档；完整会议论文仍符合资格；不可用来源是单独类别，不能当作已确认的文档类型排除。保留的 Author 候选中有 10 个未确认归属，其中包括 2 个未解决身份，不能说是 10 个已确认错误。未知判断仍保留在分母中，没有被确认。由于作者抽样在作者内部的纳入概率不等，论文还报告了筛选范围内经逆概率加权的 91.93% 确认比例。这些都是有界、按渠道划分的点比例；Task A 没有报告置信区间，因此这里不提供或重建任何区间。

### 证据与限制

- **证据页码**：4, 5, 14, 23, 25
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - The three proportions use channel-specific criteria and screened denominators; they are not interchangeable population precision estimates.
  - Task A confidence intervals and a finer per-case scoring-error taxonomy beyond the reported criteria, exclusions and unresolved categories are not provided. No intervals or unsupported error breakdowns are invented.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [4, 5, 13, 14, 15, 19, 22, 23, 25], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["confidence interval", "95%", "Wilson", "candidate precision", "screening", "excluded"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [4, 5, 13, 14, 15, 22, 23, 25], "revision_2_visual_inspection": [{"page": 23, "purpose": "Revision 2: Table S3 header, channel rows and screening/error footnotes rendered with PyMuPDF at 1.6x and viewed."}], "revision_2_full_pdf_search_performed": true, "revision_2_search_terms_checked": ["confidence", "interval", "precision", "proportion", "unresolved", "unconfirmed"]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-051

- **case_id**：`heldout-ai-051`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：How often did the controlled workflows preserve paper identity, distinguish resource versions, and retain research state, and what concrete failures or identity conflicts were observed?
- **准确中文翻译**：受控工作流多大程度上保留了论文身份、区分资源版本并保留研究状态？观察到了哪些具体失败或身份冲突？
- **通俗问法**：核对受控测试是否保住了论文身份、版本和研究上下文，并区分“测试通过”与实际失败/冲突处理。

### 按 `reference_answer` 翻译的中文候选答案

全部 160 个预先定义的身份/资源案例均通过（160/160），全部 104 个状态连续性工作流场景也均通过（104/104）。评估没有报告受控案例失败。相互冲突的身份证据被作为预期的“不合并/拒绝”处理；例如，重复使用 DOI 但生物学元数据冲突时会被拒绝。重新发现会复用论文身份，已发表版本会保留更早的预印本，不相关的状态仍保持不变。

### 证据与限制

- **证据页码**：5, 26
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - Passing deliberately constructed cases demonstrates conformance to specified operations, not a population failure rate or production reliability estimate.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [4, 5, 14, 15, 19, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["160 controlled", "104 controlled", "conflicting evidence", "identity conflict", "failed", "passed"], "external_code_data_excluded": true, "visual_inspection": []}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-052

- **case_id**：`heldout-ai-052`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：How does the proposed continuity layer address fragmentation across services and directories, and what limits does the bounded evaluation place on claims about scientist-led, 自动化模型-assisted discovery and epistemic grounding?
- **准确中文翻译**：提出的连续性层如何处理跨服务和本地目录的碎片化？有界评估对“科学家主导、自动化模型 辅助发现”和认识论基础等主张设置了哪些限制？
- **通俗问法**：解释系统怎样把散落在不同服务和目录里的研究资料串起来，同时说明这些实验不能证明哪些更强的长期科研效果。

### 按 `reference_answer` 翻译的中文候选答案

动机中的碎片化是：版本、关联记录和文件散落在不同服务与本地目录中。Gewu Insight 通过共享的论文身份，以及对某项操作会改变和保留什么的明确规则，把发现、版本更新、项目复用和连续的人—自动化模型 分析连接起来。因此，保留下来的来源和较早上下文可以被重新查看，而不必重新构建。证据范围是有界的：发现比例只描述按渠道特定标准评估的样本；Task B/C 是刻意构造的案例，报告为精确通过数，而不是随机总体估计。Task C 的 104 个场景由 26 个场景族、每族 4 个结构变体组成，不是重复运行。回顾性开发示例不同于正式评估，IBSC 文献研究是工作流示例，不是生产力或研究质量提升测试。研究者仍负责提出问题并评估解释。固定论文支持的是信息连续性和可检查性主张，而不是已证明的纵向科学结果提升或已经实现的认识论基础；更精确地把主张关联到版本、图或段落，是明确列出的未来工作。

### 证据与限制

- **证据页码**：1, 5, 6, 7, 13, 14, 15
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - The fixed evaluation covers channel-specific discovery samples and constructed preservation workflows; it does not estimate general production reliability or causal productivity/research-quality gains.
  - The IBSC case is illustrative. Claim-level links and revision tracking toward stronger epistemic grounding are proposed extensions, not measured achievements.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [1, 5, 6, 7, 13, 14, 15, 19, 20, 39], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["fragmentation", "continuous literature workflow", "bounded evaluation", "epistemic", "formal evaluation", "future"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [1, 5, 6, 7, 13, 14, 15], "revision_2_visual_inspection": [], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-053

- **case_id**：`heldout-ai-053`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：For one scientific work, trace the research-object state transitions as a new discovery record, revised paper version, associated file, and researcher interpretation are added, including how identity conflicts and provenance are represented.
- **准确中文翻译**：针对一项科学工作，追踪新发现记录、修订版论文、关联文件和研究者解释加入时研究对象的状态转移，并说明身份冲突和来源关系如何表示。
- **通俗问法**：用论文中的一般生命周期和具体案例，说明新增记录、版本、文件、解释之后对象如何变化，以及冲突和来源如何留下记录。

### 按 `reference_answer` 翻译的中文候选答案

需要用两个明确不同的来源视角来回答这个追踪问题，不能虚构一条单一的已观察运行记录。(1) Figure 2 的一般生命周期：同一个论文对象 P* 和 Gewu Key 持续存在，新的发现记录带着各自的来源/观察上下文不断累积；新增的已发表文件成为 Default，而预印本仍保留；新的 Space B 关联不会删除 Space A。有效的修订版本作为额外资源保留。自动选择优先级为 corrected-published、published、accepted-manuscript、preprint，但要服从有效的人工 Default。关联资源保留分类、来源关系、本地路径、指纹和谱系。通过 自动化模型 界面保存的解释会追加 Markdown 资源，保留来源、较早分析和 Default，并记录 Gewu Key 与起始连接。如果提供源文件链接，这是可选的，但必须提供文件 ID 和 SHA-256，并成功完成源哈希检查（页码引用可选）。(2) Table S8 的具体 Cheriton 追踪：较早分析经过重新阅读来源后被复查；2026 年 9 月 27 日追加了同一论文下的后续精炼分析，文件为 1410，而较早的文件 1124 仍可单独检索。交付文件哈希与历史来源引用和保存回执相符。随后 Obsidian 综合重用了该解释并明确引用文件 1410，同时保留 DOI、Gewu Key 以及图/页/文件定位信息。这个案例记录的是解释/文件来源关系，不是 Figure 2 中所有发现/版本事件的一条实证时间线。基于标识符的冲突书目观察会被记录下来供复核，而不是自动合并；paper_identity_conflicts 保存证据和处理状态。资源记录则分别保留文件所有权、分类、来源关系、路径、指纹和组织谱系。

### 证据与限制

- **证据页码**：3, 4, 5, 10, 11, 12, 21, 39
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - Figure 2 is a generic illustrative lifecycle; Table S8 is the concrete Cheriton interpretation/file trace. They are not asserted to be one empirical chronology containing every discovery and version transition.
  - Only the fixed PDF and appendix were checked; the external source files and save receipts listed in S8 were not independently opened. Source linkage and page locators are optional under the documented general interface.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [3, 4, 10, 12, 21, 26, 33, 34, 35, 36, 37, 38, 39], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["new discovery record", "publication-file addition", "identity conflict", "provenance", "interpretation", "Cheriton"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [3, 4, 5, 10, 11, 12, 21, 24, 39], "revision_2_visual_inspection": [{"page": 3, "purpose": "Revision 2: Figure 2 generic sequence rendered with PyMuPDF at 1.6x and viewed."}, {"page": 39, "purpose": "Revision 2: Table S8 stage/evidence columns rendered with PyMuPDF at 1.6x and viewed; not conflated with generic Figure 2."}], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-054

- **case_id**：`heldout-ai-054`
- **论文版本**：`2610.14538 / 2610.14538v1`
- **英文原题**：What longitudinal productivity or research-quality improvements were measured for scientists using Gewu Insight over multiple completed research projects?
- **准确中文翻译**：论文是否测量了科学家使用 Gewu Insight 完成多个研究项目后，生产力或研究质量的纵向提升？
- **通俗问法**：查找论文是否真的做过跨多个已完成项目的长期生产力或科研质量对比；如果没有，就明确说没有。

### 按 `reference_answer` 翻译的中文候选答案

无。reference_answer 未提供科学家使用 Gewu Insight 跨多个已完成研究项目后生产力或研究质量提升的测量结果。

### 证据与限制

- **证据页码**：4, 13, 14
- **source_pdf_sha256**：`b61f30a40506bcdf37192f817b7008b3d8a030fe764fc5f18b02200080b262c7`
- **原记录限制**：
  - Absence is bounded to the complete fixed PDF including its appendix; external evaluation data/code were excluded by scope.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 39, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39], "pages_read_in_depth": [1, 2, 3, 4, 5, 6, 7, 13, 14, 15, 19, 39], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["longitudinal", "productivity", "research quality", "completed research projects", "multiple projects", "scientist outcomes"], "external_code_data_excluded": true, "visual_inspection": []}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-055

- **case_id**：`heldout-ai-055`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：How does ReSemReason share a question-induced distribution over typed, directed relation schemas between candidate discovery and evidence selection, and how are ReSemAttn, the path prior, and absolute-support score used afterward?
- **准确中文翻译**：ReSemReason 如何在候选发现和证据选择之间共享由问题诱导的、带类型且有方向的关系模式分布？随后如何使用 ReSemAttn、路径先验和绝对支持分数？
- **通俗问法**：按“问题理解的关系模式 → 搜索和保留路径 → 注意力与路径先验 → 候选排序和解释路径”的顺序说明方法。

### 按 `reference_answer` 翻译的中文候选答案

ReSemReason 预测由问题诱导的、带类型且有方向的关系模式分布，并在两个阶段复用同一模式内容，不使用金标准测试模式。兼容性会软性引导不完整前缀的图搜索，在有限搜索预算内保留不同解释；保留的路径按候选分组，形成候选特定的证据记忆。完成的路径与这些模式评分，得到 u_ik 和候选内部的 p_ik。ReSemAttn 使用关系条件的键/值投影，在缩放后的 token-query/path-key logits 进入 softmax 前加入 η_ik=λ c_q log(p_ik+ε)；C_i=A_iV_i 再融合到更新后的 token 状态中。独立的绝对支持项 b_i=log Σ_k exp(u_ik)−log|Π_i| 保留归一化会消除的“整体较弱支持”。候选分数为 s_i=MLP([h̄_i;h_ai;h_q])+μb_i，使用更新后 token 的均值表示、候选表示和原问题表示。排序按 s_i 进行，并返回 π_i*=argmax_{π_ik∈Π_i}u_ik 及其端点；解释路径按兼容性选择，而不是按聚合注意力权重选择。

### 证据与限制

- **证据页码**：2, 3, 4, 5
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - The method does not guarantee that a high-attention or selected path is independently valid.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [1, 2, 3, 4, 5, 9, 10, 13], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["question-induced distribution", "schema", "ReSemAttn", "absolute support", "relation-conditioned"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [3, 4, 5], "revision_2_visual_inspection": [{"page": 3, "purpose": "Revision 2: Eq.2–4 schema/gate definitions rendered with PyMuPDF at 1.6x and viewed."}, {"page": 4, "purpose": "Revision 2: Eq.7,9–12 rendered with PyMuPDF at 1.6x and viewed."}, {"page": 5, "purpose": "Revision 2: Eq.13–14 and output interface rendered with PyMuPDF at 1.6x and viewed."}], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-056

- **case_id**：`heldout-ai-056`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：What answer-ranking and answer-set results did ReSemReason and all compared systems obtain on BioHopR, PrimeKGQA, and MedReason under the reported evaluation metrics?
- **准确中文翻译**：在报告的评估指标下，ReSemReason 及所有对比系统在 BioHopR、PrimeKGQA 和 MedReason 上取得了哪些答案排序和答案集合结果？
- **通俗问法**：完整抄录表 1 中各系统在三个数据集各指标上的比例，并注意是否还有附录表中的其他指标。

### 按 `reference_answer` 翻译的中文候选答案

表 1 的比例列顺序为：BioHopR 的 H@1/H@5/H@10/P@10/R@10/MRR；PrimeKGQA 的 EM/F1/Recall；MedReason 的 MCQ accuracy/Open F1。Zero-shot：0.221/0.352/0.411/0.176/0.286/0.294；0.342/0.487/0.522；0.512/0.538。CoT：0.468/0.474/0.515/0.313/0.367/0.486；0.368/0.516/0.548；0.548/0.566。Dense RAG：0.403/0.558/0.606/0.337/0.421/0.471；0.394/0.543/0.579；0.563/0.581。GraphRAG：0.548/0.641/0.674/0.402/0.478/0.601；0.427/0.574/0.612；0.586/0.604。GNN-RAG：0.587/0.672/0.708/0.425/0.501/0.635；0.444/0.591/0.629；0.604/0.621。BioLinkBERT：0.579/0.661/0.697/0.420/0.491/0.624；0.443/0.589/0.627；0.581/0.599。PullNet：0.574/0.604/0.631/0.414/0.493/0.596；0.451/0.596/0.632；0.591/0.609。GreaseLM：0.626/0.702/0.745/0.451/0.522/0.684；0.476/0.617/0.653；0.611/0.629。UniKGQA：0.643/0.709/0.748/0.466/0.536/0.696；0.488/0.628/0.664；0.620/0.638。Think-on-Graph：0.655/0.718/0.756/0.472/0.542/0.704；0.495/0.634/0.671；0.626/0.644。Fixed-Candidate ReSemAttn：0.719/0.736/0.743/0.582/0.556/0.726；0.508/0.642/0.678；0.637/0.655。Hard-Schema ReSemReason：0.748/0.789/0.811/0.598/0.621/0.765；0.531/0.661/0.696；0.652/0.670。Soft-Schema ReSemReason：0.772/0.816/0.842/0.617/0.648/0.790；0.652/0.761/0.794；0.691/0.749，是所有报告列中的最佳值。

### 证据与限制

- **证据页码**：7
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - Values are Table 1 point estimates/proportions; metrics differ by dataset and should not be compared as if they were the same task.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [5, 6, 7, 10, 11, 12], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["Table 1", "BioHopR", "PrimeKGQA", "MedReason", "H@1", "Open F1"], "external_code_data_excluded": true, "visual_inspection": [{"page": 7, "purpose": "Table 1 column/row alignment verified from a 2x PyMuPDF rendering."}]}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-057

- **case_id**：`heldout-ai-057`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：What do the path-discrimination analyses and matched controls show about the separate effects of graph search, relation-conditioned attention, schema uncertainty, and the absolute-support score?
- **准确中文翻译**：路径判别分析和匹配控制实验显示了图搜索、关系条件注意力、模式不确定性和绝对支持分数各自的什么作用？
- **通俗问法**：把检索不到候选、关系注意力、模式不确定性和绝对支持分别造成的变化列出来，同时说明控制实验不能推出的因果结论。

### 按 `reference_answer` 翻译的中文候选答案

匹配控制实验区分了这些机制。在冻结候选池下，关系引导搜索把覆盖率/候选召回率从 0.860/0.691 提高到 0.947/0.792；在同一个无引导候选池内，把 CE 替换为 ReSemAttn 后，H@1/conditional H@1/ValidPath@1/Joint@1 从 0.639/0.743/0.520/0.462 提高到 0.701/0.815/0.646/0.597；在有引导候选池内，则从 0.692/0.731/0.585/0.541 提高到 0.766/0.809/0.714/0.681。不确定性处理也有帮助：Hard schema 的 BioHopR H@1 为 0.748，无熵门控变体为 0.758，而 Full 为 0.772，并且跨数据集也有相应提升。移除绝对支持后，H@1 仅从 0.766 变为 0.758，但在 all-invalid/one-invalid-path 候选池中，错误支持接受率从 0.169/0.203 大幅增加到 0.407/0.456。这些控制实验显示了检索可用性、关系条件表示、不确定性和绝对支持的不同作用，但没有建立统计交互。

### 证据与限制

- **证据页码**：7, 8
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - The controls isolate mechanisms under the reported pools/configuration but do not establish a statistical interaction or universal causal effect.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [6, 7, 8, 11, 12, 13], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["frozen candidate", "relation-guided", "schema entropy", "hard schema", "absolute support", "FSR"], "external_code_data_excluded": true, "visual_inspection": []}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-058

- **case_id**：`heldout-ai-058`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：How does the reachable-but-invalid problem differ from simple answer-retrieval failure, and what failure cases or limitations remain when endpoint correctness and evidential support disagree?
- **准确中文翻译**：“可到达但无效”问题与简单的答案检索失败有何不同？当终点正确性与证据支持不一致时，还存在哪些失败案例或局限？
- **通俗问法**：区分“答案根本不在候选池”与“答案在但路径走错”，再列出论文归纳的失败类型及剩余边界。

### 按 `reference_answer` 翻译的中文候选答案

简单的检索失败是指所需答案/证据不在候选池中。“可到达但无效”则是指正确终点已经存在，甚至可能排名很高，但所选路径没有实现问题所要求的关系组合；因此必须把终点正确性和路径有效性分开报告。在归类的 125 个失败中，论文报告：路径被剪枝 27（21.6%）、模式遗漏 23（18.4%）、落地/对齐问题 19（15.2%）、缺失边 16（12.8%）、关系歧义 14（11.2%）、有效路径权重不足 13（10.4%）、阈值处理 8（6.4%）、标注歧义 3（2.4%）、其他 2（1.6%）。剩余局限包括知识图谱关系缺失/歧义、落地不完整、模式遗漏、束搜索剪枝、均值池化特征丢失关系顺序，以及不等同于来源质量模型的拓扑惩罚；路径/注意力是可检查的模型证据，不是因果解释，也不是临床验证。

### 证据与限制

- **证据页码**：1, 5, 9, 13
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - Failure frequencies diagnose locations, not causal module effects; inspectable paths are not clinical validation.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [1, 2, 4, 5, 6, 8, 9, 11, 12, 13], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["reachable-but-invalid", "correct endpoint", "failure", "pruned path", "missing edge", "limitations"], "external_code_data_excluded": true, "visual_inspection": [], "revision_2_pages_reread": [9], "revision_2_visual_inspection": [], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-059

- **case_id**：`heldout-ai-059`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：What equations define ReSemAttn’s relation-conditioned key and value projections, the uncertainty-weighted path prior, and the absolute-support score, and how are these quantities combined to rank an answer and its evidence path?
- **准确中文翻译**：哪些方程定义了 ReSemAttn 的关系条件键和值投影、不确定性加权路径先验和绝对支持分数？这些量如何结合起来给答案及其证据路径排序？
- **通俗问法**：逐项给出关键方程，再说明路径先验如何进入注意力、绝对支持如何进入候选分数，以及最终如何选出证据路径。

### 按 `reference_answer` 翻译的中文候选答案

对于候选 i 在第 ℓ 层的路径 k，k^ℓ_ik=W^ℓ_K(r_ik)z_ik，v^ℓ_ik=W^ℓ_V(r_ik)z_ik（Eq.9）。低秩键混合为 ω^K_ik=softmax(W^K_ω r_ik+b^K_ω)，W_K(r_ik)=W_K^(0)+Σ_{b=1}^{B_r}ω^K_ik,b U_K,b V_K,b^T；值分支使用各自的权重/基（Eq.10）。令 β=softplus(γ)≥0，E_ik^(m)=β^T d_ik^(m)，u_ik=log Σ_mρ_m exp(−E_ik^(m))，p_ik=softmax_k(u_ik)（Eq.7）。不确定性门控为：M=1 时 c_q=1，否则 c_q=1−H(ρ)/log M，其中 H(ρ)=−Σ_mρ_mlogρ_m（Eq.4）。因此 α^ℓ_tik=softmax_k((q^ℓ_ti)^T k^ℓ_ik/√d+λ_ℓc_qlog(p_ik+ε))（Eq.11）。等价地，A_i=softmax_row(Q_iK_i^T/√d+1_Tη_i^T)，η_ik=λc_qlog(p_ik+ε)，C_i=A_iV_i（Eq.13）；再通过学习到的门控残差融合更新 token 状态（Eq.12）。绝对支持为 b_i=log Σ_{k=1}^{|Π_i|}exp(u_ik)−log|Π_i|，保留归一化会丢失的整体较弱支持。排序使用 s_i=MLP([h̄_i;h_ai;h_q])+μb_i，其中 h̄_i 是更新后 token 状态的均值，h_ai 是候选文本，h_q 是原问题表示；返回的证据为 π_i*=argmax_{π_ik∈Π_i}u_ik（Eq.14）。候选按 s_i 排序，每个终点携带 π_i*。解释选择依据兼容性，而不是聚合注意力权重。

### 证据与限制

- **证据页码**：3, 4, 5
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - PDF pages 4–5 were rendered and visually checked for Eq.7 and 9–14; all quoted evidence still preserves genuine continuous extracted text after NFKC/whitespace normalization.
  - The confidence gate measures concentration of retained schemas, not validity or recovery of an omitted schema; ranking/path compatibility does not itself certify a path.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [3, 4, 5, 10, 13], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["relation-conditioned", "key", "value", "path prior", "absolute support", "candidate score"], "external_code_data_excluded": true, "visual_inspection": [{"page": 4, "purpose": "Equations 7 and 9–12 visually verified from a 2x PyMuPDF rendering."}, {"page": 5, "purpose": "Equations 13–16 and candidate scoring visually verified from a 2x PyMuPDF rendering."}], "revision_2_pages_reread": [3, 4, 5], "revision_2_visual_inspection": [{"page": 3, "purpose": "Revision 2: Eq.4 gate rendered with PyMuPDF at 1.6x and viewed."}, {"page": 4, "purpose": "Revision 2: Eq.7,9–12 rendered with PyMuPDF at 1.6x and viewed."}, {"page": 5, "purpose": "Revision 2: Eq.13–14 rendered with PyMuPDF at 1.6x and viewed."}], "revision_2_full_pdf_search_performed": false}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## heldout-ai-060

- **case_id**：`heldout-ai-060`
- **论文版本**：`2610.19754 / 2610.19754v1`
- **英文原题**：What clinician-adjudicated validity rates and inter-rater agreement were reported for the biomedical reasoning paths selected on all three evaluation datasets?
- **准确中文翻译**：对于三个评估数据集上选出的生物医学推理路径，论文报告了哪些由临床医生裁定的有效率和评审者间一致性？
- **通俗问法**：核对论文是否真的有临床医生逐数据集裁定的有效率和一致性；不要把普通标注者一致性改称临床验证。

### 按 `reference_answer` 翻译的中文候选答案

无。reference_answer 未提供三个评估数据集上由临床医生裁定的有效率和评审者间一致性。

### 证据与限制

- **证据页码**：6, 9, 12
- **source_pdf_sha256**：`49419329d3c29e935d3c462e709d84b38e8e5e12ba5cf424ae84f44a11340b37`
- **原记录限制**：
  - The paper’s generic agreement figures cannot be relabeled as clinician agreement or per-dataset validity rates.
  - Actual reviewer text-reading scope (not a complete visual audit): {"complete_pdf_available": true, "page_count": 13, "pages_scanned": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], "pages_read_in_depth": [5, 6, 9, 10, 12, 13], "appendix_included": true, "full_pdf_search_performed": true, "search_terms_checked": ["clinician", "clinical adjudication", "adjudicator", "validity rate", "inter-rater", "agreement", "κ"], "external_code_data_excluded": true, "visual_inspection": []}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.

---

## 文件绑定与审核边界

本文件只是一份方便阅读的中文整理，不是对冻结工件的替换。每题的 `source_pdf_sha256`、论文版本、证据页码和原记录限制应以对应的冻结 JSONL 为准。`reference_answer` 为空的题目明确保留为“没有可直接翻译的候选答案”，不能自行补造答案。

用户决定如果要保存，必须另存为独立的逐题决定记录，并保留 `reviewer_kind=user_provided`、`authenticated_actor=false` 等诚实 provenance；不能将本文件中的 自动化模型建议重标为真人审核。完成用户阅读也不等于完成外部身份认证、校准批准、预算批准、production dev 或 heldout live 运行。

## 冻结源文件摘要

- `v1/questions.jsonl`：`70147c77472f0e553d02b1eaa63fb45a928fb8d3f63ad4e747d2279b6ecd1503`
- `v1/references.jsonl`：`83d417e3f4699bf73f857e0acbf3a133faf77ba9a2197477fcb2c360d4b162c1`
- `v1/reviews.jsonl`：`e386536772a17fe4a61f77f8da5ce4fe7152ae9a3289b90f271f216f647aeef8`
