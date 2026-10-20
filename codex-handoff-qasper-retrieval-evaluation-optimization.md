# Codex 交接文档：QASPER 检索评测优化

## 1. 交接目的

本文件用于新会话继续完成 Paper RAG 的 QASPER retrieval evaluation optimization。当前已经完成 PRD 和 OpenSpec 设计，尚未开始实现这个新变更的代码任务。

新会话首先应阅读：

1. `docs/qasper-retrieval-evaluation-optimization-prd.md`
2. `openspec/changes/qasper-retrieval-evaluation-optimization/proposal.md`
3. `openspec/changes/qasper-retrieval-evaluation-optimization/design.md`
4. `openspec/changes/qasper-retrieval-evaluation-optimization/specs/qasper-retrieval-evaluation-optimization/spec.md`
5. `openspec/changes/qasper-retrieval-evaluation-optimization/tasks.md`

## 2. 当前仓库状态

- 仓库路径：`F:\github\Agora-Hub`
- 当前分支：`codex/reportbench-cross-source-eval`
- 当前 HEAD：`f9b2530d`
- `main` 已与当前 HEAD 对齐。
- 之前已合并的相关提交：
  - `7817e4f9 feat: add ReportBench cross-source evaluation adapter`
  - `f9b2530d perf: optimize hybrid field retrieval`
- 新 OpenSpec 变更：`qasper-retrieval-evaluation-optimization`
- 新 OpenSpec 当前状态：proposal/design/spec/tasks 已创建，任务全部未勾选，尚未实现。

## 3. 非常重要的工作区约束

当前工作区有大量其他进行中的修改和未跟踪文件，包含 Research evaluation、PDF parsing、frontend 和测试相关内容。不要执行以下操作：

```text
禁止 git reset --hard
禁止 git clean -fd
禁止删除或覆盖其他变更
禁止为了本变更整理整个工作区
```

只允许在本 OpenSpec 变更要求的文件范围内修改，并在提交时按路径暂存。若要修改已有文件，先检查是否包含其他会话的未提交内容，采用最小补丁方式。

## 4. 已知 QASPER 基线

当前 QASPER Dev 运行结果位于仓库外：

```text
F:\datasets\qasper\results\qasper_dev_optimized_full.json
```

运行脚本：

```text
F:\datasets\qasper\evaluate_qasper_optimized.py
```

本次基线：

```text
总问题数：1005
可映射文本证据问题：888
论文数：281
检索 chunks：17048
不可映射问题：117
float evidence：253
unanswerable annotation：163
```

严格指标：

```text
Strict Hit@5：60.0225%
Strict Hit@10：75.2252%
Strict EvidenceCoverage@5：46.3673%
Strict EvidenceCoverage@10：62.8853%
Strict MRR：39.6287%
Strict nDCG@5：35.8917%
Strict nDCG@10：41.9193%
```

内容等价指标：

```text
Equivalent Hit@5：66.1036%
Equivalent Hit@10：79.7297%
Equivalent EvidenceCoverage@5：54.9161%
Equivalent EvidenceCoverage@10：70.9132%
Equivalent MRR：47.3309%
Equivalent nDCG@5：57.8747%
Equivalent nDCG@10：69.1477%
```

解释：Equivalent 指标高于 Strict 指标，说明有一部分结果内容相关，但 chunk ID、父块/子块关系或切分边界与 QASPER 标注不完全一致。这个差异既是评测对齐问题，也是需要人工抽检的诊断信号，不能直接当成真实模型提升。

## 5. 之前已经完成的生产检索优化

已在 `f9b2530d` 中完成并合并到 main：

- `infrastructure/storage/vector/qdrant_store.py`
  - bounded thread-safe LRU query embedding cache；
  - list/tuple/set filter 使用 Qdrant `MatchAny`；
  - `clear_query_embedding_cache()`。
- `infrastructure/storage/vector/paper_field_chunk_store.py`
  - 多字段请求合并为一次 Qdrant 查询；
  - 通过 overfetch 和全局排序保持候选覆盖。
- `infrastructure/storage/vector/fake_store.py`
  - 支持 list membership filter。
- `backend/research/rag/retrieval/channels/sparse_lexical.py`
  - BM25 索引按文件签名缓存；
  - 提供 `invalidate_bm25_cache()`。

已有检查：

```text
97 个 retrieval/vector/BM25 相关测试通过
python -m scripts.dev compile 通过
Qdrant MatchAny smoke query 已执行成功
```

注意：这些修改主要优化生产延迟、查询次数和缓存，不能直接提高 QASPER 质量指标。当前 QASPER 外部评测脚本使用 NumPy in-memory store，不经过生产 Qdrant/PaperFieldChunkStore，因此不能用 QASPER 分数证明生产延迟提升。

