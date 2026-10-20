## Context

Paper RAG 当前已有 Dense、Field Embedding、BM25/sparse、RRF、multi-query、reranker、parent/child context expansion 和 evaluation protocol 相关能力。QASPER 外部适配器已经可以使用 NumPy 内存索引完成一轮可重复评测，但它与生产 Qdrant/PaperFieldChunkStore 链路并不完全相同，因此本变更必须把“评测质量优化”和“生产延迟优化”分开处理。

现有 QASPER Dev 运行基于 888 个可映射文本证据问题、281 篇论文和 17,048 个 chunks。严格 ID 结果低于内容等价结果，说明 evidence mapping 是主要诊断点之一；同时还缺少通道级消融和参数网格，无法判断分数瓶颈来自候选召回、融合排序还是 chunk 对齐。

本变更属于评测和实验能力建设。它必须保留 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage` 运行护栏，不把 LLM 变成 routing 或 pass/fail 决策者，也不在评测期间写 production index、memory 或 active skill。

## Goals / Non-Goals

**Goals:**

- 定义冻结、可复现的 QASPER evidence mapping 和 mapping version。
- 同时实现 Strict/Equivalent 的 Hit、Coverage、MRR、nDCG 指标，并保存逐题证据。
- 支持 Dense、BM25、Field、RRF、multi-query 和 reranker 的通道消融与参数矩阵。
- 生成完整 manifest、逐题结果、切片报告、失败归因和成本/延迟信息。
- 复用现有 evaluation ports、retrieval channels 和 scorecard，不创建平行 evaluator。
- 让后续新会话可以从文档和 OpenSpec 直接继续实现。

**Non-Goals:**

- 不在本变更中修改生产默认检索权重或强制更换 embedding 模型。
- 不实现 QASPER 的端到端回答评分、LLM judge 或新的 benchmark 数据集。
- 不把 Equivalent 指标作为唯一正式指标，也不通过放宽 mapping 规则人为提高分数。
- 不把外部 QASPER 原始数据、完整 embedding cache 或付费运行结果提交到 Git。
- 不改变既有 Harness 的 workflow routing、quality gate、memory write 或 publication authority。

## Decisions

### 1. 采用冻结 mapping，而不是运行时猜测 gold

每个 case 在评测前生成并冻结 `IndexEvidenceMapping`，绑定 dataset、corpus、parser、chunker、index 和 mapping digest。mapping 记录 gold anchor 到 exact child、parent closure、text span 和邻接 chunk 的关系。

运行时只读取 mapping，不根据当前返回的 candidate 反向扩大 gold。这样可以避免“为了命中而修改 gold”。mapping 无法确定时保留 `ambiguous`/`missing`/`system_failure`，不能强制变成负例。

备选方案是每次运行通过字符串相似度临时匹配。该方案不可复现、容易受当前候选影响，因此不采用。

### 2. Strict 与 Equivalent 使用独立 metric ID/version

`StrictHit@K` 只接受 exact evidence/chunk ID；`EquivalentHit@K` 接受冻结 mapping 定义的父子闭包、normalized span containment、相邻 chunk 覆盖或内容等价。两者都使用相同的题目分母，但保留独立的 `metric_id`、`metric_version` 和 `mapping_version`。

备选方案是只报告宽松匹配。该方案会掩盖真实 chunk ID 对齐问题，不采用。只报告严格匹配也会把切分差异误判为检索失败，同样不采用。

### 3. 先扩大高召回候选池，再进行 rerank

标准实验链路为：各通道独立召回 -> 去重并保留通道 provenance -> RRF/融合 -> reranker -> Top-K 评测。候选池实验优先于 reranker 阈值实验，避免把“未进入候选池”的问题误诊为排序问题。

默认试验点为 `candidate_limit=50/100/150`、`overfetch_multiplier=3/5/8`、`rrf_k=20/60/100` 和 `rerank_score_threshold=0/0.15/0.3`。这些是实验配置，不自动成为生产默认值。

### 4. 通过 profile 做通道消融

每次运行使用一个显式 profile，至少支持：`semantic_only`、`bm25_only`、`field_only`、`semantic_bm25`、`semantic_field`、`full_hybrid` 和 `full_hybrid_multi_query`。profile 指纹进入结果 manifest；不能通过环境变量隐式改变评测配置。

### 5. 评测适配器与生产链路分层

QASPER 快速实验可以继续使用已有 NumPy in-memory index，以降低迭代成本；生产链路验证必须另行使用真实 Qdrant/PaperFieldChunkStore。报告必须标识 `execution_profile=fixture` 或 `production`，不能把 fixture 结果描述成生产延迟或完整生产质量。

### 6. 统计以论文组为独立单位

同一论文的多道问题不是相互独立样本。比较两个 profile 时，重采样和置信区间按 paper group 保留题目相关性；样本不足时只输出描述性结果并标记 `insufficient_evidence`。

## Evaluation Pipeline

```text
Frozen dataset/corpus/mapping
          |
          v
