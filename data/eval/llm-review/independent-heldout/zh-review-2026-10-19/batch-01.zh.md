# Independent Heldout 中文复核记录：batch-01

> 范围：仅覆盖 `heldout-ai-001` 至 `heldout-ai-015`。`自动化模型结论` 与理由均来自 `reviews.jsonl`；`reference_answer` 仅作中文翻译，不扩展其事实范围。`reviewer_kind=llm` 明确表示审查者为 自动化模型，而非人类。

## 01. `heldout-ai-001`

- **case_id**：`heldout-ai-001`
- **论文版本**：`2610.19780v1`
- **英文原题**：How was LabOPBench constructed so that each item connects laboratory evidence, procedural context, an open-ended decision, and a question-specific scoring rubric?
- **准确中文翻译**：LabOPBench 是如何构建的，才能让每道题都把实验室证据、程序性背景、开放式决策和题目专属评分标准连接起来？
- **通俗问法**：简单说：作者怎样把“看实验材料—理解实验过程—做判断—按这道题自己的尺子评分”串成一道题？
- **按 `reference_answer` 翻译的中文候选答案**：LabOPBench 由 22 名具有实验室经验的贡献者构建：他们把真实实验室材料转化为开放式决策题。每道题包含实验/程序背景、一个或多个证据来源以及所要求的判断；背景提供足以完成判断的最小必要信息，例如实验阶段、操作对象、观察到的现象、仪器类型或已知条件。出题者明确记录从证据到决策的路径、相关证据、可接受的替代解释/行动、关键错误，以及题目专属且单调的 3/2/1/0 评分标准。题目必须能够依据所给证据和相关科学知识作答，不能依赖隐藏的实验条件或作者私有信息。科学内容确定后，DeepSeek-V4-Pro 只负责语言标准化，不能改动判断、证据、可接受答案、评分锚点或安全要求；作者会核对标准化版本，另外两名专家独立检查证据可信度/充分性、提示与证据的一致性、科学正确性、确定性和可评分性。实质性分歧由第三名专家裁决；证据不足或存在未解决歧义的题目会被修订或排除。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 认为候选答案完整覆盖了 22 名贡献者、真实实验材料、题目结构、最小充分背景、证据到决策记录、3/2/1/0 评分锚点、可答性约束、DeepSeek 仅做语言标准化、作者与专家复核，以及第三专家裁决和修订/排除；还保留了评分单调性与安全/可评分性条件。
- **证据页码**：2, 3, 4, 13
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The fixed paper describes the construction and review process, not a per-item authoring/reviewer log. Continuous PDF-native paragraph quotes are used because coordinate-sorted pages.json interleaves the two columns; no cross-column text has been fabricated or joined.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 02. `heldout-ai-002`

- **case_id**：`heldout-ai-002`
- **论文版本**：`2610.19780v1`
- **英文原题**：What normalized score did each evaluated multimodal model obtain overall, and how did model performance vary between organic synthesis and materials science items?
- **准确中文翻译**：每个被评估的多模态模型总体获得了多少归一化分数？模型在有机合成题和材料科学题之间的表现有什么差异？
- **通俗问法**：简单说：八个模型总分谁高谁低？它们做有机合成题和材料科学题时差多少？
- **按 `reference_answer` 翻译的中文候选答案**：各模型的总体分数 / 约加权有机合成分数 / 材料科学分数（%）为：Intern-S1-Pro 28.8 / 27.0 / 41.7；Gemini 3.1 Flash-Lite 36.8 / 35.3 / 47.6；Grok 4.3 37.5 / 35.3 / 53.7；MiniMax-M3 39.6 / 37.4 / 55.6；Qwen3.6-Plus 41.2 / 39.6 / 52.8；Gemini 3.1 Pro 41.9 / 40.3 / 53.5；GPT-5.5 43.6 / 42.4 / 52.9；Claude Opus 4.7 45.8 / 43.8 / 59.8。对每个模型，材料科学分数都高于加权有机合成分数，差距约为 10.5–18.4 个百分点。论文直接报告了总体分数和五个主要类别分数；上面的有机合成汇总值是按四个有机类别的题目数量 67/30/425/142 加权、根据表中保留一位小数的分数推导出的近似值，并非论文直接打印的精确领域汇总。论文报告材料科学的跨模型均值为 52.2%，而有机合成中的 postprocessing 是最困难的主要类别，分数为 34.9%。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 核对了 Table 2 的列头、8 个模型、题目数量和有机合成加权规则；候选明确把有机合成汇总标为基于四舍五入显示值推导的近似值，没有把派生值冒充论文直接报告值。
- **证据页码**：3, 6, 7, 18
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The fixed paper does not print one combined organic-synthesis score per model. These organic scores and the domain score differences are explicitly derived from displayed one-decimal category scores and their item counts, not presented as exact author-reported domain aggregates.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 03. `heldout-ai-003`