## 6. 新变更的核心目标

实施顺序必须保持：

```text
冻结 baseline
  -> evidence mapping
  -> Strict/Equivalent 双口径
  -> 通道消融
  -> candidate/overfetch/RRF/rerank sweep
  -> 切片诊断
  -> 人工抽检并冻结阈值
  -> 如需修改生产默认值，另开变更
```

不要一开始更换 embedding 模型，也不要只把 Equivalent 指标做高。首先要回答：

1. 正确证据是否进入候选集？
2. 进入候选集后是否被 RRF/reranker 排到 Top-K？
3. 评测是否因为父子块或切分差异误判？
4. 某个通道是否带来了独立 gold evidence？

## 7. 推荐第一轮实现任务

按照 `tasks.md` 从第 1 组开始：

1. 固定 manifest 和 baseline identity；
2. 检查并复用现有：
   - `backend/research/rag/evaluation/protocol_evidence_mapping.py`
   - `backend/research/rag/evaluation/protocol_retrieval_scoring.py`
   - `backend/research/rag/evaluation/protocol_dataset.py`
   - `backend/research/rag/evaluation/protocol_execution.py`
   - `backend/research/rag/evaluation/protocol_comparison.py`
   - `interfaces/cli/commands/rag_eval.py`
3. 先补映射与评分合同，再接 profile 和 sweep；
4. 先用已存在的 embedding cache，避免新增模型调用。

建议先运行：

```powershell
openspec status --change qasper-retrieval-evaluation-optimization --json
openspec validate qasper-retrieval-evaluation-optimization --strict
python -m scripts.dev compile
```

然后查看现有评测相关测试：

```powershell
Get-ChildItem tests/backend/research/rag -Filter '*eval*'
Get-ChildItem tests/scripts -Filter '*rag*eval*'
```

## 8. 建议实验矩阵

通道消融：

```text
semantic_only
bm25_only
field_only
semantic_bm25
semantic_field
full_hybrid
full_hybrid_multi_query
```

参数候选：

```text
candidate_limit = 50, 100, 150
overfetch_multiplier = 3, 5, 8
rrf_k = 20, 60, 100
rerank_score_threshold = 0, 0.15, 0.3
```

默认优先试验点：

```text
candidate_limit = 100
overfetch_multiplier = 5
rrf_k = 60
rerank_score_threshold = 0.0
multi_query = true
```

## 9. 评测口径要求

每次结果都必须保留：

```text
StrictHit@5 / StrictHit@10
EquivalentHit@5 / EquivalentHit@10
StrictCoverage@5 / StrictCoverage@10
EquivalentCoverage@5 / EquivalentCoverage@10
MRR
nDCG@10
有效分母
计划分母
mapping status
failure reason
execution profile
```

其中：

- `StrictHit@K`：Top-K 中必须出现完全相同的 gold evidence/chunk ID；
- `EquivalentHit@K`：Top-K 中出现冻结 mapping 认可的父子块、文本 span 或内容等价证据；
- `Hit@K` 是单题二值命中；`Coverage@K` 是多条 gold evidence 的覆盖比例；
- 不可映射问题、float evidence、unanswerable 和 system failure 必须显示，不能静默从分母删除。

## 10. 生产架构护栏

新实现必须遵守：

- CLI 调 application service，不直接调用 store/executor；
- Harness 控制流程，LLM 不能决定 routing、quality pass/fail、memory write 或 publication；
- 评测不写 production index、business memory 或 active skill；
- fixture/in-memory 与 production/Qdrant 结果必须区分；
- 评测脚本不能根据返回结果动态重建 gold；
- 生产默认策略不在本变更中直接切换。

## 11. 完成判断

本变更只有在以下证据都存在时才可认为完成：

- OpenSpec strict validation 通过；
- 相关单元/集成测试通过；
- `python -m scripts.dev compile` 通过；
- 范围匹配的 smoke 通过；
- 七组通道消融有完整 manifest 和逐题结果；
- 至少一轮参数 sweep 有完整报告；
- 高差异样本完成人工抽检；
- mapping overlap 阈值和 metric version 已冻结；
- 结果报告没有把 fixture 结果冒充生产质量；
- 任何生产默认配置变更另有独立变更和回滚依据。

## 12. 新会话第一句话建议

新会话可以直接使用：

> 请先阅读 `codex-handoff-qasper-retrieval-evaluation-optimization.md`、对应 PRD 和 OpenSpec，然后检查当前工作区和变更状态；不要清理其他未提交改动，从 `tasks.md` 的第 1 组开始实现 QASPER 检索评测优化。
