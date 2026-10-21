# Independent Heldout 中文复核记录：Batch 03（031–045）

> 范围：仅包含 `heldout-ai-031` 至 `heldout-ai-045`，共 15 题，顺序固定为 031→045。
> 说明：`自动化模型结论` 明确来自 `reviewer_kind=llm` 的 自动化模型复核记录，不代表人类评审；中文候选答案严格按 `reference_answer` 翻译，`reference_answer=null` 的题目不补造答案。

## heldout-ai-031

- **case_id**：`heldout-ai-031`
- **论文版本**：`2610.12617v1`（paper_id: `2610.12617`）
- **英文原题**：How does the document-aware generation pipeline profile intelligence reports before generating questions, and how are expert annotation and the LLM-as-judge protocol used to validate the resulting benchmark?
- **准确中文翻译**：文档感知的生成流水线在生成问题前如何对情报报告进行画像？又如何利用专家标注和 LLM-as-judge 协议验证所得基准？
- **通俗问法**：这条问题想知道：系统先怎样把报告的范围、重点和可回答边界整理出来，再怎样自动出题，并用专家和 自动化模型 评分确认题目与答案是否靠谱。

### 按 `reference_answer` 翻译的中文候选答案

报告首先用八个语义探针对其进行画像：范围、关键发现、参与者、地点、时间线、风险、建议和定量主张；每个探针检索 top-4 chunks。随后通过一次调用，将代表性 chunk 集合转换为文档类型、用途，以及明确的 `can_answer`/`cannot_answer` 范围。出题阶段接收该画像、这些 chunks 和 16 个分析师 persona；系统只选择有证据支持的 persona，最多生成 20 个原子问题，标注为事实型、推理型、比较型或风险型，并附理由和来源 chunk grounding 指针。对每个问题重复执行 hybrid top-8 检索；LLM 只能依据这些 chunks 作答，推理型/风险型答案加入行内引用和情态限定，必要时返回 `INSUFFICIENT_CONTEXT`。