- **case_id**：`heldout-ai-003`
- **论文版本**：`2610.19780v1`
- **英文原题**：On the controlled 100-question subset, what were the model, undergraduate, and PhD participant results, including the sample sizes, aggregation method, and uncertainty or significance statistics reported for the comparison?
- **准确中文翻译**：在受控的 100 道题子集上，模型、本科生和博士参与者的结果是什么？其中包括哪些样本量、聚合方法，以及论文为比较报告了哪些不确定性或显著性统计？
- **通俗问法**：简单说：100 道统一题里，8 个模型、5 名本科生和 5 名博士各自得分怎样？分数怎么合成，论文有没有给置信区间或显著性检验？
- **按 `reference_answer` 翻译的中文候选答案**：在匹配的、受控的 100 道有机合成题子集上，模型分数为：Claude Opus 4.7 43.7%、GPT-5.5 43.5%、Gemini 3.1 Pro 39.0%、Qwen3.6-Plus 39.0%、MiniMax-M3 37.8%、Gemini 3.1 Flash-Lite 35.4%、Grok 4.3 34.5%、Intern-S1-Pro 26.9%。共有 10 名真人参与者：5 名本科生和 5 名博士，每人回答全部 100 道题。两位审查者评分均值为：本科生 U01–U05 分别为 27.4、30.2、34.4、30.3、32.7%；博士 P01–P05 分别为 47.3、46.6、40.8、48.4、46.5%。本科生组均值为 31.0%（样本标准差 2.7 个百分点），博士组均值为 45.9%（样本标准差 3.0 个百分点）。每个回答由 GPT-5.5 和 Gemini 3.1 Pro 按同一题目评分标准独立评分；每位审查者的分数都在全部 100 个有效回答上归一化，报告的参与者/模型分数是两位审查者归一化分数的算术平均，人类组汇总则是 5 名参与者的均值和样本标准差。论文没有为这一比较报告置信区间或模型与人类之间的显著性检验；Claude 与博士组相差 2.2 个百分点只是描述性结果。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 独立复核了 100 题受控有机合成子集中的 8 个模型分数、排序和百分比，确认题目范围没有扩展，15 个证据锚点均命中固定 v1 正文/表格。
- **证据页码**：5, 22, 23
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The paper reports sample SD only for the human groups and no inferential model-human comparison. Four model rows are directly quoted; the remaining subset values were checked in Figure 5/Table 16 from the same complete PDF.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 04. `heldout-ai-004`

- **case_id**：`heldout-ai-004`
- **论文版本**：`2610.19780v1`
- **英文原题**：How do the benchmark design and the reported Evidence Misreading and Evidence-Decision Conflict error categories jointly explain what current multimodal models fail to do, and what limitations does the paper identify for generalizing this conclusion beyond the evaluated laboratory settings?
- **准确中文翻译**：基准测试的设计，以及报告中的“证据误读”和“证据—决策冲突”错误类别，如何共同说明当前多模态模型失败之处？论文指出哪些限制，使这一结论不能直接推广到评估之外的实验室场景？
- **通俗问法**：简单说：这些题和错误分类具体暴露了模型链条中的哪两类问题？为什么不能据此说模型已经具备真实实验室的闭环自主能力？
- **按 `reference_answer` 翻译的中文候选答案**：LabOPBench 要求模型依据特定案例的多模态证据和实验背景，判断状态、原因、风险或合适的操作，而不只是识别物体或回忆事实。“证据误读”指误读或遗漏与决策相关的实验证据；“证据—决策冲突”指作出与证据或评分标准不一致的诊断或行动建议。这两类错误暴露了证据到决策链条中相邻的两个失败环节：先识别哪些信息约束判断，再选择与这些信息一致的结论/行动。它们分别占模型平衡后的错误标签 40.0% 和 35.8%，合计 75.8%；这不是回答层面的错误率。该错误分析针对三个领先模型在 100 道受控有机合成题子集上的非满分回答，且错误分类法并不穷尽所有错误。更广泛地说，基准测试只覆盖所表示的有机合成/材料科学场景、一个静态决策点以及固定的背景和证据；它排除了连续实验轨迹、行动后的观察、机器人执行，以及干预失败后的重新规划，因此不能完整检验闭环实验自主性。这些输出错误类别也不能证明模型内部的因果机制。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 认为候选完整保留了基准任务语义、两类错误的边界、模型平衡错误标签的统计口径，以及静态单决策、非穷尽分类和不能据此推出内部因果机制等限制，没有夸大为闭环能力或因果结论。
- **证据页码**：3, 6, 7, 8, 23, 24
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - The percentages are equally model-weighted shares of assigned labels from three leading models on the controlled organic-synthesis subset, not response-level rates or causal decomposition of model internals. The benchmark is static and the taxonomy non-exhaustive.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 05. `heldout-ai-005`

