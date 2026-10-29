## Context

现有的 QASPER 检索评测流程如下：`QasperAssetService` 把 dev 集构建成 `ResearchDocument` 和 `PaperChunk`；`freeze_qasper_dataset` 生成冻结证据映射，每道题的 `case_id` 为 `{paper_id}:{question_id}`，映射状态分为 valid、needs_review、not_applicable、system_failure 四种；`CachedFixtureChunkStore` 和 `CachedFixtureFieldIndex` 只从离线缓存读取向量，缺少向量时直接报错。

生产检索器只依赖 store 的 `get_chunk`、`list_chunks` 和 `search_with_scores` 三个接口。缓存中的向量已经归一化，本机 embedding 配置为 dashscope `text-embedding-v4`、1024 维，与缓存一致。

## Goals / Non-Goals

**Goals**
- 用真实 embedding 检索，走生产回答链路，在 QASPER dev 上得到分题型、可复现的端到端指标。
- 只有查询向量允许实时调用 embedding 服务；调用结果必须持久化，同样的查询下次直接复用。
- 评分口径与 QASPER 官方保持一致，判分不能因为看过系统的回答而改变。

**Non-Goals**
- 不计算官方 Evidence-F1。它要求按段落字符串严格匹配，而我们的 chunk 和官方段落不是一一对应的。证据质量改用冻结映射上的 strict/equivalent 引用覆盖来衡量。
- 不修改生产检索、上下文、生成和门禁逻辑。
- 不评测 QASPER test 集。test 集留作最终验证。

## Decisions

### D1 查询向量解析器
`QueryVectorResolver` 按以下顺序查找查询向量：
1. 基础离线缓存；
2. 补充缓存；
3. 上面两处都没有时，调用 `QueryEmbeddingPort.embed_queries`，归一化后写入补充缓存。

具体规则：
- 补充缓存的 key 与基础缓存一致，都是文本的 sha256。补充缓存会记录基础缓存的 SHA-256、模型 ID 和维度，任意一项不匹配就报错。
- 实时返回的向量维度与缓存不一致，或者向量值不是有限数，也直接报错。
- 解析器统计三类计数：基础缓存命中、补充缓存命中、实时调用，并写入运行报告。
- 补充缓存在每道题处理完后立即持久化，运行中途失败时，已经算出的向量不会丢。

`CachedFixtureChunkStore` 和 `CachedFixtureFieldIndex` 新增可选参数 `query_vector`。不传时保持原来的严格离线行为，现有检索评测不受影响。

### D2 分层抽样
- 只抽可评分的题：第一位标注者必须能归入一种答案类型。
- 每类题按 `sha256(seed:case_id)` 排序，取前 N 道（默认 75）；某类不足 N 道时全部入选，并在清单中记录实际数量。
- 抽样只依据题目本身和种子，不读取任何运行结果。

### D3 预测投影与 Answer-F1
- 规范化、token F1、在多个标注者答案中取最高分，三处逻辑都与官方 `qasper_evaluator.py` 逐行对齐。参考答案的生成方式也和官方一致：抽取片段用 ", " 连接，是否题生成 "Yes"/"No"，不可回答题生成 "Unanswerable"。
- 预测投影规则：
  - 模型拒答或证据不足 → "Unanswerable"；
  - 系统失败 → 空字符串，F1 记 0，并单独统计；
  - 是否题的回答以明确的 Yes/No 开头时 → 投影为 "Yes"/"No"；
  - 其余回答 → 去掉 `[n]` 引用标记后原样参与打分。
- "是否题"的判定只看问题文本（是否以助动词开头），不读取 gold，避免看到答案后再决定怎么投影。

### D4 引用与上下文覆盖
只对冻结映射为 valid 的题计算：
- **strict 命中**：引用的 chunk 落在某个锚点的精确路径上。
- **equivalent 命中**：引用的 chunk 落在锚点的 exact_child、parent、adjacent 或 normalized_span 关系中。

上下文覆盖用同样的规则，比较对象换成最终送进模型的 context chunk。其余状态的题如实记录，不参与这两项计算。

### D5 judge
- prompt 内容：问题、全部标注者的参考答案、系统回答，以及系统引用的段落原文。
- 要求 judge 输出严格 JSON：`{"correct": bool, "citations": [{"index": int, "supports": bool}], "rationale": str}`。
- 解析失败或字段非法时记为 `judge_error`，不计入任何正确率；调用失败按同样方式处理。
- 只对实际作答的题调用 judge，拒答和系统失败的题不调用。

### D6 分层与放置
| 层 | 内容 |
|---|---|
| 端口 | `backend/research/ports/qasper_query_embedding.py`：`QueryEmbeddingPort`、`QueryVectorStorePort`，只使用标准库类型 |
| 领域逻辑 | `backend/research/rag/evaluation/` 下的 `qasper_query_vectors`、`qasper_answer_metrics`、`qasper_answer_sampling`、`qasper_answer_judge` |
| 编排 | `backend/research/application/qasper_answer_evaluation.py`，复用 `QasperAssetService` 和 `QasperArtifactStore` |
| 存储 | `infrastructure/research/qasper_query_vector_store.py`，补充缓存以 npz 格式原子写入 |
| 组装 | `interfaces/composition/qasper_evaluation.py` 注入 embedding 模型、回答模型和 judge |

CLI 只调用 application 层。

### D7 上下文组装与证据计数

评测 session 与桌面端问答一致，使用默认上下文组装器，不注入 Research 的压缩运行时，确保测量的是生产问答路径。首轮真实运行时，检索满 8 条证据的题目被预算门判为超限，根因是 `ContextBudgetEstimator` 把承载证据的 `evidence_memory` 段额外计为一条证据（8+1>8）。生产问答同样会触发，因此在框架层修正：证据条数只按去重后的 `evidence_refs` 计算，承载段的成本已计入输入 token 与段数。

## Risks / Trade-offs

- **实时 embedding 产生费用**：只对缓存中没有的查询调用，结果持久化，重跑时不再调用；报告中写明实时调用次数。
- **网关的内容拒答**：QASPER 以 NLP 论文为主，受影响的题应该很少；被拒的题会按系统失败单独统计。
- **单次运行存在波动**：报告中写明样本量和"单次运行"。如需评估稳定性，用相同种子重跑。
- **fixture 检索不经过 Qdrant**：向量与生产使用同一模型、同样的维度，但召回过程是在内存中做矩阵运算，与生产的 Qdrant 近似检索可能存在细微差异。

## Migration Plan

新增功能，不影响现有检索评测。验证顺序：单元测试 → 小样本真实运行 → smoke → 提交 → 300 题完整运行。回滚只需还原对应提交。
