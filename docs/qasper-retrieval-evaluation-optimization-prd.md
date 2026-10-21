# QASPER 检索评测优化 PRD

## 1. 文档信息

- **产品/项目**：Agora-Hub Paper RAG
- **功能名称**：QASPER Retrieval Evaluation Optimization
- **文档版本**：v1.0
- **日期**：2026-10-21
- **关联 OpenSpec**：`openspec/changes/qasper-retrieval-evaluation-optimization/`
- **目标**：在不虚增指标的前提下，提高论文证据检索的可测量性、可解释性和实际召回能力。

## 2. 背景与问题

当前 QASPER Dev 评测已完成一轮全量运行，使用的是 888 个可映射文本证据问题、281 篇论文和 17,048 个检索 chunk。当前基线为：

| 指标 | 当前值 |
|---|---:|
| StrictHit@5 | 60.0% |
| StrictHit@10 | 75.2% |
| EquivalentHit@5 | 66.1% |
| EquivalentHit@10 | 79.7% |
| StrictCoverage@10 | 62.9% |
| EquivalentCoverage@10 | 70.9% |
| MRR | 39.6% |
| nDCG@10 | 41.9% |

当前结果存在两个核心问题：

1. 一部分正确内容因为 chunk ID、父子块关系或切分边界不同，被严格 ID 评测判为未命中。
2. 即使映射问题修正，Dense、BM25、Field Retrieval、RRF 和 reranker 的组合仍未经过系统消融和参数搜索。

## 3. 目标

### 3.1 业务目标

- 能回答“系统是否找到了正确证据”，而不是只回答“ID 是否碰巧相同”。
- 能定位性能瓶颈属于数据映射、候选召回、融合排序、字段路由还是重排阈值。
- 能在固定数据和固定配置下重复运行，比较优化前后结果。
- 能将实验结论安全地转化为后续生产配置变更依据。

### 3.2 量化目标

第一阶段目标为实验目标，不是保证值：

- `StrictHit@10`：从 75.2% 提升至 80% 以上；
- `EquivalentHit@10`：从 79.7% 提升至 85% 以上；
- `EquivalentCoverage@10`：从 70.9% 提升至 75% 以上；
- 不以牺牲 `StrictHit@5`、证据完整性、P95 延迟或可追溯性为代价；
- 所有结论都必须同时报告有效分母、不可映射题和失败状态。

这些目标用于指导实验优先级，不能在样本、映射规则或分母变化后直接宣称达成。

## 4. 用户与使用场景

### 4.1 主要用户

- RAG 检索开发者：调试召回、融合和重排策略；
- 评测维护者：维护 QASPER 数据、evidence mapping 和指标版本；
- 研究报告作者：获得可解释的检索质量证据；
- 质量审核者：审查实验是否存在分母、映射或数据泄漏问题。

### 4.2 核心使用场景

1. 固定当前 baseline，运行 Strict/Equivalent 双口径评测。
2. 比较 Dense、BM25、Field 和 Hybrid 各通道对候选召回的贡献。
3. 比较 `candidate_limit`、`overfetch_multiplier`、`rrf_k` 和 rerank threshold。
4. 查看某一类问题为什么失败，例如 numeric、dataset、table 或 method。
5. 将经过验证的 profile 交给后续生产变更，而不是在评测脚本里隐式改变默认配置。

## 5. 功能需求

### 5.1 Evidence mapping

系统必须为每个 gold evidence 保存：

- 原始 evidence 身份；
- 论文和 source identity；
- section/page/character span；
- 原文摘要或 digest；
- 对应 child chunk、parent chunk 和相邻 chunk；
- mapping 状态：`exact`、`equivalent`、`partial`、`missing`、`ambiguous`。

映射规则必须在实验前冻结，不能根据某次检索结果动态扩大 gold。

### 5.2 双口径命中率

- `StrictHit@K`：Top-K 中出现完全相同的 gold evidence/chunk ID；
- `EquivalentHit@K`：Top-K 中出现内容等价、父子闭包匹配或冻结 span 重叠的证据。