- **case_id**：`heldout-ai-005`
- **论文版本**：`2610.19780v1`
- **英文原题**：For one fully documented benchmark example, trace the evidence and procedural context through the required laboratory judgment, the item-specific rubric, and the error category assigned to an incorrect response.
- **准确中文翻译**：对于一个资料完整的基准测试示例，请追踪证据和程序性背景如何经过必需的实验室判断、题目专属评分标准，并最终得到错误回答的错误类别标注。
- **通俗问法**：简单说：能不能从论文中找出一道完整的题，展示材料、背景、正确判断、评分规则和错误分类？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目所要求的完整、可直接回答的示例答案）。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 将其判为合理的 negative heldout：候选没有虚构一个来源可直接回答的完整示例，negative evidence 覆盖固定 v1 全文及附录，且该题不是因关键词缺失、范围含混或越出 PDF 才无法回答。
- **证据页码**：5, 8
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - A released benchmark item or supplementary artifact containing the full question, rubric, candidate response and expert error annotation would be required.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 06. `heldout-ai-006`

- **case_id**：`heldout-ai-006`
- **论文版本**：`2610.19780v1`
- **英文原题**：What exact inter-reviewer agreement statistics are reported for the expert authoring and independent review of all 756 questions, broken down by scientific domain and decision type?
- **准确中文翻译**：对于全部 756 道题的专家出题与独立审查，论文按科学领域和决策类型报告了哪些精确的审查者间一致性统计？
- **通俗问法**：简单说：论文有没有给出 756 道题按领域和决策类型拆分的专家一致性数字？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目所要求的、按科学领域和决策类型拆分的精确审查者间一致性统计）。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 将其判为合理 abstain：候选没有把来源未报告的一致性统计改写成可计算答案，negative evidence 覆盖固定 v1 全文及附录，独立检查也未发现足以唯一推出答案的条件。
- **证据页码**：4, 19
- **source_pdf_sha256**：`1e073a43202bdbb4e6d2924461d06866b170ff501d2f651779156982eaaf92de`
- **原记录限制（原文）**：
  - No external review logs or benchmark-construction data may be used under the declared scope.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 07. `heldout-ai-007`

- **case_id**：`heldout-ai-007`
- **论文版本**：`2610.19432v1`
- **英文原题**：How was MULTIFINGROUND built from FY2025 English, French, and Greek disclosures, and how were reports aligned through the accounting taxonomy to produce traceable fact-level annotations?
- **准确中文翻译**：MULTIFINGROUND 是如何由 FY2025 的英文、法文和希腊文披露构建的？报告又是如何通过会计分类体系对齐，以产生可追溯到事实层面的标注？
- **通俗问法**：简单说：这 90 份三语年报怎样变成可追踪的事实数据，多个任务又怎样共享同一套会计概念？
- **按 `reference_answer` 翻译的中文候选答案**：MULTIFINGROUND 为 90 家发行人各收集一份 FY2025 ESEF 年报：英文、法文和希腊文各 30 份；每家发行人只出现一种语言，且都覆盖截至 2025 年 12 月 31 日结束的完整年度。发行人的 iXBRL 标签提供初始的事实到概念映射。报告被转换为保留表头的文本/Markdown 表格；事实通过可见文本偏移、概念、数据类型、期间类型和借贷方向保持可追溯，并附有来自 IFRS Accounting Taxonomy 2024 或发行人扩展 schema 的属性，数字还被分配标识符。发行人特定扩展只用于 T1；概念级 T2 任务使用标准的 ifrs-full 概念。共享概念连接的是不同发行人独立编写的报告上下文，而不是翻译文本。英文/法文/希腊文上下文中的所有目标出现位置都被标记；满足干扰项条件的 187 个概念产生 521 个配对组。T2b 要求找出所有对应事实，包括比较期数值；T2a 和 T2b 使用独立的无状态调用；T2c 在不提供标签的情况下测试概念辨别。两名金融专家分别审查了分层抽样的 10% 样本：131 个 T1 项、52 个共享 T2a/T2b 组和 53 个 T2c 项，覆盖语言、格式、数据类型和规模，然后讨论分歧。他们的修正/排除只作用于被审查的项目；最终标注集将这些结果与自动生成但未经审计的标注合并。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 认为候选建立了完整可核验链，准确保留了 90 份 FY2025 ESEF 年报的语言分布、iXBRL 映射和发行人扩展限制、521 个组/187 个概念、T2b 比较期、T2a/T2b 分离、T2c 无标签，以及仅审查 10% 分层样本的边界；没有把跨语言差异说成纯语言因果，也没有把子集修正泛化到全体数据。
- **证据页码**：2, 3, 4, 5, 19, 20
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - The audit is not a full-dataset expert relabeling: only the stratified 10% subset is corrected/excluded by expert review. Different issuer contexts are concept-aligned but not parallel translated evidence.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 08. `heldout-ai-008`

