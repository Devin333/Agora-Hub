## Why

业务题库只有 41 道可评分题，检索用的还是内存哈希索引，题目也没有标注答案要点。它只能帮我们定位回答链路的问题，没法给出质量结论。QASPER 是论文问答的标准测评集，有多位标注者给出的答案（抽取式、自由文本、是否题、不可回答），也有官方的 Answer-F1 口径；仓库里已有 dev 集的冻结证据映射，以及 text-embedding-v4 的 chunk 向量缓存。要拿到可以下结论的端到端数据，需要在 QASPER 上走生产回答链路，并且用真实 embedding 做检索。

现有的 QASPER 缓存读取器是严格离线的：遇到缓存里没有的文本就直接报错。而生产检索规划器会改写查询，例如在问题后面拼上证据类型，或者在重新规划时换一种问法，这些改写后的查询都不在缓存里，所以现有缓存跑不通生产链路。

## What Changes

- **查询向量补算**。chunk 和字段向量仍然只从离线缓存读取。查询向量查不到时，调用与缓存相同的 embedding 模型（`text-embedding-v4`，1024 维）补算，归一化后写入单独的补充缓存文件，重跑时直接复用，不会重复计费。补充缓存绑定基础缓存的 SHA-256 和模型身份，两者不一致时直接拒绝运行。
- **分层抽样**。按第一位标注者的答案类型（抽取式、自由文本、是否题、不可回答）等量抽样，默认每类 75 题，共 300 题。抽样使用固定种子，顺序由种子决定、可复现。抽样清单（题号、类型、种子）写入运行目录。
- **生产回答链路**。使用 `PaperRAGSession` + `PaperAnswerWorker` + `AnswerGenerator`，检索策略取生产配置（`build_retrieval_policy_from_env`），语料用缓存支持的 QASPER chunk store 和字段索引，生成模型走 Research 回答路由。
- **评分**，分四部分：
  - **官方 Answer-F1**：与 QASPER `qasper_evaluator.py` 口径一致，包括规范化、token F1，以及在多位标注者答案中取最高分。打分前先对预测做投影：拒答记为 "Unanswerable"；是否题按明确的 Yes/No 开头投影；去掉回答中的引用标记。同时按答案类型分别报告。
  - **拒答**：不可回答题的正确拒答率；可回答题的过度拒答率；系统失败单独统计，不计入这两个比率。
  - **引用与上下文覆盖**：只对冻结映射为 valid 的题计算。分 strict（精确锚点）和 equivalent（父子、相邻、规范化片段）两种口径，取自冻结映射。
  - **judge**：用 `gpt-6.1-sol`，通过 `NEWS_RAG_EVAL_JUDGE_*` 配置。对每个已作答的题判断正确性，并逐条判断引用是否支撑结论，输出严格 JSON；解析失败记为 judge_error，不计入任何正确率。
- **效率**：记录每题端到端延迟、生成延迟、token、实际发出的字符数，以及 embedding 的缓存命中、补充缓存命中和实时调用次数。
- **CLI**：新增 `rag-eval qasper answer run`，参数包括数据集路径、缓存路径、补充缓存路径、输出目录、每类题数、种子和题数上限。

## Capabilities

### New Capabilities

- `qasper-end-to-end-answer-evaluation`：QASPER dev 分层抽样端到端回答评测，包括查询向量补算、官方 Answer-F1、拒答、引用覆盖、judge 和效率指标。

### Modified Capabilities

- 无。现有 QASPER 检索评测的严格离线行为保持不变，补算只在显式传入解析器时启用。

## Impact

- 新增：
  - `backend/research/ports/qasper_query_embedding.py`
  - `backend/research/rag/evaluation/qasper_query_vectors.py`
  - `qasper_answer_metrics.py`
  - `qasper_answer_sampling.py`
  - `qasper_answer_judge.py`
  - `backend/research/application/qasper_answer_evaluation.py`
  - `infrastructure/research/qasper_query_vector_store.py`
- 修改：
  - `qasper_retrieval.py`：fixture store 和字段索引可以接收查询向量解析器
  - `qasper_assets.py`：公开基础缓存
  - `interfaces/cli/commands/qasper_eval.py`、`interfaces/composition/qasper_evaluation.py`
- 运行成本：约 300 次生成、至多 300 次 judge，以及几百次 embedding 调用。