两者必须同时输出，并记录 metric version 和 mapping version。

### 5.3 召回消融

至少支持以下 profile：

```text
semantic_only
bm25_only
field_only
semantic_bm25
semantic_field
full_hybrid
full_hybrid_multi_query
```

每个 profile 记录各通道的候选数量、独立 gold evidence 数、首次命中位置和最终 Top-K 结果。

### 5.4 参数实验

至少支持以下参数网格：

```text
candidate_limit: 50, 100, 150
overfetch_multiplier: 3, 5, 8
rrf_k: 20, 60, 100
rerank_score_threshold: 0, 0.15, 0.3
```

实验结果必须携带完整配置指纹，不允许只保存一个不可复现的总分。

### 5.5 切片诊断

报告至少支持按以下维度切片：

- 问题类型：method、dataset、result、comparison、numeric、why/how；
- 证据数量：单证据、多证据、必要证据组；
- 论文；
- section/field；
- mapping 状态；
- 首次命中排名；
- 失败原因。

### 5.6 结果状态

以下状态必须保留并可单独统计：

```text
ok
needs_review
system_failure
not_applicable
```

不可映射题、float evidence 和缺失语料不得静默从分母中删除。

## 6. 非目标

- 本阶段不直接更换 embedding 模型；
- 不通过增加 LLM 生成调用来改善 retrieval 指标；
- 不把 Equivalent 指标替换 Strict 指标；
- 不在同一 held-out 集上反复调参后继续称为盲测；
- 不在评测脚本中修改生产索引、active skill、业务 memory 或质量 gate；
- 不把 QASPER retrieval 分数当作端到端回答质量分数。

## 7. 验收标准

1. 同一数据、同一 index、同一 profile 重复运行，结果和 manifest 可复现。
2. Strict 和 Equivalent 指标同时存在，且每个指标都有版本、分子、分母和映射说明。
3. 父块、子块和文本等价的判断可追溯到具体 mapping 证据。
4. 至少完成七组通道消融，并能输出逐题结果。
5. 至少完成一轮候选池、RRF 和 rerank threshold 参数实验。
6. 117 个当前不可映射问题、253 个 float evidence 项和 163 个 unanswerable annotation 的处理状态在报告中可见，不能被隐式丢弃。
7. 评测运行不会修改生产状态，默认不产生未声明的付费调用。
8. OpenSpec 通过 `openspec validate qasper-retrieval-evaluation-optimization --strict`。

## 8. 推荐实施顺序

```text
1. 固定 baseline 和评测分母
2. 实现 evidence mapping 与 Strict/Equivalent 双口径
3. 做通道消融，确认召回来源
4. 调 candidate_limit、overfetch 和 RRF
5. 调 reranker threshold 和字段路由
6. 按问题类型切片诊断
7. 最后再考虑 embedding 模型比较
8. 形成生产配置变更提案
```

## 9. 实施与验收状态（2026-10-21）

实现与使用入口见 `docs/qasper-retrieval-evaluation.md`，可恢复的工作状态见 `codex-handoff-qasper-retrieval-evaluation-optimization.md` 和 OpenSpec `tasks.md`。基线身份只代表对应的历史/重建运行，不覆写其分母或补造历史代码指纹。

当前冻结映射覆盖全部 1005 道问题，分为 844 道 valid mapping、101 道 needs_review、60 道 not_applicable；这不等价于历史摘要的 888 道可映射题。253 条 float evidence 与 163 条 unanswerable annotation 继续作为独立单位保留，不从 planned denominator 中静默删除。

现有离线 cache 对 multi-query 缺少 982 个必需精确文本 key，影响全部 844 道可评测题。六个单查询 profile 有诊断结果，第七个 profile 的失败不计作实验完成；需要完整同模型 cache 后再验收七组消融。未授权的 provider 调用、向量替代和跳过 query 变体均不可用于消除该缺口。

人工差异样本复核和 overlap 阈值冻结仍未完成。程序可以生成候选包，但不能自批人类审核；`pending_human_review` 保留。生产策略默认值不变，任何生产切换继续需要独立变更。