- **case_id**：`heldout-ai-008`
- **论文版本**：`2610.19432v1`
- **英文原题**：For each of the 15 evaluated models, what were the concept-recognition accuracy and exhaustive fact-recovery results, and which models showed the largest gap between recognizing a concept and recovering all of its facts?
- **准确中文翻译**：对于 15 个被评估模型中的每一个，概念识别准确率和穷举式事实恢复结果分别是多少？哪些模型在“识别概念”和“恢复该概念的全部事实”之间表现出最大的差距？
- **通俗问法**：简单说：15 个模型“认出概念”和“把所有对应数字找全”分别考得怎样？谁认得出但漏得最多？
- **按 `reference_answer` 翻译的中文候选答案**：15 个模型的 T2a 概念识别准确率 / T2b 概念宏平均 F1 / T2b exact-set 准确率（%）为：GPT-5.6 Luna 97.3 / 88.3 / 65.8；Claude Haiku 4.5 97.5 / 86.2 / 60.1；Gemini 3.8 Flash 99.4 / 89.2 / 65.6；DeepSeek-V4.1-Flash 98.3 / 86.2 / 59.7；Qwen3.8-27B 98.5 / 62.5 / 57.0；Qwen3.8-Flash-Next 98.5 / 73.7 / 67.4；Gemma 4 31B IT 98.8 / 87.4 / 65.1；Qwen3.5-122B-A10B 99.0 / 90.4 / 71.8；Fin-R1 82.7 / 66.8 / 23.4；Fin-o1-8B 87.3 / 74.3 / 39.0；ODA-Fin-RL-8B 90.6 / 81.1 / 49.7；Plutus-8B-Instruct 77.5 / 10.7 / 0.2；Qwen2.5-7B-Instruct 88.3 / 57.0 / 7.5；Qwen3-8B 94.2 / 80.4 / 45.5；Llama-Krikri-8B-Instruct 84.1 / 44.9 / 1.0。以 T2a 正确识别为条件，至少漏掉一个目标的比例最高的是 Qwen2.5-7B-Instruct（88.7%）、Llama-Krikri-8B-Instruct（85.8%）和 Plutus-8B-Instruct（85.1%）；在 8 个 API/开放权重参考模型中，最高的是 Qwen3.8-27B（41.9%）。若采用更简单的 T2a 准确率减 exact-set 准确率差值，最大的三个差距是 Llama-Krikri 83.1 个百分点、Qwen2.5-7B 80.8 个百分点和 Plutus 77.3 个百分点。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 核对了 Table 3 的任务/指标列头、全部参考模型和 Table 5 的 Plutus 行，并确认条件性遗漏率、T2a 与 exact-set 差距、exact-set 的无遗漏且无额外定义及 concept-level 聚合均未被混淆。
- **证据页码**：5, 6, 7, 12, 15
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - T2b macro-F1 rewards partial recovery, while exact-set accuracy requires no omissions and no extras; the conditional miss rate permits extra selections and measures only omission among correctly recognized groups. These metrics should not be conflated.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 09. `heldout-ai-009`