Question + allowed scope
          |
          +--> Dense channel --------+
          +--> BM25 channel ----------+--> dedup + provenance
          +--> Field channel ---------+
          +--> Multi-query variants --+
                                      |
                                      v
                              RRF / candidate fusion
                                      |
                                      v
                                  Reranker
                                      |
                                      v
                            top-k evidence candidates
                                      |
               +----------------------+---------------------+
               |                                            |
        Strict evaluator                           Equivalent evaluator
               |                                            |
               +----------------------+---------------------+
                                      v
                         slice report + experiment artifacts
```

## Data and Artifact Contracts

每次实验至少产生：

- `manifest.json`：dataset/corpus/mapping/index identity、代码 commit、profile、参数、模型、execution profile、时间和预算；
- `cases.jsonl`：每题 query、gold groups、预测证据、通道 provenance、排名和 mapping 状态；
- `metrics.json`：双口径指标、分子/分母、metric/mapping version、slice counts；
- `failures.jsonl`：未命中、部分覆盖、映射失败、候选缺失、rerank 过滤和系统错误；
- `report.md`：总体、切片、消融、参数比较、延迟和成本。

不得把 API key、原始 provider secret、完整外部数据集或未经授权的付费响应写入提交文件。

## Risks / Trade-offs

- [Equivalent mapping 过宽] -> mapping 规则版本化；报告同时保留 Strict 指标；所有等价命中保留 span/parent 证据。
- [候选池增大导致延迟和成本上升] -> 分开记录 index/request/judge 成本，先做离线缓存实验，再对 production profile 做 P95 预算检查。
- [fixture 与生产链路不一致] -> 结果强制带 execution profile；fixture 只用于质量迭代，生产性能另行测试。
- [调参过拟合 QASPER Dev] -> 预留论文级 held-out 集；Dev 只用于选择策略，不能把同一结果当作泛化证明。
- [mapping 缺失被误当成检索失败] -> 保留 `needs_review`/`system_failure`，报告有效分母和完整计划分母。
- [通道排序改变后破坏已有行为] -> 实验 profile 与生产默认策略隔离，只有独立变更通过评测后才更新默认值。

## Migration Plan

1. 以当前 QASPER Dev 结果冻结 baseline、数据摘要和运行工件。
2. 增加 mapping 生成/校验和 Strict/Equivalent 评分，不改变生产默认检索配置。
3. 增加 profile 消融和参数 sweep，使用已有 embedding cache 优先完成离线实验。
4. 对最佳候选 profile 做逐题人工抽检和论文级切片复核。
5. 如需改变生产默认配置，另开生产检索优化变更，引用本变更的实验 manifest 和报告；失败时只回退 profile，不删除历史工件。
6. 运行范围匹配的测试、compile、smoke 和 OpenSpec strict validation。

## Open Questions

- QASPER gold evidence 到项目 chunk 的最终 span overlap 阈值，需要在第一批人工抽检后冻结并写入 mapping version。
- 目前 QASPER 外部适配器与生产 Qdrant 链路的候选排序是否完全一致，需要通过 production execution profile 单独核对。
- float evidence 是否在本变更中加入 numeric/table 专用 scorer，还是先作为独立 not-applicable slice 保留。
