# Business Question Bank v1 (draft)

## 用途

这是面向本项目跨来源研究报告流程的业务题库草稿，重点覆盖 agent / RAG / 论文研究场景。它不是直接从公开 benchmark 原样搬来的题，而是把当前仓库已有的已审阅题目作为单篇论文切片，并补充跨论文综合、来源冲突和证据不足拒答场景。

## 规模

| 类别 | 数量 | 当前状态 |
|---|---:|---|
| 单篇论文题 `single_paper` | 30 | 复用当前 `golden_set.json` 中已审阅的 agent/retrieval 题 |
| 跨论文综合题 `cross_paper_synthesis` | 30 | 已生成题面，待证据映射和评分 rubric |
| 来源冲突题 `source_conflict` | 15 | 已生成题面，待确认真实关系（兼容/条件成立/矛盾/证据不足） |
| 证据不足、应拒答题 `insufficient_evidence` | 15 | 12 道复用已审阅题，3 道新增草稿 |
| **合计** | **90** |  |

文件格式是 JSONL，每行一个 case。所有来源引用使用 `arxiv://...`，不依赖本机绝对路径。

## 重要限制

当前只有 30 道单篇论文题和 12 道复用拒答题带有现成的 `gold_evidence`。其余 48 道标记为 `draft_needs_annotation`，目前只能作为题面草稿，不能直接宣称为正式 benchmark 分数。

要冻结为正式评测集，必须为每道综合题和冲突题补齐：

1. 每篇来源论文的支持证据片段和页码/locator；
2. claim 之间的关系：`supports`、`contradicts`、`qualifies` 或 `insufficient`；
3. 预期答案要点、不可过度推断的边界；
4. 对冲突题的最终标签：`compatible`、`conditional`、`contradictory` 或 `insufficient_evidence`；
5. 至少第二位标注者复核，记录分歧和裁决；
6. 按论文家族或主题拆分 dev / held-out，避免同一论文的段落泄漏到训练和测试两侧。

## 适合本项目的评测维度

运行时建议额外记录以下 trace，否则无法评估“找到证据后是否真正交给模型”：

- 检索候选列表、分数和 rank；
- 最终送入模型的 context，以及被裁剪/丢弃的 evidence；
- 每个答案 claim 对应的 citation/evidence；
- `run_id`、模型、prompt 版本、parser 版本、索引版本；
- token、延迟、重试、失败阶段和最终拒答原因。

由这些 trace 可以计算：

- evidence retrieval recall / precision；
- final-context evidence coverage；
- context truncation loss rate；
- irrelevant-context ratio；
- claim-level citation precision / recall；
- unsupported-claim rate；
- abstention precision / recall；
- cross-source contradiction resolution accuracy；
- 端到端任务成功率、延迟、成本和稳定性。

## 推荐冻结流程

1. 固定论文版本、`source_hash` 和 corpus snapshot。
2. 先标注 30 道单篇论文题，作为可运行的基线集。
3. 再给 30 道综合题和 15 道冲突题建立 evidence graph 与答案 rubric。
4. 盲测 retrieval-only、oracle-context、agent-full 三种模式，区分检索问题和生成/编排问题。
5. 最后用 held-out 论文家族做正式报告，并保留每题 trace 供人工抽查。

## 文件

- 题库：`business_question_bank_v1_draft.jsonl`
- 现有单篇题来源：`golden_set.json`
- 现有语料快照：`golden_corpus_snapshot.json`