- **case_id**：`heldout-ai-009`
- **论文版本**：`2610.19432v1`
- **英文原题**：How do concept discrimination and exhaustive fact selection vary by language, accounting concept, and model family, and what quantitative evidence supports the conclusion that the evaluated finance-specialized models provide limited gains?
- **准确中文翻译**：概念辨别和穷举式事实选择在语言、会计概念和模型家族之间如何变化？有哪些定量证据支持“被评估的金融专用模型带来的收益有限”这一结论？
- **通俗问法**：简单说：不同语言、概念和模型家族的结果有什么规律？金融专用模型真的明显更好吗？
- **按 `reference_answer` 翻译的中文候选答案**：论文报告了语言层面和模型层面的变化，但没有逐个会计概念给出 T2b/T2c 拆分；T2b 概念宏平均 F1 是先在每个概念内对组 F1 求平均，再跨概念求平均，不能误解为逐概念结果。联合英文/法文/希腊文证据下，各模型的 T2b 英/法/希腊 F1（%）以及当异常摘录为英/法/希腊文时的 T2c 准确率（%）分别为：API—GPT-5.6 Luna：85.0/88.3/83.9；40.3/49.4/63.4（总体 49.4）；API—Claude Haiku 4.5：85.3/85.4/83.6；57.5/62.5/60.7（总体 59.9）；API—Gemini 3.8 Flash：89.8/90.3/85.3；95.5/94.6/95.2（总体 95.1）；API—DeepSeek-V4.1-Flash：83.8/85.0/82.3；38.5/34.5/32.4（总体 35.6）；开放权重—Qwen3.8-27B：68.4/69.0/66.9；71.9/73.2/71.7（总体 72.3）；开放权重—Qwen3.8-Flash-Next：74.3/76.6/75.2；74.2/73.8/79.3（总体 75.5）；开放权重—Gemma 4 31B IT：84.6/86.2/80.4；86.9/90.5/90.3（总体 89.0）；开放权重—Qwen3.5-122B-A10B：87.7/89.1/85.8；91.9/91.1/91.7（总体 91.6）；金融专用—Fin-R1：72.4/65.2/53.8；27.6/24.4/31.0（总体 27.5）；金融专用—Fin-o1-8B：75.7/72.3/64.1；46.6/48.2/47.6（总体 47.4）；金融专用—ODA-Fin-RL-8B：81.2/78.2/73.7；42.5/48.2/47.6（总体 45.7）；金融专用—Plutus-8B-Instruct：24.6/17.0/18.2；18.1/29.2/38.6（总体 27.2）；金融基座—Qwen2.5-7B-Instruct：66.6/52.6/39.0；30.8/17.9/44.8（总体 30.5）；金融基座—Qwen3-8B：80.4/77.2/73.0；70.1/67.9/67.6（总体 68.7）；金融基座—Llama-Krikri-8B-Instruct：49.2/35.1/29.8；32.6/26.8/20.0（总体 27.3）。在 8 个 API/开放权重参考模型中，有 7 个模型的联合证据 T2b F1 在希腊文最低，例外是 Qwen3.8-Flash-Next；这些比较不是纯粹的语言效应，因为发行人、行业和文档风格也在变化。Table 10 报告的金融专用模型相对基座模型差值（T1 宏平均 F1 / T2a 准确率 / T2b 宏平均 F1 / T2c 准确率，单位为百分点）为：Fin-R1 对 Qwen2.5-7B-Instruct：−8.4 / −5.6 / +9.9 / −3.0；Fin-o1-8B 对 Qwen3-8B：−19.7 / −6.9 / −6.1 / −21.3；ODA-Fin-RL-8B 对 Qwen3-8B：−0.9 / −3.6 / +0.6 / −23.0；Plutus-8B-Instruct 对 Llama-Krikri-8B-Instruct：−24.7 / −6.5 / −34.2 / −0.2。因此，4 个金融专用模型在 T1、T2a 和 T2c 上都更低；T2b 中 Fin-R1 有提升、ODA 只有小幅提升，而 Fin-o1 和 Plutus 反而下降。这些是已发布模型之间的观察差异，不是对“金融专用化”因果效应的单独估计。根据四舍五入后的 Table 3 分数相减，会得到 Fin-R1 T2b +9.8、ODA T2b +0.7、Plutus T2a −6.6、Plutus T2c −0.1 个百分点，与 Table 10 的 +9.9、+0.6、−6.5、−0.2 不同 0.1 个百分点。固定 PDF 没有提供未四舍五入数值，无法解决这些差异；这里同时保留论文报告值和基于显示值复算的结果。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 认为候选完整且保守：给出了语言层面和模型家族层面的结果，明确没有逐概念 T2b/T2c 拆分，正确解释了 concept-macro F1 的聚合方式，并同时保留 Table 10 报告值与基于四舍五入 Table 3 的复算差异，没有猜测未四舍五入值。
- **证据页码**：5, 6, 7, 9, 14, 16, 17
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - Per-accounting-concept discrimination/selection results are not reported. Language is confounded with issuer/sector/style; specialist/base comparisons do not isolate specialization. Four Table 10 versus rounded Table 3 differences of 0.1 pp cannot be reconciled from the fixed PDF, so this record explicitly retains needs_review for independent recheck rather than choosing hidden unrounded values.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 10. `heldout-ai-010`