验证使用六个维度、每项 1–3 分：groundedness、文档用途匹配度、原子性、清晰度、答案忠实性和总体评价。三名领域分析师各评估约 150 个条目，共享 100 条重叠题块，并各自评估约 50 条独有题，因此共有 250 个不同条目。在重叠题块上，成对平均的二次加权 Cohen’s κ 为 0.24，三方完全一致率为 63%，至少 2/3 一致率为 99%。候选评审器 GPT-5.4、Claude Sonnet 5 和 DeepSeek-V3.2 使用同一量表；最终选用 GPT-5.4，它相对于 A1、A2、A3 的六维合并完全一致率分别为 80%、63% 和 75%。在 250 条人工标注题上，论文报告 low-effort 评分与默认评分在完全一致率和 macro-F1 上等效，总体均值仍为 2.5。之后，GPT-5.4 通过 Batch API 进行 low-effort 评分，为全部 5,525 条题返回有效分数，没有拒答或丢失的评审调用。

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：p4–p5 的 eight probes/top-4、profile scopes、16 personas/≤20 typed questions、hybrid top-8/INSUFFICIENT_CONTEXT、六维 1–3 validation、250-item human sample 与 p6 的 judge comparison/5,525 valid calls 全部覆盖。κ、63%、99%、80/63/75、2.5 等数字的表头和条件没有被省略。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：采用部分可答而非整题 abstain：直接报告 5,525/413/13.4、四类数量比例、415 报告的 institutional-category 分布与 122 publishers；同时明确 per-report Q&A、report-length、individual-publisher、realized geopolitical-region 分布未报。p4 的 55,353 chunks mean/median 仅被标成 chunk length，未冒充 report length。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：Table 6 的 15 个系统及五项指标、bge-m3/Qwen3-8B 与 commercial API 条件逐项核对；reference answer 的数值、指标方向和比较范围与表头一致。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：p3/p5/p6/p7–p9 的 corpus scope/bias、conditional gold-context flawed rates、model-mediated generation、human/author involvement、single-run/one-judge/bootstrap、citation-gold bias、evaluation exclusions 与 deliberate diversity 均有语义成组证据。答案把 28/38/47/70 明确限定为该 reader protocol 的 conditional answer-quality rates，不外推为 real-world error probabilities。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2 将题目改为部分可答且没有机械整题 abstain：p13 Table 10 的 risk-oriented/Geopolitical Researcher/question/reference answer/[1]、p12 Appendix D 的 answer-cited-gold 规则、p6 retrieval judgment、p13 reader scoring 逐项核对；p14 只是 profile/scoring template，不被冒充为该 item 的实例。所有真实缺口（item profile、[1] chunk text/ID、ranked retrieval、generated response、individual score）均明确 not reported。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：负例合理：p10 说明 derived artifacts 才按 CC-BY-4.0 发布、source PDFs 不 republish，122 publishers 条款各异且 many reserve rights，并保留 attribution/authoritative link；全文没有 415 reports 的 publisher-by-publisher permission matrix。故不能把“未列出许可”误答成逐 publisher 均已取得许可。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2独立复核确认流程、角色边界和访问边界均被连续语义锚覆盖：第3页的固定代码调度/typed size-bounded merge，第4页的Locator与bridge judge输入、逐hop检查、最多两次去重follow-up，第5页的30K分池、Deep Reader只读工具/无搜索、读结果回池与synthesizer，第15页的format checker仅见question/answer且不能增添信息。61/61 anchors形式命中，reference_answer的每个组件职责和条件均有对应证据，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2补足Table 6表头、full-set范围、基线配置和模型条件后，M3DocVQA各项EM/F1数值及比较关系可逐项核对：M3DocRAG与same-index Qwen2-VL-7B在第16页，Qwen3.6 S4/M5与Flash M4/M5在同表，且第2/5页明确了模型/模板条件。14/14 anchors命中；数值、单位和比较scope无缺口，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2已补足Table 2/3/4表头、数据集问题数、strict/official accuracy条件及round-cap含义。第6–8、15页支持三数据集per-job ablation、mid300模型对比、0+0至6+6 sweep以及4→3边际损失；答案中的“reading/searching/rounds”结论是由表值作出的明确比较而非越界推断。35/35 anchors命中，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2补足延迟表头、串行100题/无其他负载条件、model-call数量、baseline对照及限制条件。第1/6/8–11页共同支持visuality与reader收益、one-agent调度差异、pipeline/one-shot latency、corpus-scale retrieval成本、跨页/false refusal/小模型/judge/硬件等限制；51/51 anchors命中，单位、条件和negative limitations均清楚，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2复核Appendix F worked example的完整执行轨迹：Locator queries、bridge第二跳及候选偏差、M5读取表格、四名候选、26.6K pool无eviction、Rick Mears/four wins及format normalization均与原文一致。19/19 anchors命中，问题要求的单个案例scope已完整回答，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2确认这是合理无答案边界题而非材料缺失：全文报告的是token/embedding/OCR page/wall-clock/latency/call/hardware等非货币量，没有每题美元成本所需的价格、折旧、电价、计费或货币总额。8/8 anchors命中，negative explanation与fixed-PDF scope一致，故unanswerable标注接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2补足SARA实现条件与公式语义：第6页明确Concat、projector的LayerNorm/GELU、crop grid切片与average-pool、T-hat残差注入，第7页明确base VLM frozen/adapter only，第2–4页明确schema、coordinate、labels/metrics不变。20/20 anchors命中，答案没有把实现细节扩展到external code，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2独立核对完整PDF、渲染后的Figure 1b及Appendix表：答案如实保留abstract 18,163与body/Appendix 18,164的页数冲突，没有猜选；区分2,402 benchmark与2,151 broader analyzed pool，并给出各自domain/visual/layout分布、8-page截取和90:5:5 paper-level split。未报道的language、exact per-split counts/cross-tab明确写not reported，29/29 anchors命中，接受。
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

### 自动化模型复核

- **自动化模型结论**：`accepted`（`reviewer_kind=llm`；自动化模型复核记录，非人类评审）
- **自动化模型 理由中文摘要**：r2补足Table 2/3完整表头、zero-shot/full-FT/adapter条件及“-- means field not produced”语义。37/37 anchors命中，所有列顺序F1/mIoU/OCR/RO/METEOR/BERTScore、各模型/管线行及component ablation数值均可在第8–9页核对；“joint performance”被正确解释为并列指标而非虚构综合分，接受。
- **证据页码**：8, 9
- **source_pdf_sha256**：`a9fbda250a985c3293e183ab3bf359a633740ad36bd9c3d044ac93b4964da963`
- **原记录限制**：
  - “--” means the system does not produce that field; metrics are separate rather than one deployment-specific scalar.
  - Actual reviewer text-reading scope (not a complete visual audit): {"declared_scope": "complete fixed version paper including appendix, not external code/data", "pages_read": "all 22 PDF pages including appendices", "text_source": "pages.json fully scanned; every quote asserted after Unicode NFKC normalization and whitespace collapse", "visual_inspection": "Revision 2: PDF pages 1, 6, 8, and 9 rendered with PyMuPDF and visually inspected, including Figure 1b labels, SARA equations, and Tables 2/3 headers; r1 also inspected page 4. No claim of full-page visual coverage.", "external_code_or_data": "excluded", "revision_recheck": "Fixed PDF/pages rechecked against the independent r1 review; complete-PDF reading from r1 remains the source scope. Table headers, condition/metric units, and claim-level support were checked in addition to literal quote matching."}
  - 自动化模型生成的 reference; no authenticated human review and no production qualification.
- **用户决定提示**：请用户决定：是否接受该条目及其 自动化模型复核记录 verdict（不将其视为人类评审）？