- **case_id**：`heldout-ai-010`
- **论文版本**：`2610.19432v1`
- **英文原题**：Why can near-ceiling concept recognition coexist with incomplete numerical evidence recovery in this benchmark, and which dataset or evaluation limitations constrain how broadly that recognition-grounding gap can be interpreted?
- **准确中文翻译**：为什么在该基准测试中，接近上限的概念识别表现可以与不完整的数值证据恢复同时存在？哪些数据集或评估限制约束了我们对这一“识别—落地”差距的宽泛解读？
- **通俗问法**：简单说：模型知道数字属于哪个概念，为什么仍可能漏掉部分数字？数据和评分规则有哪些地方让结论不能过度外推？
- **按 `reference_answer` 翻译的中文候选答案**：在该协议中，概念识别和证据恢复是不同操作。T2a 在已经标出的数值事实中从四个候选概念里选择共享概念；T2b 在独立的无状态调用中提供该概念，并要求在相同的多语言上下文中返回所有匹配出现位置，包括比较期数值。每组平均有 7.8 个目标；漏掉一个目标就无法达到完整覆盖，exact-set 准确率还要求不能多选。因而语义概念识别正确，并不意味着能完整枚举事实。8 个 API/开放权重参考模型的 T2a 识别准确率为 97.3–99.4%，但在正确识别的组中，仍有 26.6–41.9% 至少漏掉一个目标。对 Gemini 而言，目标数为 3–6 时遗漏率为 27.7%，目标数为 7 个及以上时为 46.3%；这是相关性，不是目标数量的孤立因果效应。该差距受到标注和覆盖限制约束：初始发行人 iXBRL 映射可能有错误或概念选择不一致；专家只审查分层抽样的 10%，因此仍可能有残余错误；T1 不对未标记的类金额数字评分；大多数 T2 上下文是表格窗口，共享概念也往往是主表中的常见概念；数据只覆盖一个报告制度下的三种语言；语言还与发行人、行业和文档风格共同变化。评估本身也有影响：共享发行人使 T2 组存在依赖，因此没有报告 T2 bootstrap 区间；项目级 McNemar p 值只是探索性的，因为没有处理这种依赖。严格评分会把请求、预算、拒答和格式失败都算错，推理/生成设置也没有平衡计算量。这些限制意味着不能把该差距解释为孤立的语言效应或普遍的模型行为因果解释。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 确认候选清楚区分了 recognition 与 evidence recovery，说明了 T2a 四选一、T2b 独立无状态调用、T2c 无标签、exact-set 的双重约束及标注/语言/发行人依赖限制，没有把识别正确误报为证据完整恢复。
- **证据页码**：2, 4, 5, 6, 9
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - The paired gap describes this fixed benchmark and scoring protocol, not a causal mechanism or isolated language effect. Complete coverage permits extras, unlike exact-set recovery; target-count associations, incomplete audit, shared-issuer dependence, generation failures and unequal compute constrain generalization.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 11. `heldout-ai-011`

- **case_id**：`heldout-ai-011`
- **论文版本**：`2610.19432v1`
- **英文原题**：Using one aligned accounting concept from the benchmark, reconstruct how the corresponding numerical facts are identified across independently authored multilingual reports and how recognition, exhaustive selection, and discrimination are scored separately.
- **准确中文翻译**：使用基准测试中的一个已对齐会计概念，重建对应的数值事实如何在独立编写的多语言报告中被识别，以及识别、穷举式选择和辨别是如何分别评分的。
- **通俗问法**：简单说：能否从一个例子说明同一会计概念在不同语言年报里如何找出对应数字，并分别考“认概念、找全数字、排除干扰项”？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目要求的可重建答案）。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 将其判为合理的 negative heldout：negative evidence 在固定 v1 全文及附录中核验了题目所要求但来源未报告的信息，没有发现可直接回答、可无歧义推导或仅因关键词未命中而遗漏的答案。
- **证据页码**：15
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - A released real benchmark group or supplementary item is required; external dataset access is outside the declared scope.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 12. `heldout-ai-012`

- **case_id**：`heldout-ai-012`
- **论文版本**：`2610.19432v1`
- **英文原题**：What exchange rates, currency-normalization dates, and inflation adjustments were applied when comparing numerical facts reported in different currencies across the 90 issuers?
- **准确中文翻译**：在比较 90 家发行人以不同货币报告的数值事实时，采用了哪些汇率、货币归一化日期和通胀调整？
- **通俗问法**：简单说：论文有没有说明不同货币之间到底用什么汇率、哪一天换算、是否做通胀调整？
- **按 `reference_answer` 翻译的中文候选答案**：无（reference_answer 为 None；固定版本论文没有提供题目要求的可重建答案）。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 将其判为有效 abstain/negative：固定 v1 全文及附录没有给出题目要求的汇率、换算日期或通胀调整，也没有足够条件唯一推出它们；该题不属于范围含混、越界或可答误判。
- **证据页码**：17, 20
- **source_pdf_sha256**：`bb73e8dc0e04fb1d8d9c4a8223bcd4b700d8c87db7d5eb432b2ed668e28cdaa4`
- **原记录限制（原文）**：
  - No external rates or economic data may be introduced to fill this absent method.
  - Actual reviewer text-reading scope (not a complete visual audit): [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 13. `heldout-ai-013`

- **case_id**：`heldout-ai-013`
- **论文版本**：`2610.19223v1`
- **英文原题**：How does the definition-grounding process repeatedly judge and revise Lean definitions, and how are clarified informal definitions and unproved basic properties used at different stages of that process?
- **准确中文翻译**：定义落地过程如何反复判断并修订 Lean 定义？经过澄清的非正式定义和未经证明的基本性质，又分别在该过程的哪些阶段发挥作用？
- **通俗问法**：简单说：Lean 定义不合格时怎么循环修改？清理后的自然语言定义和后续未证明的基本性质分别什么时候加入？
- **按 `reference_answer` 翻译的中文候选答案**：定义落地循环会生成下一个 Lean 库状态，并询问 LLM 审查者这些定义能否继续兼容未来陈述；如果得到 False，就重构或重新生成该状态，直到审查者返回 True，之后才继续。ReuseBench 在生成阶段提供清理后的非正式定义：每个定义只介绍数学对象，并把捆绑的主张拆开，同时明确缺失的假设、约定和边界情况。在审查实验中，J0 接收 Lean 定义及其非正式定义，不接收下游证据。随后，标准代理会陈述而不证明下游陈述，其中 S1 包含基本性质。之后的 Ji 接收已经形式化的 S1…Si 及其对应非正式陈述，把观察到的兼容性作为未来可用性的证据。因此，清理生成器输入和向后续审查者加入未经证明的下游证据属于不同阶段，不能互相替代。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 重新核对了 False 后重构/重新生成、直到 True 才继续的循环，以及清理后的对象定义、假设/边界情况、S1 基本性质、state-not-prove、J0 和 Ji 的输入差异；候选没有混淆两个阶段。
- **证据页码**：3, 4, 5, 7
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - The PDF text is multi-column and interleaved in places; supporting clauses are intentionally split into separately verifiable contiguous anchors.
  - The paper evaluates the proposed loop experimentally but does not report running the full iterative repair loop end-to-end as a production system.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["1-9", "12-14"], "bibliography_pages_skimmed": ["10-11"], "rendered_pages_checked": [4, 5, 7], "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 14. `heldout-ai-014`

- **case_id**：`heldout-ai-014`
- **论文版本**：`2610.19223v1`
- **英文原题**：What fraction of generated definition sets was compatible with all later statements for each evaluated model and experimental condition, and how much did clarification of the informal definitions change those rates?
- **准确中文翻译**：对于每个被评估模型和实验条件，有多少比例的生成定义集与所有后续陈述兼容？澄清非正式定义使这些比例发生了多大变化？
- **通俗问法**：简单说：原始定义和澄清后的定义，最终能一路通过后续陈述的比例分别是多少，提升了多少？
- **按 `reference_answer` 翻译的中文候选答案**：“全部通过”表示与每一层后续内容都兼容。Source 条件下：GPT 6 Astra 43.8%、GPT 5.6 Sol 31.2%、Claude Fable 5.1 50.0%、Claude Opus 5 50.0%、Claude Sonnet 5 25.0%，所有模型平均 40.0%。Cleaned 条件下：相应为 81.2%、75.0%、100.0%、87.5%、43.8%，所有模型平均 77.5%。Cleaned 减去 Source 的变化分别为 +37.4、+43.8、+50.0、+37.5 和 +18.8 个百分点；所有模型平均提高 +37.5 个百分点。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 通过独立核对 Table 2 确认使用的是 Full pass rate (%) 列，而不是 S1…S5 witness 列；5 个模型、40.0%/77.5% 汇总和所有由显示值计算的百分点变化均正确，并保留了 8 个理论×2 次生成重复的条件。
- **证据页码**：6, 7
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - Percentage-point changes are arithmetic differences computed from Table 2, not separately printed delta values.
  - Rates are averaged over eight theories and two generator repeats, as specified by the paper.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["4-8"], "rendered_pages_checked": [], "table_text_sufficient": true, "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。

## 15. `heldout-ai-015`

- **case_id**：`heldout-ai-015`
- **论文版本**：`2610.19223v1`
- **英文原题**：How was the cost of stating basic properties without proving them measured, and what detection-quality gains and formalization-cost trade-offs were reported for every model or judge configuration?
- **准确中文翻译**：不证明基本性质、仅陈述它们的成本是如何衡量的？对于每个模型或审查者配置，论文报告了哪些检测质量提升和形式化成本权衡？
- **通俗问法**：简单说：只写出基本性质、不写证明要花多少钱？加入不同层的证据后，检测效果和成本怎样变化？
- **按 `reference_answer` 翻译的中文候选答案**：论文把成本衡量为陈述 S1 的平均货币形式化成本（美元），而不是证明 S1 的成本：标准下游代理只陈述命题、不提供证明。审查实验使用一次调用的 GPT 5.6 Sol 审查者，对每个 J0…J4 配置重复调用 5 次。Table 3 的列顺序为 Baseline（始终 True）、D/J0、+S1/J1、+S2/J2、+S3/J3、+S4/J4；准确率为 0.62/0.65/0.85/0.82/0.82/0.81，precision-recall AUC 为 0.62/0.79/0.85/0.85/0.84/0.87，precision 为 0.62/0.78/0.86/0.82/0.86/0.86。形式化成本行（美元）为 –/$4.32/$0.27/$0.30/$0.24/$0.26；重复 5 次的审查者成本行为 –/$0.17/$0.21/$0.27/$0.32/$0.35。这些是各层报告的形式化成本和特定上下文的审查成本，并不表示后续配置都具有相同的累计成本。加入未经证明的 S1 证据后，准确率从 0.65 提升到 0.85，AUC 从 0.79 提升到 0.85：S1 形式化加 5 次 J1 调用的成本为 $0.27+$0.21=$0.48，约为定义和辅助函数形式化成本 $4.32 的 11%，并在后续层之前标出 73% 的不兼容候选。加入 S2…S4 并没有进一步明显提高准确率，尽管 J4 的 AUC 最高，为 0.87。检测研究比较的是证据配置，而不是多个审查者模型。固定论文没有报告足以独立重算这些美元成本的 token 数量或计费/定价流程。
- **自动化模型结论**：`accepted`（`reviewer_kind=llm`，非人类审阅者）
- **自动化模型理由中文摘要**：自动化模型复核记录 核对了 p7–p9 和 p8 Table 3，确认是一套 GPT 5.6 Sol 一次调用审查者、J0…J4 五次重复、PR-AUC/accuracy/precision 及两类美元成本；候选正确计算 $0.48 和约 11%，没有把层成本当累计成本，也没有编造 token/计费流程。
- **证据页码**：5, 7, 8, 9
- **source_pdf_sha256**：`bce1d8c497badb2d1f8cdc1e0f926aecc4d99b0d215890173de5677932b41c89`
- **原记录限制（原文）**：
  - All 14 fixed-paper page texts, including appendices, were searched for cost, USD, token, pricing and billing methodology. The paper reports average USD amounts but not token counts, a pricing schedule, or a procedure sufficient to independently recompute them; those dimensions remain not reported.
  - The question asks for every model or judge configuration: Table 3 varies J0…J4 evidence configurations of GPT 5.6 Sol, not a separate detection-quality/cost evaluation for every generator model in Table 2.
  - The reported formalization-cost row is not silently converted into cumulative costs for later J2…J4 contexts. The $0.48 S1 calculation includes five judge calls and is not the formalization-only $0.27 cost.
  - Multi-column text interference is retained in contiguous evidence sequences; the actual page 8 table and page 7 method were also rendered and read.
  - Actual reviewer text-reading scope (not a complete visual audit): {"page_count": 14, "full_text_page_scan": "pages 1-14 loaded and searched", "close_read_page_ranges": ["7-9"], "rendered_pages_checked": [5, 7, 8], "table_text_sufficient": true, "full_visual_coverage": "not_verified", "external_code_or_data": "excluded"}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请决定是否接受本题当前的 自动化模型结论 作为评估记录：`□ 接受` `□ 要求复核` `□ 不接受`。如要求复核，请注明需要复核的事实、页码或限制。
